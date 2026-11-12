import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from bridge import ChatBridge, RunLock, check_agy, watch_shutdown
from config_store import Config
from qq_transport import QQMessage


def message(text="你好", user="owner", scope="c2c", message_id="m1"):
    return QQMessage(scope, user, user, message_id, text)


class PairingTests(unittest.IsolatedAsyncioTestCase):
    def build(self, owner=""):
        persisted = Mock()
        backend = Mock(reply=AsyncMock(return_value="你好呀"))
        service = ChatBridge(Config("123", "secret", owner_id=owner), backend,
                             emit_event=Mock(), persist=persisted)
        service.transport = Mock(send=AsyncMock())
        return service, persisted, backend

    async def test_pair_requires_correct_private_code_and_persistence(self):
        service, persist, backend = self.build()
        for item in (message("你好"), message(service.code, scope="group"),
                     QQMessage("c2c", "u", "different", "x", service.code)):
            await service.handle(item)
        self.assertEqual(service.config.owner_id, "")
        persist.assert_not_called()
        backend.reply.assert_not_awaited()
        await service.handle(message(service.code))
        self.assertEqual(service.config.owner_id, "owner")
        self.assertEqual(persist.call_args.args[0].owner_id, "owner")
        service.transport.send.assert_awaited_once()

    async def test_failed_persistence_does_not_authorize(self):
        service, persist, _ = self.build()
        persist.side_effect = OSError("cannot write")
        with self.assertRaises(OSError):
            await service.handle(message(service.code))
        self.assertEqual(service.config.owner_id, "")
        service.transport.send.assert_not_awaited()

    async def test_concurrent_pairing_grants_only_one_owner(self):
        service, persist, _ = self.build()
        await asyncio.gather(service.handle(message(service.code, user="first")),
                             service.handle(message(service.code, user="second")))
        self.assertEqual(service.config.owner_id, "first")
        persist.assert_called_once()
        self.assertTrue(service.queue.empty())

    async def test_replayed_pair_ack_and_unauthorized_messages_not_queued(self):
        service, _, _ = self.build()
        paired = message(service.code)
        await service.handle(paired)
        await service.handle(paired)
        await service.handle(message("hello", user="stranger", message_id="m2"))
        await service.handle(message("hello", scope="group", message_id="m3"))
        self.assertTrue(service.queue.empty())

    async def test_expired_code_cannot_bind(self):
        service, _, _ = self.build()
        old_code = service.code
        service.code_time -= 601
        await service.handle(message(old_code))
        self.assertEqual(service.config.owner_id, "")
        self.assertNotEqual(service.code, old_code)

    async def test_deduplication_and_bounded_queue(self):
        service, _, _ = self.build("owner")
        await service.handle(message())
        await service.handle(message())
        self.assertEqual(service.queue.qsize(), 1)
        for index in range(20):
            await service.handle(message(message_id=str(index)))
        self.assertEqual(service.queue.qsize(), 8)

    async def test_owner_chat_reset_and_delivery_limit(self):
        service, _, backend = self.build("owner")
        backend.reply.return_value = "答" * 2200
        worker = asyncio.create_task(service.consume())
        try:
            await service.handle(message())
            await asyncio.wait_for(service.queue.join(), 1)
            self.assertEqual(len(service.history), 2)
            self.assertLessEqual(len(service.transport.send.call_args.args[1]), 1800)
            await service.handle(message("/reset", message_id="reset"))
            await asyncio.wait_for(service.queue.join(), 1)
            self.assertEqual(service.history, [])
            backend.reply.assert_awaited_once()
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)

    async def test_failed_generation_does_not_pollute_history(self):
        from agy_backend import AgyError
        service, _, backend = self.build("owner")
        backend.reply.side_effect = AgyError("连接失败")
        worker = asyncio.create_task(service.consume())
        try:
            await service.handle(message())
            await asyncio.wait_for(service.queue.join(), 1)
            self.assertEqual(service.history, [])
            self.assertEqual(service.transport.send.call_args.args[1], "连接失败")
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)


class RunLockTests(unittest.TestCase):
    def test_second_instance_rejected_and_lock_released(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch("bridge.CONFIG_PATH", Path(directory) / "config.json"):
                with RunLock():
                    with self.assertRaises(RuntimeError):
                        with RunLock():
                            pass
                with RunLock():
                    pass


class ShutdownTests(unittest.IsolatedAsyncioTestCase):
    @unittest.skipUnless(os.name == "posix", "requires POSIX signals")
    async def test_daemon_entrypoint_survives_eof_and_stops_on_sigterm(self):
        script = '''
import sys
from unittest.mock import patch
from bridge import main
from config_store import Config

class FakeTransport:
    def __init__(self, *args):
        pass

    async def run(self, stop):
        print("transport-ready", flush=True)
        await stop.wait()

sys.argv = ["bridge.py", "run", "--daemon"]
with patch("bridge.load_config", return_value=Config("123", "test-secret", owner_id="owner")), \\
        patch("bridge.RunLock"), patch("qq_transport.QQTransport", FakeTransport):
    raise SystemExit(main())
'''
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-c", script, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            async with asyncio.timeout(3):
                line = b""
                while b"transport-ready" not in line:
                    line = await process.stdout.readline()
                    self.assertTrue(line, "Daemon exited before transport startup")
            await asyncio.sleep(0.05)
            self.assertIsNone(process.returncode)
            process.terminate()
            stdout, stderr = await asyncio.wait_for(process.communicate(), 3)
            self.assertEqual(process.returncode, 0, stderr.decode())
            self.assertIn(b'"stopped"', stdout)
        finally:
            if process.returncode is None:
                process.kill()
            await process.wait()

    async def test_default_mode_keeps_desktop_stdin_shutdown(self):
        stop = asyncio.Event()
        loop = Mock()
        with patch("bridge.watch_stdin") as stdin, \
                patch("bridge.asyncio.get_running_loop", return_value=loop):
            watch_shutdown(stop)
        stdin.assert_called_once_with(stop, loop)

    async def test_daemon_uses_signals_and_ignores_stdin(self):
        stop = asyncio.Event()
        loop = Mock()
        with patch("bridge.os.name", "posix"), \
                patch("bridge.watch_stdin") as stdin, \
                patch("bridge.asyncio.get_running_loop", return_value=loop):
            watch_shutdown(stop, daemon=True)
        stdin.assert_not_called()
        self.assertEqual(loop.add_signal_handler.call_count, 2)
        loop.add_signal_handler.call_args.args[1]()
        self.assertTrue(stop.is_set())

    async def test_headless_check_completes_without_stdin(self):
        backend = Mock(reply=AsyncMock(return_value="连接成功"), close=AsyncMock())
        with patch("bridge.AgyBackend", return_value=backend), \
                patch("bridge.watch_stdin") as stdin, patch("bridge.emit") as emit:
            await check_agy(Config(), daemon=True)
        stdin.assert_not_called()
        backend.reply.assert_awaited_once()
        backend.close.assert_awaited_once()
        self.assertTrue(any(call.args[0] == "checked" for call in emit.call_args_list))
