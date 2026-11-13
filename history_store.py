"""SQLite archive and restartable context for isolated QQ conversations."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import json
import os
from pathlib import Path
import sqlite3
import time


CONVERSATION_FILTER = "app_id = ? AND owner_id = ? AND scope = ? AND chat_id = ?"
PAGE_SIZE = 5


class HistoryError(RuntimeError):
    """A storage error whose message is safe to show in QQ and status logs."""


@dataclass(frozen=True)
class ConversationKey:
    app_id: str
    owner_id: str
    scope: str
    chat_id: str

    @property
    def values(self) -> tuple[str, str, str, str]:
        return self.app_id, self.owner_id, self.scope, self.chat_id


@dataclass(frozen=True)
class SavedTurn:
    id: int
    created_at: float
    user_text: str
    assistant_text: str
    model: str


def trim_context(history: list[dict[str, str]]) -> None:
    history[:] = history[-12:]
    while len(json.dumps(history, ensure_ascii=False)) > 24000:
        del history[:2]


class HistoryStore:
    def __init__(self, path: str | Path = ":memory:") -> None:
        self._connection: sqlite3.Connection | None = None
        try:
            if str(path) != ":memory:":
                path = Path(path)
                path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
                os.close(descriptor)
                if os.name != "nt":
                    path.chmod(0o600)
            self._connection = sqlite3.connect(str(path), timeout=5)
            self._connection.row_factory = sqlite3.Row
            self._connection.execute("PRAGMA secure_delete = ON")
            self._connection.execute("PRAGMA journal_mode = DELETE")
            version = self._connection.execute("PRAGMA user_version").fetchone()[0]
            if version > 1:
                raise HistoryError("聊天记录数据库版本较新，请使用兼容的桥接版本。")
            self._connection.executescript("""
                CREATE TABLE IF NOT EXISTS conversations (
                    app_id TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    context_after_id INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (app_id, owner_id, scope, chat_id)
                );
                CREATE TABLE IF NOT EXISTS turns (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    app_id TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    user_text TEXT NOT NULL,
                    assistant_text TEXT NOT NULL,
                    model TEXT NOT NULL,
                    UNIQUE (app_id, owner_id, scope, chat_id, message_id)
                );
                CREATE INDEX IF NOT EXISTS turns_conversation
                    ON turns (app_id, owner_id, scope, chat_id, id);
                CREATE TABLE IF NOT EXISTS processed_messages (
                    app_id TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    PRIMARY KEY (app_id, owner_id, scope, chat_id, message_id)
                );
                PRAGMA user_version = 1;
            """)
        except (sqlite3.Error, OSError, HistoryError) as error:
            self.close()
            if isinstance(error, HistoryError):
                raise
            raise HistoryError("聊天记录数据库无法打开，请检查本地文件、空间与权限。") from None

    @contextmanager
    def _access(self):
        if self._connection is None:
            raise HistoryError("聊天记录数据库已经关闭。")
        try:
            with self._connection:
                yield self._connection
        except sqlite3.Error:
            raise HistoryError("聊天记录读取或保存失败，请检查本地空间与文件权限。") from None

    def __enter__(self) -> HistoryStore:
        return self

    def __exit__(self, *_args) -> None:
        self.close()

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def contains(self, conversation: ConversationKey, message_id: str) -> bool:
        with self._access() as database:
            for table in ("processed_messages", "turns"):
                found = database.execute(
                    f"SELECT 1 FROM {table} WHERE {CONVERSATION_FILTER} AND message_id = ? LIMIT 1",
                    (*conversation.values, message_id),
                ).fetchone()
                if found is not None:
                    return True
        return False

    def append(self, conversation: ConversationKey, message_id: str,
               user_text: str, assistant_text: str, model: str) -> bool:
        with self._access() as database:
            result = database.execute(
                f"""INSERT OR IGNORE INTO turns
                    (app_id, owner_id, scope, chat_id, message_id,
                     created_at, user_text, assistant_text, model)
                    SELECT ?, ?, ?, ?, ?, ?, ?, ?, ?
                    WHERE NOT EXISTS (
                        SELECT 1 FROM processed_messages
                        WHERE {CONVERSATION_FILTER} AND message_id = ?
                    )""",
                (*conversation.values, message_id, time.time(), user_text, assistant_text, model,
                 *conversation.values, message_id),
            )
            inserted = result.rowcount == 1
            database.execute(
                "INSERT OR IGNORE INTO processed_messages VALUES (?, ?, ?, ?, ?)",
                (*conversation.values, message_id),
            )
            # Keep bounded replay protection after archive deletion.
            database.execute("""
                DELETE FROM processed_messages WHERE rowid NOT IN (
                    SELECT rowid FROM processed_messages ORDER BY rowid DESC LIMIT 1000
                )
            """)
        return inserted

    def load_context(self, conversation: ConversationKey) -> list[dict[str, str]]:
        with self._access() as database:
            state = database.execute(
                f"SELECT context_after_id FROM conversations WHERE {CONVERSATION_FILTER}",
                conversation.values,
            ).fetchone()
            boundary = state[0] if state is not None else 0
            rows = database.execute(
                f"""SELECT user_text, assistant_text FROM turns
                    WHERE {CONVERSATION_FILTER} AND id > ? ORDER BY id DESC LIMIT 6""",
                (*conversation.values, boundary),
            ).fetchall()
        history = []
        for row in reversed(rows):
            history.extend([
                {"role": "user", "content": row["user_text"]},
                {"role": "assistant", "content": row["assistant_text"]},
            ])
        trim_context(history)
        return history

    @staticmethod
    def _reset(database: sqlite3.Connection, conversation: ConversationKey) -> None:
        boundary = database.execute(
            f"SELECT COALESCE(MAX(id), 0) FROM turns WHERE {CONVERSATION_FILTER}",
            conversation.values,
        ).fetchone()[0]
        database.execute(
            "INSERT OR REPLACE INTO conversations VALUES (?, ?, ?, ?, ?)",
            (*conversation.values, boundary),
        )

    def reset_context(self, conversation: ConversationKey) -> None:
        with self._access() as database:
            self._reset(database, conversation)

    def clear(self, conversation: ConversationKey) -> None:
        with self._access() as database:
            self._reset(database, conversation)
            database.execute(f"DELETE FROM turns WHERE {CONVERSATION_FILTER}", conversation.values)

    def page(self, conversation: ConversationKey, page: int = 1,
             keyword: str = "") -> tuple[list[SavedTurn], int]:
        if page < 1:
            raise ValueError("页码必须为正整数。")
        condition = CONVERSATION_FILTER
        parameters = conversation.values
        if keyword:
            condition += " AND (instr(user_text, ?) > 0 OR instr(assistant_text, ?) > 0)"
            parameters = (*parameters, keyword, keyword)
        with self._access() as database:
            total = database.execute(
                f"SELECT COUNT(*) FROM turns WHERE {condition}", parameters,
            ).fetchone()[0]
            rows = database.execute(
                f"""SELECT id, created_at, user_text, assistant_text, model FROM turns
                    WHERE {condition} ORDER BY id DESC LIMIT ? OFFSET ?""",
                (*parameters, PAGE_SIZE, (page - 1) * PAGE_SIZE),
            ).fetchall()
        return [SavedTurn(**dict(row)) for row in rows], total

    def get(self, conversation: ConversationKey, turn_id: int) -> SavedTurn | None:
        with self._access() as database:
            row = database.execute(
                f"""SELECT id, created_at, user_text, assistant_text, model FROM turns
                    WHERE {CONVERSATION_FILTER} AND id = ?""",
                (*conversation.values, turn_id),
            ).fetchone()
        return SavedTurn(**dict(row)) if row is not None else None
