"""QQ official private and authorized @group chat -> Antigravity bridge."""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import logging
import os
import re
import secrets
import signal
import sys
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, replace

from agy_backend import AgyBackend, AgyError
from config_store import CONFIG_PATH, ROOT, Config, ConfigError, load_config, save_config
from history_commands import HistoryCommand, format_history_page, format_history_turn, parse_history_command
from history_store import ConversationKey, HistoryError, HistoryStore, trim_context
from qq_transport import QQMessage

VERSION = "0.3.0"


@dataclass(frozen=True)
class QueuedMessage:
    message: QQMessage
    version: int
    history_command: HistoryCommand | None = None
    target_version: int = 0


def emit(event: str, message: str, **extra: str) -> None:
    print(json.dumps({"event": event, "message": message, **extra}, ensure_ascii=False), flush=True)


class RunLock:
    """An OS lock, automatically released after a crash."""
    def __enter__(self):
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        self.file = (CONFIG_PATH.parent / "bridge.lock").open("a+b")
        try:
            if os.name == "nt":
                import msvcrt
                self.file.seek(0, 2)
                if not self.file.tell():
                    self.file.write(b"0")
                    self.file.flush()
                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.file.close()
            raise RuntimeError("此程序已经启动，请先停止另一个实例。") from exc
        return self

    def __exit__(self, *_):
        if os.name == "nt":
            import msvcrt
            self.file.seek(0)
            msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
        self.file.close()


class ChatBridge:
    def __init__(self, config: Config, backend: AgyBackend, *, emit_event=emit,
                 persist=save_config, archive: HistoryStore | None = None):
        self.config = config
        self.backend = backend
        self.emit = emit_event
        self.persist = persist
        self.archive = archive if archive is not None else HistoryStore()
        self.transport = None
        self.history = self.archive.load_context(self.conversation("c2c", config.owner_id)) if config.owner_id else []
        self.private_histories = {
            user: self.archive.load_context(self.conversation("c2c", user))
            for user in config.private_user_ids
        }
        self.private_versions = {user: 0 for user in config.private_user_ids}
        self.group_histories = {
            group: self.archive.load_context(self.conversation("group", group))
            for group in config.group_ids
        }
        self.group_versions = {group: 0 for group in config.group_ids}
        self.queue: asyncio.Queue[QueuedMessage] = asyncio.Queue(maxsize=8)
        self.authorization_lock = asyncio.Lock()
        self.seen: OrderedDict[str, None] = OrderedDict()
        self.code = secrets.token_hex(4)
        self.code_time = time.monotonic()
        self.group_code = ""
        self.group_code_time = 0.0
        self.invite_code = ""
        self.invite_code_time = 0.0

    def conversation(self, scope: str, chat_id: str) -> ConversationKey:
        return ConversationKey(self.config.app_id.strip(), self.config.owner_id, scope, chat_id)

    def context_for(self, scope: str, chat_id: str) -> list[dict[str, str]]:
        if scope == "group":
            return self.group_histories.setdefault(chat_id, [])
        if chat_id == self.config.owner_id:
            return self.history
        return self.private_histories.setdefault(chat_id, [])

    def announce_pairing(self) -> None:
        if not self.config.owner_id:
            self.emit("pair", "在 QQ 私聊 doro，发送此配对码。有效期 10 分钟。", code=self.code)
        else:
            self.emit("ready", f"已绑定管理员 QQ、{len(self.config.private_user_ids)} 个私聊账号和 {len(self.config.group_ids)} 个群。"
                      "管理员私聊 /invite 可邀请新账号，/group 可绑定新群。")

    def authorized(self, message) -> bool:
        if message.scope == "c2c":
            return bool(self.config.owner_id and (message.user_id == self.config.owner_id
                        or message.user_id in self.config.private_user_ids))
        return bool(self.config.owner_id and message.scope == "group"
                    and message.chat_id in self.config.group_ids)

    def still_authorized(self, message, version) -> bool:
        versions = self.group_versions if message.scope == "group" else self.private_versions
        return self.authorized(message) and versions.get(message.chat_id, 0) == version

    def remember(self, message) -> bool:
        key = f"{message.scope}:{message.chat_id}:{message.message_id}"
        if key in self.seen:
            return False
        self.seen[key] = None
        if len(self.seen) > 1000:
            self.seen.popitem(last=False)
        return True

    async def owner_command(self, message, text: str) -> bool:
        if text == "/invite":
            self.invite_code = secrets.token_hex(6)
            self.invite_code_time = time.monotonic()
            await self.transport.send(message,
                "把下面的指令发给要授权的朋友，让对方在与 doro 的私聊中发送：\n"
                f"/join {self.invite_code}\n"
                "邀请码 5 分钟有效、仅能使用一次。每个账号有独立的私聊记忆。"
                "发送 /users 可查看已授权账号。")
            self.emit("status", "已向管理员私聊发送邀请码，等待新账号加入。")
            return True
        if text == "/users":
            lines = [f"{index}. 私聊账号标识尾号 {user[-8:]}"
                     for index, user in enumerate(self.config.private_user_ids, 1)]
            listing = "\n".join(lines) if lines else "当前没有额外授权的私聊账号。"
            await self.transport.send(message,
                listing + "\n管理员账号一直保留权限。/invite 邀请新账号；/revoke 序号 解除对应账号的私聊授权。")
            return True
        if text.startswith("/revoke"):
            parts = text.split()
            if (len(parts) != 2 or parts[0] != "/revoke" or not parts[1].isascii() or not parts[1].isdigit()
                    or not 1 <= int(parts[1]) <= len(self.config.private_user_ids)):
                await self.transport.send(message, "请先私聊发送 /users 查看序号，再发送 /revoke 序号。")
                return True
            user = self.config.private_user_ids[int(parts[1]) - 1]
            updated = replace(self.config, private_user_ids=[value for value in self.config.private_user_ids if value != user])
            self.persist(updated)
            self.config.private_user_ids = updated.private_user_ids
            self.private_versions[user] = self.private_versions.get(user, 0) + 1
            self.private_histories.pop(user, None)
            self.archive.reset_context(self.conversation("c2c", user))
            self.emit("status", f"已解除一个账号的私聊授权，目前额外授权 {len(updated.private_user_ids)} 个账号。")
            await self.transport.send(message, "已解除该账号的私聊授权，并清空它的私聊记忆。")
            return True
        if text == "/group":
            self.group_code = secrets.token_hex(6)
            self.group_code_time = time.monotonic()
            await self.transport.send(message,
                "请先把 doro 添加到目标群，再在群里 @doro 并发送：\n"
                f"/bind {self.group_code}\n"
                "绑定码 5 分钟有效、仅能使用一次。绑定后群内成员 @我即可聊天。")
            self.emit("status", "已向你的私聊发送群绑定码，等待目标群内 @绑定。")
            return True
        if text == "/groups":
            lines = [f"{index}. 群标识尾号 {group[-8:]}"
                     for index, group in enumerate(self.config.group_ids, 1)]
            listing = "\n".join(lines) if lines else "当前没有授权的群。"
            await self.transport.send(message, listing + "\n私聊 /group 绑定新群；/ungroup 序号 解除对应群。")
            return True
        if text.startswith("/ungroup"):
            parts = text.split()
            if (len(parts) != 2 or not parts[1].isascii() or not parts[1].isdigit()
                    or not 1 <= int(parts[1]) <= len(self.config.group_ids)):
                await self.transport.send(message, "请先私聊发送 /groups 查看序号，再发送 /ungroup 序号。")
                return True
            group = self.config.group_ids[int(parts[1]) - 1]
            updated = replace(self.config, group_ids=[value for value in self.config.group_ids if value != group])
            self.persist(updated)
            self.config.group_ids = updated.group_ids
            self.group_versions[group] = self.group_versions.get(group, 0) + 1
            self.group_histories.pop(group, None)
            self.archive.reset_context(self.conversation("group", group))
            self.emit("status", f"已解除一个群的授权，目前启用 {len(updated.group_ids)} 个群。")
            await self.transport.send(message, "已解除该群授权，doro 将停止在该群回复。")
            return True
        return False

    async def handle(self, message) -> None:
        if (message.scope not in {"c2c", "group"} or not message.user_id or not message.chat_id
                or not message.message_id
                or (message.scope == "c2c" and message.chat_id != message.user_id)):
            return
        text = (re.sub(r"^(?:\s*<@!?\d+>\s*)+", "", message.text)
                if message.scope == "group" else message.text).strip()
        if text != message.text:
            message = replace(message, text=text)
        history_request = None
        target_version = 0
        async with self.authorization_lock:
            if message.scope == "group":
                binding = re.fullmatch(r"/bind\s+([0-9a-f]{12})", text)
                if binding:
                    if (not self.config.owner_id or not self.group_code
                            or time.monotonic() - self.group_code_time > 300
                            or not secrets.compare_digest(binding[1], self.group_code)):
                        return
                    if not self.remember(message):
                        return
                    updated = replace(self.config, group_ids=list(dict.fromkeys(
                        self.config.group_ids + [message.chat_id])))
                    if message.chat_id not in self.config.group_ids:
                        self.archive.reset_context(self.conversation("group", message.chat_id))
                    self.persist(updated)
                    if message.chat_id not in self.config.group_ids:
                        self.group_versions[message.chat_id] = self.group_versions.get(message.chat_id, 0) + 1
                    self.config.group_ids = updated.group_ids
                    self.group_code = ""
                    self.emit("status", f"群绑定成功，目前启用 {len(updated.group_ids)} 个群。")
                    await self.transport.send(message, "群绑定成功！群友 @doro 后发文字即可聊天；@doro /reset 可清空本群记忆。")
                    return
                if not self.authorized(message):
                    return
            else:
                if not self.config.owner_id:
                    if time.monotonic() - self.code_time > 600:
                        self.code = secrets.token_hex(4)
                        self.code_time = time.monotonic()
                        self.announce_pairing()
                    if not secrets.compare_digest(text.encode("utf-8"), self.code.encode("ascii")):
                        return
                    # Persist first: a failed write must never grant authorization.
                    paired = replace(self.config, owner_id=message.user_id)
                    self.archive.reset_context(ConversationKey(
                        paired.app_id.strip(), paired.owner_id, "c2c", paired.owner_id))
                    self.persist(paired)
                    self.config.owner_id = paired.owner_id
                    self.remember(message)
                    self.emit("ready", "配对成功。管理员私聊 /invite 可邀请新账号，/group 可获取群绑定码。")
                    await self.transport.send(message, "配对成功！你是 doro 的管理员，现在可以直接和我聊天。"
                        "发送 /reset 清空私聊记忆；/invite 邀请其他账号私聊；/group 获取群绑定码。")
                    return
                command = text.split(maxsplit=1)[0] if text else ""
                if command == "/join":
                    if self.authorized(message):
                        if self.remember(message):
                            await self.transport.send(message, "你已有私聊权限，可以直接和我聊天。")
                        return
                    invitation = re.fullmatch(r"/join\s+([0-9a-f]{12})", text)
                    if (invitation is None or not self.invite_code
                            or time.monotonic() - self.invite_code_time > 300
                            or not secrets.compare_digest(invitation[1], self.invite_code)):
                        return
                    updated = replace(self.config, private_user_ids=self.config.private_user_ids + [message.user_id])
                    self.archive.reset_context(self.conversation("c2c", message.user_id))
                    self.persist(updated)
                    self.config.private_user_ids = updated.private_user_ids
                    self.private_versions[message.user_id] = self.private_versions.get(message.user_id, 0) + 1
                    self.invite_code = ""
                    self.remember(message)
                    self.emit("status", f"私聊授权成功，目前额外授权 {len(updated.private_user_ids)} 个账号。")
                    await self.transport.send(message, "私聊授权成功！现在可以直接和我聊天。发送 /reset 可清空你自己的私聊记忆。")
                    return
                if not self.authorized(message):
                    return
                if (command in {"/group", "/groups", "/invite", "/users", "/revoke"}
                        or text.startswith(("/ungroup", "/revoke"))):
                    if self.remember(message):
                        if message.user_id == self.config.owner_id:
                            if not await self.owner_command(message, text):
                                await self.transport.send(message, "请发送 /invite、/users、/revoke 序号、/group、/groups 或 /ungroup 序号。")
                        else:
                            await self.transport.send(message, "授权管理命令仅管理员可用。你可以直接聊天，或发送 /reset 清空自己的私聊记忆。")
                    return
            if text.split(maxsplit=1)[:1] == ["/history"]:
                try:
                    if (message.scope == "c2c" and message.user_id != self.config.owner_id
                            and text.split()[1:2] == ["group"]):
                        raise ValueError("群记录管理仅管理员可用。发送 /history 可查看你自己的私聊记录。")
                    history_request = parse_history_command(text, message.scope, message.chat_id,
                        self.config.group_ids if message.user_id == self.config.owner_id else [])
                except ValueError as exc:
                    if self.remember(message):
                        await self.transport.send(message, str(exc))
                    return
                target_versions = self.group_versions if history_request.scope == "group" else self.private_versions
                target_version = target_versions.get(history_request.chat_id, 0)
            if not self.remember(message):
                return
            versions = self.group_versions if message.scope == "group" else self.private_versions
            try:
                self.queue.put_nowait(QueuedMessage(message, versions.get(message.chat_id, 0),
                                                   history_request, target_version))
            except asyncio.QueueFull:
                self.emit("status", "待回复消息已满，暂时忽略新的消息，请等当前回答完成。")

    async def handle_history(self, item: QueuedMessage) -> None:
        command = item.history_command
        if command.scope == "group":
            if (command.chat_id not in self.config.group_ids
                    or self.group_versions.get(command.chat_id, 0) != item.target_version):
                return
        conversation = self.conversation(command.scope, command.chat_id)
        if command.operation == "clear":
            self.archive.clear(conversation)
            self.context_for(command.scope, command.chat_id).clear()
            response = "已删除当前会话的聊天归档和上下文。"
        elif command.operation == "show":
            response = format_history_turn(self.archive.get(conversation, command.turn_id), command)
        else:
            turns, total = self.archive.page(conversation, command.page, command.keyword)
            response = format_history_page(turns, total, command)
        await self.transport.send(item.message, response)

    async def consume(self) -> None:
        while True:
            item = await self.queue.get()
            message, version = item.message, item.version
            try:
                async with self.authorization_lock:
                    if not self.still_authorized(message, version):
                        continue
                    group = message.scope == "group"
                    history = self.context_for(message.scope, message.chat_id)
                    if item.history_command is not None:
                        await self.handle_history(item)
                        continue
                    conversation = self.conversation(message.scope, message.chat_id)
                    if self.archive.contains(conversation, message.message_id):
                        continue
                    text = message.text.strip()
                    if text == "/reset":
                        self.archive.reset_context(conversation)
                        history.clear()
                        await self.transport.send(message, "已清空本群聊天记忆。" if group else "已清空私聊记忆。")
                        continue
                    if group and text.startswith(("/bind", "/group", "/ungroup", "/invite", "/join", "/users", "/revoke")):
                        await self.transport.send(message, "授权管理请在与 doro 的私聊中操作；管理员可发送 /invite 或 /group 获取授权码。")
                        continue
                    if not text:
                        if message.has_attachments:
                            await self.transport.send(message, "目前这个桥接版本支持文字聊天，请发文字给我。")
                        continue
                    if len(text) > 6000:
                        await self.transport.send(message, "这条消息较长，请拆分成每条 6000 字以内再发送。")
                        continue
                self.emit("status", "收到群内 @消息，Antigravity 正在生成回答…" if group else "收到你的私聊，Antigravity 正在生成回答…")
                if group:
                    member = hashlib.sha256(message.user_id.encode()).hexdigest()[:8]
                    text = f"[群成员 {member}] {text}"
                try:
                    answer = await self.backend.reply(history, text, group=True) if group else await self.backend.reply(history, text)
                except AgyError as exc:
                    self.emit("error", str(exc))
                    async with self.authorization_lock:
                        if self.still_authorized(message, version):
                            await self.transport.send(message, str(exc))
                    continue
                async with self.authorization_lock:
                    if not self.still_authorized(message, version):
                        continue
                    # Delivery and history changes finish before revocation is acknowledged.
                    answer = answer if len(answer) <= 1800 else answer[:1780] + "\n（回复较长，已截断）"
                    await self.transport.send(message, answer)
                    if not self.archive.append(conversation, message.message_id, text, answer, self.config.model):
                        continue
                    history.extend([{"role": "user", "content": text},
                                         {"role": "assistant", "content": answer}])
                    trim_context(history)
                    self.emit("status", "回答已发送。等待下一条消息。")
            except asyncio.CancelledError:
                raise
            except HistoryError as exc:
                self.emit("error", str(exc))
                async with self.authorization_lock:
                    if self.still_authorized(message, version):
                        try:
                            await self.transport.send(message, str(exc))
                        except Exception:
                            self.emit("error", "聊天记录错误提示发送失败，请检查 QQ 连接与网络。")
            except Exception:
                self.emit("error", "本次消息处理失败。请检查 QQ 连接、权限与网络后重试。")
            finally:
                self.queue.task_done()


def watch_stdin(stop: asyncio.Event, loop: asyncio.AbstractEventLoop) -> None:
    def read():
        try:
            for line in sys.stdin:
                if line.strip() == "stop":
                    break
        except (OSError, ValueError):
            pass
        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(stop.set)
    threading.Thread(target=read, daemon=True, name="bridge-stop").start()


def watch_shutdown(stop: asyncio.Event, *, daemon: bool = False) -> None:
    loop = asyncio.get_running_loop()
    if not daemon:
        watch_stdin(stop, loop)
    if os.name != "nt":
        for signum in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(signum, stop.set)


async def run_bridge(config: Config, *, daemon: bool = False) -> None:
    from qq_transport import QQTransport
    config.validate_qq()
    with HistoryStore(ROOT / ".local" / "history.sqlite3") as archive:
        backend = AgyBackend(config)
        bridge = ChatBridge(config, backend, archive=archive)
        stop = asyncio.Event()
        watch_shutdown(stop, daemon=daemon)
        transport = QQTransport(config.app_id, config.app_secret, bridge.handle,
                                lambda text: emit("status", text))
        bridge.transport = transport
        bridge.announce_pairing()
        worker = asyncio.create_task(bridge.consume())
        connection = asyncio.create_task(transport.run(stop))
        stopping = asyncio.create_task(stop.wait())
        try:
            done, _ = await asyncio.wait((connection, stopping), return_when=asyncio.FIRST_COMPLETED)
            if connection in done:
                await connection
        finally:
            stop.set()
            worker.cancel()
            stopping.cancel()
            await asyncio.gather(worker, stopping, return_exceptions=True)
            await backend.close()
            try:
                await asyncio.wait_for(asyncio.shield(connection), 10)
            except TimeoutError:
                connection.cancel()
                await asyncio.gather(connection, return_exceptions=True)


async def check_agy(config: Config, *, daemon: bool = False) -> None:
    stop = asyncio.Event()
    watch_shutdown(stop, daemon=daemon)
    backend = AgyBackend(config)
    emit("status", "正在测试 Antigravity 登录与文字回复…")
    reply = asyncio.create_task(backend.reply([], "只回复四个字：连接成功。"))
    stopping = asyncio.create_task(stop.wait())
    try:
        done, _ = await asyncio.wait((reply, stopping), return_when=asyncio.FIRST_COMPLETED)
        if reply in done:
            await reply
            emit("checked", "Antigravity 已能正常生成文字回答。")
    finally:
        reply.cancel()
        stopping.cancel()
        await asyncio.gather(reply, stopping, return_exceptions=True)
        await backend.close()


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="QQ 官方机器人与 Antigravity 私聊桥接")
    parser.add_argument("command", choices=["run", "check-agy", "version"])
    parser.add_argument("--daemon", action="store_true", help="由服务管理器运行，通过系统信号停止")
    args = parser.parse_args()
    if args.command == "version":
        print(VERSION)
        return 0
    # SDK/library diagnostics may contain request details: keep them out of the UI.
    logging.disable(logging.CRITICAL)
    try:
        config = load_config()
        with RunLock():
            asyncio.run(run_bridge(config, daemon=args.daemon) if args.command == "run"
                        else check_agy(config, daemon=args.daemon))
        return 0
    except KeyboardInterrupt:
        return 0
    except (ValueError, AgyError, ConfigError, HistoryError) as exc:
        emit("error", str(exc))
        return 1
    except Exception as exc:
        from qq_transport import QQError
        if isinstance(exc, QQError):
            emit("error", str(exc))
            return 1
        # Never echo raw SDK responses, credentials or untrusted exception messages.
        emit("error", "启动失败（" + type(exc).__name__ + "）。请检查配置、网络及依赖安装。")
        return 1
    finally:
        emit("stopped", "桥接已停止。")


if __name__ == "__main__":
    sys.exit(main())
