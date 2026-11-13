"""Small private-chat and group-mention adapter for qqbot-agent-sdk 1.2.2.

The SDK is imported when run() starts, so the rest of the application and its
tests do not require QQ dependencies. Credentials and raw SDK exceptions must
never be included in status messages or public exceptions.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
import re
import threading
from typing import Any


@dataclass(frozen=True)
class QQMessage:
    scope: str
    user_id: str
    chat_id: str
    message_id: str
    text: str
    has_attachments: bool = False


class QQError(RuntimeError):
    """An error whose text is already safe to show to the user."""


def _failure(label: str, error: BaseException) -> str:
    """Retain a useful HTTP status, never the original exception text."""
    candidate: BaseException | None = error
    for _ in range(4):
        if candidate is None:
            break
        response = getattr(candidate, "response", None)
        code = getattr(response, "status_code", None)
        if isinstance(code, int) and 100 <= code <= 599:
            return f"{label}（HTTP {code}）"
        candidate = candidate.__cause__
    return f"{label}（{type(error).__name__}）"


def _group_text(content: str) -> str:
    # SDK 1.2.2 handles a leading @nickname, but leaves <@!123>/<@123>
    # untouched. Strip one leading platform token, never mentions in the body.
    return re.sub(r"^<@!?\d+>\s*", "", content.lstrip(), count=1)


class QQTransport:
    def __init__(
        self,
        app_id: str,
        app_secret: str,
        on_message: Callable[[QQMessage], Awaitable[None]],
        on_status: Callable[[str], None],
    ) -> None:
        self._app_id = app_id.strip()
        self._app_secret = app_secret.strip()
        self._on_message = on_message
        self._on_status = on_status
        self._api: Any = None
        self._running = False

    def _report(self, status: str) -> None:
        # A UI that has already closed must not crash the network thread.
        try:
            self._on_status(status)
        except Exception:
            pass

    async def run(self, stop: asyncio.Event) -> None:
        if self._running:
            raise QQError("QQ连接已经启动")
        if stop.is_set():
            self._report("QQ连接已停止")
            return
        if not self._app_id or not self._app_secret:
            raise QQError("请先填写QQ机器人的AppID和AppSecret")

        # Deliberately lazy: importing this module does not import the SDK.
        try:
            import httpx
            from qqbot_agent_sdk import EventParser, QQApiClient, QQWebSocket, WSCallbacks
        except ImportError:
            raise QQError("缺少QQ连接依赖，请先运行安装脚本") from None

        class UnquotedApiClient(QQApiClient):
            @staticmethod
            def build_text_body(*args: Any, **kwargs: Any) -> Any:
                body = QQApiClient.build_text_body(*args, **kwargs)
                # Keep the passive-reply ID while omitting the visible quote.
                body.message_reference = None
                return body

        self._running = True
        active = True
        loop = asyncio.get_running_loop()
        fatal: asyncio.Future[str] = loop.create_future()
        stop_wait: asyncio.Task[bool] | None = None
        ws: Any = None
        session: tuple[str | None, int | None] = (None, None)
        session_lock = threading.Lock()
        message_tasks: set[asyncio.Task[Any]] = set()

        def get_session() -> tuple[str | None, int | None]:
            with session_lock:
                return session

        def set_session(session_id: str | None, sequence: int | None) -> None:
            nonlocal session
            with session_lock:
                session = (session_id, sequence)

        def on_main(callback: Callable[..., None], *args: Any) -> None:
            def invoke() -> None:
                if active:
                    callback(*args)

            try:
                loop.call_soon_threadsafe(invoke)
            except RuntimeError:
                pass  # Shutdown can race with a final SDK callback.

        def on_fatal_error(code: str, _raw_message: str) -> None:
            # The SDK's second argument can include HTTP response text.
            # Only its short, numeric protocol close code is retained.
            code_text = str(code)
            suffix = f"（状态码 {code_text}）" if code_text.isascii() and code_text.isdigit() and len(code_text) <= 6 else ""

            def fail() -> None:
                message = f"QQ连接失败{suffix}"
                self._report(message)
                if not fatal.done():
                    fatal.set_result(message)

            on_main(fail)

        async def on_message(event_type: str, raw: dict[str, Any]) -> None:
            # Full-group and channel events never reach the application. Only
            # the platform's explicit group-@ event can trigger a group reply.
            expected_scope = {
                "C2C_MESSAGE_CREATE": "c2c",
                "GROUP_AT_MESSAGE_CREATE": "group",
            }.get(event_type)
            if not active or not self._running or expected_scope is None:
                return
            task = asyncio.current_task()
            if task is not None:
                message_tasks.add(task)
            try:
                event = EventParser.parse(event_type, raw)
                if event is None or event.chat_scope != expected_scope:
                    return
                if not event.user_id or not event.chat_id or not event.message_id:
                    self._report("已忽略不完整的QQ消息")
                    return
                if expected_scope == "c2c" and event.user_id != event.chat_id:
                    self._report("已忽略来源不一致的QQ私聊消息")
                    return
                await self._on_message(
                    QQMessage(
                        scope=expected_scope,
                        user_id=event.user_id,
                        chat_id=event.chat_id,
                        message_id=event.message_id,
                        text=_group_text(event.content) if expected_scope == "group" else event.content,
                        has_attachments=bool(event.attachments),
                    )
                )
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self._report(_failure("处理QQ消息失败", error))
            finally:
                if task is not None:
                    message_tasks.discard(task)

        try:
            self._report("正在连接QQ官方机器人")
            async with httpx.AsyncClient() as http:
                api = UnquotedApiClient(app_id=self._app_id, client_secret=self._app_secret)
                api.setup(http)
                self._api = api
                ws = QQWebSocket(
                    callbacks=WSCallbacks(
                        on_message_event=on_message,
                        on_connected=lambda: on_main(self._report, "QQ已连接，等待私聊或群内@消息"),
                        on_disconnected=lambda: on_main(self._report, "QQ连接断开，正在重连"),
                        on_fatal_error=on_fatal_error,
                        get_token=api.ensure_token_sync,
                        get_session=get_session,
                        set_session=set_session,
                        set_heartbeat_interval=lambda _seconds: None,
                        clear_token=api.clear_token,
                        fail_pending=lambda _reason: None,
                        get_gateway_url=api.get_gateway_url_sync,
                    )
                )
                try:
                    await api.ensure_token()
                    gateway_url = await api.get_gateway_url()
                    if not stop.is_set():
                        ws.start(gateway_url, loop)
                        stop_wait = asyncio.create_task(stop.wait())
                        done, _ = await asyncio.wait(
                            {stop_wait, fatal}, return_when=asyncio.FIRST_COMPLETED
                        )
                        if fatal in done:
                            raise QQError(fatal.result())
                finally:
                    active = False
                    self._running = False
                    self._api = None
                    if stop_wait is not None:
                        stop_wait.cancel()
                        await asyncio.gather(stop_wait, return_exceptions=True)
                    fatal.cancel()
                    for task in tuple(message_tasks):
                        task.cancel()
                    if message_tasks:
                        await asyncio.gather(*tuple(message_tasks), return_exceptions=True)
                    # async_stop joins the SDK's thread before HTTP is closed.
                    await ws.async_stop()
        except asyncio.CancelledError:
            raise
        except QQError:
            raise
        except Exception as error:
            raise QQError(_failure("QQ连接失败", error)) from None
        finally:
            active = False
            self._running = False
            self._api = None
            self._report("QQ连接已停止")

    async def send(self, message: QQMessage, text: str) -> None:
        if message.scope not in {"c2c", "group"}:
            raise QQError("此连接仅支持QQ私聊或群内@消息")
        if not message.chat_id or not message.user_id or not message.message_id:
            raise QQError("缺少有效的QQ回复信息")
        if message.scope == "c2c" and message.chat_id != message.user_id:
            raise QQError("QQ私聊来源信息不一致")
        if not isinstance(text, str) or not text.strip():
            raise QQError("QQ回复不能为空")
        # The SDK silently truncates longer messages. Let the caller format a
        # concise answer explicitly instead of silently dropping its tail.
        if len(text) > 4000:
            raise QQError("单条QQ回复不能超过4000字符")
        api = self._api
        if api is None or not self._running:
            raise QQError("QQ连接尚未启动或已经停止")
        try:
            await api.send_text(
                message.scope,
                message.chat_id,
                text,
                reply_to=message.message_id,
                markdown=False,
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            raise QQError(_failure("QQ回复发送失败", error)) from None
