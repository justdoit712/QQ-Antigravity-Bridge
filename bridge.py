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
from dataclasses import replace

from agy_backend import AgyBackend, AgyError
from config_store import CONFIG_PATH, ROOT, Config, ConfigError, load_config, save_config

VERSION = "0.2.0"


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
                 persist=save_config):
        self.config = config
        self.backend = backend
        self.emit = emit_event
        self.persist = persist
        self.transport = None
        self.history: list[dict[str, str]] = []
        self.group_histories: dict[str, list[dict[str, str]]] = {}
        self.group_versions = {group: 0 for group in config.group_ids}
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=8)
        self.authorization_lock = asyncio.Lock()
        self.seen: OrderedDict[str, None] = OrderedDict()
        self.code = secrets.token_hex(4)
        self.code_time = time.monotonic()
        self.group_code = ""
        self.group_code_time = 0.0

    def announce_pairing(self) -> None:
        if not self.config.owner_id:
            self.emit("pair", "在 QQ 私聊 doro，发送此配对码。有效期 10 分钟。", code=self.code)
        else:
            self.emit("ready", f"已绑定你的 QQ 和 {len(self.config.group_ids)} 个群。私聊发送 /group 可绑定新群。")

    def authorized(self, message) -> bool:
        if message.scope == "c2c":
            return message.user_id == self.config.owner_id
        return bool(self.config.owner_id and message.scope == "group"
                    and message.chat_id in self.config.group_ids)

    def still_authorized(self, message, version) -> bool:
        return self.authorized(message) and (message.scope != "group"
            or self.group_versions.get(message.chat_id, 0) == version)

    def remember(self, message) -> bool:
        key = f"{message.scope}:{message.chat_id}:{message.message_id}"
        if key in self.seen:
            return False
        self.seen[key] = None
        if len(self.seen) > 1000:
            self.seen.popitem(last=False)
        return True

    async def owner_command(self, message, text: str) -> bool:
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
                    self.persist(paired)
                    self.config.owner_id = paired.owner_id
                    self.remember(message)
                    self.emit("ready", "配对成功。私聊可聊天，发送 /group 可获取群绑定码。")
                    await self.transport.send(message, "配对成功！我是 doro，现在可以直接和我聊天。发送 /reset 清空私聊记忆；发送 /group 获取群绑定码。")
                    return
                if message.user_id != self.config.owner_id:
                    return
                if text in {"/group", "/groups"} or text.startswith("/ungroup"):
                    if self.remember(message):
                        await self.owner_command(message, text)
                    return
        if not self.remember(message):
            return
        try:
            self.queue.put_nowait((message, self.group_versions.get(message.chat_id, 0)))
        except asyncio.QueueFull:
            self.emit("status", "待回复消息已满，暂时忽略新的消息，请等当前回答完成。")

    async def consume(self) -> None:
        while True:
            message, version = await self.queue.get()
            try:
                if not self.still_authorized(message, version):
                    continue
                group = message.scope == "group"
                history = self.group_histories.setdefault(message.chat_id, []) if group else self.history
                text = message.text.strip()
                if text == "/reset":
                    history.clear()
                    await self.transport.send(message, "已清空本群聊天记忆。" if group else "已清空私聊记忆。")
                    continue
                if group and text.startswith(("/bind", "/group", "/ungroup")):
                    await self.transport.send(message, "群绑定管理请由已配对的账号在 doro 私聊中操作。")
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
                    if self.still_authorized(message, version):
                        await self.transport.send(message, str(exc))
                    continue
                if not self.still_authorized(message, version):
                    continue
                # QQ may reject overlong messages; send one bounded answer, not a burst.
                answer = answer if len(answer) <= 1800 else answer[:1780] + "\n（回复较长，已截断）"
                await self.transport.send(message, answer)
                history.extend([{"role": "user", "content": text},
                                     {"role": "assistant", "content": answer}])
                history[:] = history[-12:]
                while len(json.dumps(history, ensure_ascii=False)) > 24000:
                    del history[:2]
                self.emit("status", "回答已发送。等待下一条消息。")
            except asyncio.CancelledError:
                raise
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
    backend = AgyBackend(config)
    bridge = ChatBridge(config, backend)
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
    except (ValueError, AgyError, ConfigError) as exc:
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
