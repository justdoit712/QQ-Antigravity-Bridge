import asyncio
import hashlib
import json
import sys
import unittest
from unittest.mock import AsyncMock, patch

from agy_backend import AgyBackend, AgyError, CHAT_SETTINGS, parse_response
from config_store import Config


def result(response="你好", status="SUCCESS"):
    return json.dumps({"event": "result", "result": {"status": status, "response": response}},
                      ensure_ascii=False).encode("utf-8")


class ResultTests(unittest.TestCase):
    def test_unicode_success_with_intermediate_events(self):
        response = "中文\n第二行😀\u0085下一段\u2028结尾"
        output = b'{"event":"init"}\n' + result(response)
        self.assertEqual(parse_response(output, b"", 0), response)

    def test_invalid_duplicate_missing_or_failed_result_is_rejected(self):
        outputs = [b"", b"not json", b"[]", b'{"event":"result"}',
                   result() + b"\n" + result(), result(status="ERROR"),
                   result(""), result(None), b'{"event":"progress"}']
        for output in outputs:
            with self.subTest(output=output), self.assertRaises(AgyError):
                parse_response(output, b"", 0)

    def test_failure_details_not_exposed(self):
        with self.assertRaises(AgyError) as error:
            parse_response(result(), b"sensitive-token-secret", 1)
        self.assertNotIn("sensitive-token-secret", str(error.exception))


class ProcessTests(unittest.IsolatedAsyncioTestCase):
    async def test_long_unicode_prompt_over_real_stdin_and_permissions(self):
        actual_spawn = asyncio.create_subprocess_exec
        text = '中文"引号"\n下一行😀' * 500
        backend = AgyBackend(Config("123", "not-a-real-key", model="test-model"))
        fake_script = (
            "import sys,json,hashlib; "
            "e=json.loads(sys.stdin.buffer.read().decode('utf-8')); "
            "p=e['message']['content']; "
            "s=json.dumps({'sha':hashlib.sha256(p.encode()).hexdigest(),'event':e['event']}); "
            "print(json.dumps({'event':'result','result':{'status':'SUCCESS','response':s}}))"
        )

        async def substitute(*args, **kwargs):
            self.assertNotIn("--dangerously-skip-permissions", args)
            self.assertNotIn("-p", args)
            self.assertNotIn(text, args)
            self.assertNotIn("not-a-real-key", str(args))
            self.assertEqual(json.loads(args[args.index("--settings") + 1]), CHAT_SETTINGS)
            self.assertEqual(args[args.index("--model") + 1], "test-model")
            self.assertTrue(str(kwargs["cwd"]).endswith(".runtime"))
            return await actual_spawn(sys.executable, "-c", fake_script, **kwargs)

        history = [{"role": "user", "content": "之前的消息"}]
        with patch("agy_backend.resolve_agy", return_value="fake-agy"), \
                patch("agy_backend.asyncio.create_subprocess_exec", side_effect=substitute):
            response = json.loads(await backend.reply(history, text))
        self.assertEqual(response["event"], "user")
        self.assertEqual(response["sha"], hashlib.sha256(backend.build_prompt(history, text).encode()).hexdigest())
        self.assertIsNone(backend.process)

    async def test_timeout_kills_active_process(self):
        backend = AgyBackend(Config(), timeout=0.01)
        process = AsyncMock(returncode=None)

        async def slow(**_):
            await asyncio.sleep(10)

        process.communicate.side_effect = slow
        with patch("agy_backend.resolve_agy", return_value="fake"), \
                patch("agy_backend.asyncio.create_subprocess_exec", return_value=process), \
                patch("agy_backend.kill_process_tree", new_callable=AsyncMock) as kill:
            with self.assertRaises(AgyError):
                await backend.reply([], "hi")
            kill.assert_awaited_once_with(process)
        self.assertIsNone(backend.process)

    async def test_cancel_kills_active_process(self):
        backend = AgyBackend(Config())
        process = AsyncMock(returncode=None)
        started = asyncio.Event()

        async def slow(**_):
            started.set()
            await asyncio.sleep(10)

        process.communicate.side_effect = slow
        with patch("agy_backend.resolve_agy", return_value="fake"), \
                patch("agy_backend.asyncio.create_subprocess_exec", return_value=process), \
                patch("agy_backend.kill_process_tree", new_callable=AsyncMock) as kill:
            task = asyncio.create_task(backend.reply([], "hi"))
            await started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            kill.assert_awaited_once_with(process)
        self.assertIsNone(backend.process)
