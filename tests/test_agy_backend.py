import asyncio
import hashlib
import json
import os
import signal
import sys
import unittest
from unittest.mock import AsyncMock, patch

from agy_backend import AgyBackend, AgyError, CHAT_SETTINGS, kill_process_tree, parse_response
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


class PromptTests(unittest.TestCase):
    def test_brief_default_allows_requested_detail_and_preserves_plain_text(self):
        prompt = AgyBackend(Config()).build_prompt([], "你好")
        self.assertIn("50～150 字", prompt)
        self.assertIn("简单问题直接简短回答", prompt)
        self.assertIn("无需凑字数", prompt)
        self.assertIn("用户明确要求详细解释时再按需展开", prompt)
        self.assertIn("纯文本聊天", prompt)
        self.assertNotIn("800 字", prompt)


class ProcessTests(unittest.IsolatedAsyncioTestCase):
    @unittest.skipUnless(os.name == "posix", "requires POSIX process groups")
    async def test_cleanup_kills_descendants_after_parent_exit(self):
        # The direct CLI can exit while a descendant still holds its output pipe.
        parent = "import subprocess,sys; subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'])"
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-c", parent, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, start_new_session=True,
        )
        try:
            async with asyncio.timeout(3):
                while process.returncode is None:
                    await asyncio.sleep(0.01)
            with patch("agy_backend.os.killpg", wraps=os.killpg) as kill_group:
                await kill_process_tree(process)
            kill_group.assert_called_once_with(process.pid, signal.SIGKILL)
            await asyncio.wait_for(process.communicate(), 3)
        finally:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

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

    async def test_finishing_one_reply_keeps_other_process_tracked(self):
        backend = AgyBackend(Config())
        first_process = AsyncMock(returncode=0)
        second_process = AsyncMock(returncode=0)
        first_started = asyncio.Event()
        second_started = asyncio.Event()
        finish_first = asyncio.Event()
        finish_second = asyncio.Event()

        async def first_reply(**_):
            first_started.set()
            await finish_first.wait()
            return result("first"), b""

        async def second_reply(**_):
            second_started.set()
            await finish_second.wait()
            return result("second"), b""

        first_process.communicate.side_effect = first_reply
        second_process.communicate.side_effect = second_reply
        with patch("agy_backend.resolve_agy", return_value="fake"), \
                patch("agy_backend.asyncio.create_subprocess_exec",
                      side_effect=[first_process, second_process]), \
                patch("agy_backend.kill_process_tree", new_callable=AsyncMock) as kill:
            first_task = asyncio.create_task(backend.reply([], "first"))
            await asyncio.wait_for(first_started.wait(), 1)
            second_task = asyncio.create_task(backend.reply([], "second"))
            await asyncio.wait_for(second_started.wait(), 1)
            try:
                finish_second.set()
                self.assertEqual(await second_task, "second")
                self.assertIs(backend.process, first_process)
                first_task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await first_task
                kill.assert_awaited_once_with(first_process)
                self.assertIsNone(backend.process)
            finally:
                for task in (first_task, second_task):
                    task.cancel()
                await asyncio.gather(first_task, second_task, return_exceptions=True)

    async def test_close_kills_both_concurrent_processes_and_stops_new_replies(self):
        backend = AgyBackend(Config())
        processes = [AsyncMock(returncode=0), AsyncMock(returncode=0)]
        started = [asyncio.Event(), asyncio.Event()]
        finished = [asyncio.Event(), asyncio.Event()]

        async def communicate(index, **_):
            started[index].set()
            await finished[index].wait()
            return result(), b""

        async def first_reply(**kwargs):
            return await communicate(0, **kwargs)

        async def second_reply(**kwargs):
            return await communicate(1, **kwargs)

        async def kill_process(process):
            index = processes.index(process)
            process.returncode = 1
            finished[index].set()

        processes[0].communicate.side_effect = first_reply
        processes[1].communicate.side_effect = second_reply
        with patch("agy_backend.resolve_agy", return_value="fake"), \
                patch("agy_backend.asyncio.create_subprocess_exec", side_effect=processes) as spawn, \
                patch("agy_backend.kill_process_tree", side_effect=kill_process) as kill:
            first_task = asyncio.create_task(backend.reply([], "first"))
            await asyncio.wait_for(started[0].wait(), 1)
            second_task = asyncio.create_task(backend.reply([], "second"))
            await asyncio.wait_for(started[1].wait(), 1)
            try:
                await backend.close()
                outcomes = await asyncio.gather(first_task, second_task, return_exceptions=True)
                self.assertTrue(all(isinstance(outcome, AgyError) for outcome in outcomes))
                self.assertIsNone(backend.process)
                killed_processes = {call.args[0] for call in kill.await_args_list}
                self.assertEqual(killed_processes, set(processes))
                with self.assertRaises(AgyError):
                    await backend.reply([], "after close")
                self.assertEqual(spawn.await_count, 2)
            finally:
                for task in (first_task, second_task):
                    task.cancel()
                await asyncio.gather(first_task, second_task, return_exceptions=True)

    async def test_close_waits_for_pending_spawn_then_kills_its_process(self):
        backend = AgyBackend(Config())
        process = AsyncMock(returncode=0)
        spawning = asyncio.Event()
        spawned = asyncio.Event()
        finished = asyncio.Event()

        async def spawn(*_, **kwargs):
            spawning.set()
            await spawned.wait()
            return process

        async def communicate(**_):
            await finished.wait()
            return result(), b""

        async def kill_process(_):
            process.returncode = 1
            finished.set()

        process.communicate.side_effect = communicate
        with patch("agy_backend.resolve_agy", return_value="fake"), \
                patch("agy_backend.asyncio.create_subprocess_exec", side_effect=spawn), \
                patch("agy_backend.kill_process_tree", side_effect=kill_process) as kill:
            reply_task = asyncio.create_task(backend.reply([], "hello"))
            await asyncio.wait_for(spawning.wait(), 1)
            close_task = asyncio.create_task(backend.close())
            try:
                await asyncio.sleep(0)
                self.assertFalse(close_task.done())
                spawned.set()
                await asyncio.wait_for(close_task, 1)
                with self.assertRaises(AgyError):
                    await reply_task
                kill.assert_any_await(process)
                self.assertIsNone(backend.process)
            finally:
                reply_task.cancel()
                close_task.cancel()
                await asyncio.gather(reply_task, close_task, return_exceptions=True)
