"""Parse and display chat-history commands after conversation authorization."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Sequence


PAGE_SIZE = 5
DETAIL_PAGE_SIZE = 1500
CHINA_TIME = timezone(timedelta(hours=8))


@dataclass(frozen=True)
class HistoryCommand:
    scope: str
    chat_id: str
    operation: str = "list"
    page: int = 1
    keyword: str = ""
    turn_id: int = 0


def history_usage(scope: str) -> str:
    usage = (
        "记录指令\n"
        "/history [页码]\n"
        "/history search 关键词\n"
        "/history show 记录ID [页码]"
    )
    if scope == "c2c":
        return usage + (
            "\n/history clear\n"
            "管理员私聊可加 group 群序号 指定群，例如 /history group 1。"
            "群序号通过 /groups 查询。"
        )
    return usage + "\n群记录删除请由管理员私聊发送 /history group 群序号 clear。"


def _positive_number(value: str, usage: str) -> int:
    if not value.isascii() or not value.isdigit() or len(value) > 9 or int(value) < 1:
        raise ValueError(usage)
    return int(value)


def parse_history_command(text: str, scope: str, chat_id: str,
                          group_ids: list[str]) -> HistoryCommand:
    """Resolve a command target within the caller's authorized conversation list."""
    usage = history_usage(scope)
    parts = text.strip().split(maxsplit=1)
    if scope not in {"c2c", "group"} or not chat_id or not parts or parts[0] != "/history":
        raise ValueError(usage)

    target_scope = scope
    target_chat = chat_id
    arguments = parts[1] if len(parts) == 2 else ""
    group_parts = arguments.split(maxsplit=2)
    if group_parts and group_parts[0] == "group":
        if scope != "c2c" or len(group_parts) < 2:
            raise ValueError(usage)
        group_number = _positive_number(group_parts[1], usage)
        if group_number > len(group_ids):
            raise ValueError("群序号无效，请先私聊发送 /groups 查看当前序号。")
        target_scope = "group"
        target_chat = group_ids[group_number - 1]
        arguments = group_parts[2] if len(group_parts) == 3 else ""

    action_parts = arguments.split(maxsplit=1)
    if not action_parts:
        return HistoryCommand(target_scope, target_chat)

    action = action_parts[0]
    if action == "search":
        if len(action_parts) != 2 or not action_parts[1].strip():
            raise ValueError(usage)
        return HistoryCommand(target_scope, target_chat, "search", keyword=action_parts[1].strip())

    if action == "show":
        details = action_parts[1].split() if len(action_parts) == 2 else []
        if len(details) not in {1, 2}:
            raise ValueError(usage)
        turn_id = _positive_number(details[0], usage)
        page = _positive_number(details[1], usage) if len(details) == 2 else 1
        return HistoryCommand(target_scope, target_chat, "show", page=page, turn_id=turn_id)

    if action == "clear":
        if len(action_parts) != 1:
            raise ValueError(usage)
        if scope != "c2c":
            raise ValueError("群记录删除请由管理员私聊发送 /history group 群序号 clear。")
        return HistoryCommand(target_scope, target_chat, "clear")

    if len(action_parts) != 1:
        raise ValueError(usage)
    return HistoryCommand(target_scope, target_chat, page=_positive_number(action, usage))


def _summary(text: str) -> str:
    summary = " ".join(text.split())
    return summary if len(summary) <= 100 else summary[:99] + "…"


def format_history_page(turns: Sequence[Any], total: int, command: HistoryCommand) -> str:
    """Show up to five saved exchanges within one QQ text message."""
    is_search = command.operation == "search" or bool(command.keyword)
    if total == 0:
        return "当前会话暂无匹配的聊天记录。" if is_search else "当前会话暂无聊天记录。"
    page_count = (total + PAGE_SIZE - 1) // PAGE_SIZE
    if not is_search and not 1 <= command.page <= page_count:
        return f"页码超出范围，当前共有 {page_count} 页记录。"

    if is_search:
        lines = [f"匹配记录共 {total} 轮，显示最近 {min(len(turns), PAGE_SIZE)} 轮（北京时间）"]
    else:
        lines = [f"聊天记录 第 {command.page}/{page_count} 页，共 {total} 轮（北京时间）"]
    for turn in turns[:PAGE_SIZE]:
        created = datetime.fromtimestamp(turn.created_at, CHINA_TIME).strftime("%m-%d %H:%M")
        lines.extend(("", f"#{turn.id}  {created}",
                      "用户　" + _summary(turn.user_text),
                      "doro　" + _summary(turn.assistant_text)))
    lines.extend(("", "查看全文用 /history show 记录ID [页码]。指定群时沿用 group 群序号前缀。"))
    return "\n".join(lines)


def format_history_turn(turn: Any | None, command: HistoryCommand) -> str:
    """Display a complete saved exchange over bounded pages."""
    if turn is None:
        return "当前会话中找不到这条记录。"
    body = f"用户\n{turn.user_text}\n\ndoro\n{turn.assistant_text}"
    page_count = (len(body) + DETAIL_PAGE_SIZE - 1) // DETAIL_PAGE_SIZE
    if not 1 <= command.page <= page_count:
        return f"页码超出范围，这条记录共有 {page_count} 页。"

    created = datetime.fromtimestamp(turn.created_at, CHINA_TIME).strftime("%Y-%m-%d %H:%M")
    start = (command.page - 1) * DETAIL_PAGE_SIZE
    header = (f"记录 #{turn.id}  第 {command.page}/{page_count} 页（北京时间）\n"
              f"时间 {created}\n模型 {_summary(turn.model or 'CLI 默认模型')}")
    return (header + "\n\n" + body[start:start + DETAIL_PAGE_SIZE]
            + "\n\n全文分段显示，修改原指令的页码即可翻页。")
