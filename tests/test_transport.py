from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
import sys
import threading
import types
import unittest
from unittest.mock import patch

from qq_transport import QQError, QQMessage, QQTransport


@dataclass
class FakeCallbacks:
    # Mirrors the required qqbot-agent-sdk 1.2.2 constructor arguments.
    on_message_event: object
    on_connected: object
    on_disconnected: object
    on_fatal_error: object
    get_token: object
    get_session: object
    set_session: object
    set_heartbeat_interval: object
    clear_token: object
    fail_pending: object
    get_gateway_url: object


class FakeSDK:
    def __init__(self):
        self.started = asyncio.Event()
        self.api = None
        self.socket = None
        self.http_open = False
        self.http_closed = False
        self.socket_stopped = False
        self.init_error = None
        self.send_error = None
        self.sent = []
        self.bodies = []
        owner = self

        class HTTP:
            async def __aenter__(self):
                owner.http_open = True
                owner.http_closed = False
                return self

            async def __aexit__(self, *_args):
                owner.http_closed = True

        class API:
            def __init__(self, app_id, client_secret):
                owner.api = self
                self.app_id = app_id
                self.client_secret = client_secret
                self.http = None

            def setup(self, http):
                self.http = http

            async def ensure_token(self):
                assert self.http is not None
                if owner.init_error:
                    raise owner.init_error
                return "test-token"

            async def get_gateway_url(self):
                return "wss://example.invalid/gateway"

            def ensure_token_sync(self):
                return "test-token"

            def get_gateway_url_sync(self):
                return "wss://example.invalid/gateway"

            def clear_token(self):
                pass

            @staticmethod
            def build_text_body(content, reply_to=None, markdown=True, max_length=4000):
                return types.SimpleNamespace(
                    content=content[:max_length],
                    msg_id=reply_to or "",
                    msg_seq=1,
                    message_reference=types.SimpleNamespace(message_id=reply_to) if reply_to else None,
                )

            async def send_text(self, *args, **kwargs):
                if owner.send_error:
                    raise owner.send_error
                owner.sent.append((args, kwargs))
                owner.bodies.append(self.build_text_body(args[2], **kwargs))

        class WebSocket:
            def __init__(self, callbacks):
                owner.socket = self
                self.callbacks = callbacks

            def start(self, url, main_loop):
                assert owner.http_open
                self.url = url
                self.loop = main_loop
                self.callbacks.on_connected()
                owner.started.set()

            async def async_stop(self):
                assert not owner.http_closed
                owner.socket_stopped = True

        class Parser:
            @staticmethod
            def parse(_event_type, raw):
                return types.SimpleNamespace(**raw) if raw else None

        self.modules = {
            "httpx": types.SimpleNamespace(AsyncClient=HTTP),
            "qqbot_agent_sdk": types.SimpleNamespace(
                QQApiClient=API,
                QQWebSocket=WebSocket,
                WSCallbacks=FakeCallbacks,
                EventParser=Parser,
            ),
        }


def frame(**changes):
    payload = dict(
        chat_scope="c2c",
        user_id="owner-openid",
        chat_id="owner-openid",
        message_id="message-id",
        content="你好",
        attachments=[],
    )
    payload.update(changes)
    return payload


class TransportTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.sdk = FakeSDK()
        self.received = []
        self.statuses = []

        async def on_message(message):
            self.received.append(message)

        self.transport = QQTransport("123456", "local-secret", on_message, self.statuses.append)
        self.stop = asyncio.Event()

    @asynccontextmanager
    async def running(self):
        with patch.dict(sys.modules, self.sdk.modules):
            runner = asyncio.create_task(self.transport.run(self.stop))
            await asyncio.wait_for(self.sdk.started.wait(), 1)
            try:
                yield runner
            finally:
                self.stop.set()
                await asyncio.wait_for(runner, 1)

    async def test_setup_callbacks_session_and_orderly_stop(self):
        async with self.running():
            callbacks = self.sdk.socket.callbacks
            callbacks.set_session("session", 9)
            self.assertEqual(callbacks.get_session(), ("session", 9))
            self.assertEqual(callbacks.get_token(), "test-token")
            self.assertIsNotNone(self.sdk.api.http)
            self.assertEqual(self.sdk.sent, [])
        self.assertTrue(self.sdk.socket_stopped)
        self.assertTrue(self.sdk.http_closed)
        self.assertIn("QQ已连接，等待私聊或群内@消息", self.statuses)

    async def test_c2c_validation_rejects_inconsistent_and_incomplete_messages(self):
        async with self.running():
            callback = self.sdk.socket.callbacks.on_message_event
            await callback("C2C_MESSAGE_CREATE", frame(chat_scope="group"))
            await callback("C2C_MESSAGE_CREATE", {})
            await callback("C2C_MESSAGE_CREATE", frame(message_id=""))
            await callback("C2C_MESSAGE_CREATE", frame(chat_id="someone-else"))
            await callback("C2C_MESSAGE_CREATE", frame(attachments=[{"url": "ignored"}]))
            self.assertEqual(
                self.received,
                [QQMessage("c2c", "owner-openid", "owner-openid", "message-id", "你好", True)],
            )
            self.assertEqual(self.sdk.sent, [])

    async def test_group_mention_preserves_distinct_sender_and_group_ids(self):
        async with self.running():
            callback = self.sdk.socket.callbacks.on_message_event
            await callback(
                "GROUP_AT_MESSAGE_CREATE",
                frame(
                    chat_scope="group",
                    user_id="member-openid",
                    chat_id="group-openid",
                    content="<@!123456> 你好",
                ),
            )
            await callback("GROUP_AT_MESSAGE_CREATE", frame(chat_scope="c2c"))
            await callback(
                "GROUP_AT_MESSAGE_CREATE",
                frame(chat_scope="group", user_id="", chat_id="group-openid"),
            )
            self.assertEqual(
                self.received,
                [QQMessage("group", "member-openid", "group-openid", "message-id", "你好")],
            )
            self.assertEqual(self.sdk.sent, [])

    async def test_only_leading_group_mention_is_stripped(self):
        async with self.running():
            callback = self.sdk.socket.callbacks.on_message_event
            await callback(
                "GROUP_AT_MESSAGE_CREATE",
                frame(
                    chat_scope="group",
                    user_id="member-openid",
                    chat_id="group-openid",
                    content="<@123456> 请问 <@!654321> 的问题",
                ),
            )
            await callback("C2C_MESSAGE_CREATE", frame(content="<@!123456> 请保留"))
            self.assertEqual(
                [message.text for message in self.received],
                ["请问 <@!654321> 的问题", "<@!123456> 请保留"],
            )

    async def test_full_group_and_channel_events_are_ignored(self):
        async with self.running():
            callback = self.sdk.socket.callbacks.on_message_event
            # Even a plausible parsed scope must not turn full-group messages
            # or channel events into an accepted @-mention event.
            for event_type in (
                "GROUP_MESSAGE_CREATE",
                "GUILD_MESSAGE_CREATE",
                "GUILD_AT_MESSAGE_CREATE",
                "DIRECT_MESSAGE_CREATE",
            ):
                await callback(
                    event_type,
                    frame(chat_scope="group", user_id="member-openid", chat_id="group-openid"),
                )
            self.assertEqual(self.received, [])
            self.assertEqual(self.sdk.sent, [])

    async def test_send_uses_passive_plain_text_without_quote(self):
        message = QQMessage("c2c", "owner-openid", "owner-openid", "message-id", "hello")
        async with self.running():
            await self.transport.send(message, "reply")
            self.assertEqual(
                self.sdk.sent,
                [(("c2c", "owner-openid", "reply"), {"reply_to": "message-id", "markdown": False})],
            )
            self.assertEqual(self.sdk.bodies[0].msg_id, "message-id")
            self.assertEqual(self.sdk.bodies[0].msg_seq, 1)
            self.assertIsNone(self.sdk.bodies[0].message_reference)
        with self.assertRaisesRegex(QQError, "已经停止"):
            await self.transport.send(message, "reply")

    async def test_group_send_targets_group_without_quote(self):
        message = QQMessage("group", "member-openid", "group-openid", "group-message", "hello")
        async with self.running():
            await self.transport.send(message, "group reply")
            self.assertEqual(
                self.sdk.sent,
                [(("group", "group-openid", "group reply"), {"reply_to": "group-message", "markdown": False})],
            )
            self.assertEqual(self.sdk.bodies[0].msg_id, "group-message")
            self.assertEqual(self.sdk.bodies[0].msg_seq, 1)
            self.assertIsNone(self.sdk.bodies[0].message_reference)

    async def test_rejects_other_scopes_and_silent_truncation(self):
        async with self.running():
            with self.assertRaisesRegex(QQError, "仅支持QQ私聊或群内@消息"):
                await self.transport.send(QQMessage("guild", "user", "channel", "id", ""), "reply")
            with self.assertRaisesRegex(QQError, "4000"):
                await self.transport.send(QQMessage("c2c", "u", "u", "id", ""), "x" * 4001)
            self.assertEqual(self.sdk.sent, [])

    async def test_thread_fatal_is_safe_and_closes_resources(self):
        with patch.dict(sys.modules, self.sdk.modules):
            runner = asyncio.create_task(self.transport.run(self.stop))
            await asyncio.wait_for(self.sdk.started.wait(), 1)
            thread = threading.Thread(
                target=self.sdk.socket.callbacks.on_fatal_error,
                args=("4014", "local-secret raw server response"),
            )
            thread.start()
            thread.join(timeout=1)
            with self.assertRaisesRegex(QQError, "4014") as caught:
                await asyncio.wait_for(runner, 1)
        self.assertNotIn("local-secret", str(caught.exception))
        self.assertFalse(any("local-secret" in status for status in self.statuses))
        self.assertTrue(self.sdk.socket_stopped)
        self.assertTrue(self.sdk.http_closed)

    async def test_initialization_and_send_errors_do_not_expose_response(self):
        self.sdk.init_error = RuntimeError("local-secret raw HTTP response")
        with patch.dict(sys.modules, self.sdk.modules):
            with self.assertRaises(QQError) as caught:
                await self.transport.run(self.stop)
        self.assertNotIn("local-secret", str(caught.exception))
        self.assertTrue(caught.exception.__suppress_context__)
        self.assertTrue(self.sdk.http_closed)

        self.sdk.init_error = None
        self.sdk.send_error = RuntimeError("local-secret raw HTTP response")
        async with self.running():
            with self.assertRaises(QQError) as caught:
                await self.transport.send(QQMessage("c2c", "u", "u", "id", ""), "reply")
            self.assertNotIn("local-secret", str(caught.exception))
            self.assertTrue(caught.exception.__suppress_context__)

    async def test_stop_cancels_inflight_message_handler(self):
        began = asyncio.Event()
        cancelled = asyncio.Event()

        async def wait_forever(_message):
            began.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        self.transport._on_message = wait_forever
        async with self.running():
            task = asyncio.create_task(
                self.sdk.socket.callbacks.on_message_event("C2C_MESSAGE_CREATE", frame())
            )
            await asyncio.wait_for(began.wait(), 1)
        self.assertTrue(cancelled.is_set())
        self.assertTrue(task.cancelled())


if __name__ == "__main__":
    unittest.main()
