import asyncio
import copy
import json
import unittest
from unittest.mock import AsyncMock, Mock

from agy_backend import AgyError
from bridge import ChatBridge
from config_store import Config
from qq_transport import QQMessage


def group(text, message_id, chat_id="group-a"):
    return QQMessage("group", "group-member", chat_id, message_id, text)


def private(text, message_id, user="owner"):
    return QQMessage("c2c", user, user, message_id, text)


class ConcurrentChatTests(unittest.IsolatedAsyncioTestCase):
    def build(self, *, groups=("group-a", "group-b"), users=("friend",)):
        backend = Mock(reply=AsyncMock(return_value="已收到"))
        service = ChatBridge(
            Config("test-app", "test-secret", model="test-model", owner_id="owner",
                   group_ids=list(groups), private_user_ids=list(users)),
            backend, emit_event=Mock(), persist=Mock(),
        )
        self.addCleanup(service.archive.close)
        service.transport = Mock(send=AsyncMock())
        return service, backend

    def start_workers(self, service):
        workers = [asyncio.create_task(service.consume()) for _ in range(2)]

        async def stop():
            for worker in workers:
                worker.cancel()
            await asyncio.gather(*workers, return_exceptions=True)

        self.addAsyncCleanup(stop)

    async def test_group_backlog_allows_another_group_to_finish(self):
        service, backend = self.build()
        first_started = asyncio.Event()
        release_first = asyncio.Event()
        other_sent = asyncio.Event()
        calls = []

        async def reply(history, text, **options):
            calls.append((copy.deepcopy(history), text, options))
            if text.endswith("甲的第一问"):
                first_started.set()
                await release_first.wait()
                return "甲的第一答"
            if text.endswith("甲的第二问"):
                return "甲的第二答"
            return "乙的回答"

        async def send(message, answer):
            if message.chat_id == "group-b":
                other_sent.set()

        backend.reply.side_effect = reply
        service.transport.send.side_effect = send
        await service.handle(group("甲的第一问", "a-1"))
        await service.handle(group("甲的第二问", "a-2"))
        await service.handle(group("乙的提问", "b-1", "group-b"))
        self.start_workers(service)
        await asyncio.wait_for(first_started.wait(), 1)
        await asyncio.wait_for(other_sent.wait(), 1)
        self.assertEqual(len(calls), 2)
        self.assertTrue(calls[0][1].endswith("甲的第一问"))
        self.assertTrue(calls[1][1].endswith("乙的提问"))
        self.assertEqual(calls[0][0], [])
        self.assertEqual(calls[1][0], [])

        release_first.set()
        await asyncio.wait_for(service.queue.join(), 1)
        self.assertEqual(len(calls), 3)
        self.assertIn("甲的第一问", calls[2][0][0]["content"])
        self.assertEqual(calls[2][0][1]["content"], "甲的第一答")
        self.assertNotIn("乙的提问", str(calls[2][0]))
        self.assertEqual(calls[2][2], {"group": True})

    async def test_reset_waits_for_its_conversation_and_next_turn_uses_empty_context(self):
        service, backend = self.build()
        first_started = asyncio.Event()
        release_first = asyncio.Event()
        other_sent = asyncio.Event()
        next_contexts = []
        sent_ids = []

        async def reply(history, text, **_options):
            if text.endswith("重置前"):
                first_started.set()
                await release_first.wait()
            if text.endswith("重置后"):
                next_contexts.append(copy.deepcopy(history))
            return "回答"

        async def send(message, answer):
            sent_ids.append(message.message_id)
            if message.chat_id == "group-b":
                other_sent.set()

        backend.reply.side_effect = reply
        service.transport.send.side_effect = send
        await service.handle(group("重置前", "before"))
        await service.handle(group("/reset", "reset"))
        await service.handle(group("重置后", "after"))
        await service.handle(group("另一群的话", "other", "group-b"))
        self.start_workers(service)
        await asyncio.wait_for(first_started.wait(), 1)
        await asyncio.wait_for(other_sent.wait(), 1)
        self.assertNotIn("reset", sent_ids)
        self.assertEqual(next_contexts, [])

        release_first.set()
        await asyncio.wait_for(service.queue.join(), 1)
        self.assertEqual([item for item in sent_ids if item != "other"],
                         ["before", "reset", "after"])
        self.assertEqual(next_contexts, [[]])
        self.assertEqual(len(service.group_histories["group-a"]), 2)
        self.assertIn("重置后", service.group_histories["group-a"][0]["content"])
        conversation = service.conversation("group", "group-a")
        self.assertEqual(service.archive.page(conversation)[1], 2)
        self.assertEqual(service.archive.load_context(conversation), service.group_histories["group-a"])
        self.assertTrue(service.group_histories["group-b"])

    async def test_administrator_group_clear_waits_for_target_group_generation(self):
        service, backend = self.build()
        first_started = asyncio.Event()
        release_first = asyncio.Event()
        other_sent = asyncio.Event()
        next_contexts = []
        sent_ids = []

        async def reply(history, text, **_options):
            if text.endswith("删除前"):
                first_started.set()
                await release_first.wait()
            if text.endswith("删除后"):
                next_contexts.append(copy.deepcopy(history))
            return "回答"

        async def send(message, answer):
            sent_ids.append(message.message_id)
            if message.chat_id == "group-b":
                other_sent.set()

        backend.reply.side_effect = reply
        service.transport.send.side_effect = send
        await service.handle(group("删除前", "before"))
        await service.handle(private("/history group 1 clear", "clear"))
        await service.handle(group("删除后", "after"))
        await service.handle(group("保留的另一群", "other", "group-b"))
        self.start_workers(service)
        await asyncio.wait_for(first_started.wait(), 1)
        await asyncio.wait_for(other_sent.wait(), 1)
        self.assertNotIn("clear", sent_ids)
        self.assertEqual(next_contexts, [])

        release_first.set()
        await asyncio.wait_for(service.queue.join(), 1)
        self.assertEqual([item for item in sent_ids if item != "other"],
                         ["before", "clear", "after"])
        self.assertEqual(next_contexts, [[]])
        conversation = service.conversation("group", "group-a")
        turns, total = service.archive.page(conversation)
        self.assertEqual(total, 1)
        self.assertIn("删除后", turns[0].user_text)
        self.assertEqual(service.archive.load_context(conversation), service.group_histories["group-a"])
        self.assertEqual(service.archive.page(service.conversation("group", "group-b"))[1], 1)

    async def test_equal_private_and_group_identifiers_keep_independent_contexts(self):
        service, backend = self.build(groups=("shared", "group-b"), users=("shared",))
        group_started = asyncio.Event()
        private_started = asyncio.Event()
        release = asyncio.Event()
        calls = []

        async def reply(history, text, **options):
            calls.append((copy.deepcopy(history), text, options))
            if text.endswith("群内秘密"):
                group_started.set()
                await release.wait()
            elif text == "私聊秘密":
                private_started.set()
                await release.wait()
            return "回答"

        backend.reply.side_effect = reply
        await service.handle(group("群内秘密", "group-first", "shared"))
        await service.handle(private("私聊秘密", "private-first", "shared"))
        self.start_workers(service)
        await asyncio.wait_for(group_started.wait(), 1)
        await asyncio.wait_for(private_started.wait(), 1)
        self.assertEqual([call[0] for call in calls], [[], []])
        release.set()
        await asyncio.wait_for(service.queue.join(), 1)

        await service.handle(group("群内后续", "group-next", "shared"))
        await service.handle(private("私聊后续", "private-next", "shared"))
        await asyncio.wait_for(service.queue.join(), 1)
        group_context = next(call[0] for call in calls if call[1].endswith("群内后续"))
        private_context = next(call[0] for call in calls if call[1] == "私聊后续")
        self.assertIn("群内秘密", str(group_context))
        self.assertNotIn("私聊秘密", str(group_context))
        self.assertIn("私聊秘密", str(private_context))
        self.assertNotIn("群内秘密", str(private_context))
        self.assertEqual(len(service.group_histories["shared"]), 4)
        self.assertEqual(len(service.private_histories["shared"]), 4)

    async def test_revoke_and_rebind_discards_old_answer_with_two_workers(self):
        service, backend = self.build()
        first_started = asyncio.Event()
        release_first = asyncio.Event()

        async def reply(history, text, **_options):
            if text.endswith("旧问题"):
                first_started.set()
                await release_first.wait()
                return "旧问题的回答"
            return "新问题的回答"

        backend.reply.side_effect = reply
        self.start_workers(service)
        await service.handle(group("旧问题", "old"))
        await asyncio.wait_for(first_started.wait(), 1)
        await service.handle(private("/ungroup 1", "revoke"))
        await service.handle(private("/group", "get-code"))
        await service.handle(group(f"/bind {service.group_code}", "rebind"))
        await service.handle(group("新问题", "new"))
        release_first.set()
        await asyncio.wait_for(service.queue.join(), 1)
        sent = [call.args[1] for call in service.transport.send.await_args_list]
        self.assertNotIn("旧问题的回答", sent)
        self.assertIn("新问题的回答", sent)
        context = service.group_histories["group-a"]
        self.assertEqual(len(context), 2)
        self.assertIn("新问题", context[0]["content"])
        self.assertEqual(service.archive.page(service.conversation("group", "group-a"))[1], 1)

    async def test_success_timing_is_nonnegative_and_omits_chat_identifiers_and_text(self):
        service, _ = self.build()
        self.start_workers(service)
        await service.handle(private("仅用于测试的敏感正文", "sensitive-message-id", "friend"))
        await asyncio.wait_for(service.queue.join(), 1)
        self.assert_timing(service, "sent")

    async def test_generation_error_still_emits_safe_timing(self):
        service, backend = self.build()
        backend.reply.side_effect = AgyError("测试生成失败")
        self.start_workers(service)
        await service.handle(private("仅用于测试的敏感正文", "sensitive-message-id", "friend"))
        await asyncio.wait_for(service.queue.join(), 1)
        self.assert_timing(service, "generation_error")
        self.assertEqual(service.private_histories["friend"], [])

    def assert_timing(self, service, outcome):
        timings = [call for call in service.emit.call_args_list if call.args[0] == "timing"]
        self.assertEqual(len(timings), 1)
        timing = timings[0].kwargs
        self.assertEqual(timing["outcome"], outcome)
        self.assertEqual(timing["model"], "test-model")
        self.assertRegex(timing["request_id"], r"^[0-9a-f]+$")
        for field in ("admission_seconds", "queue_seconds", "authorization_seconds",
                      "model_seconds", "delivery_seconds", "total_seconds"):
            with self.subTest(field=field):
                self.assertIsInstance(timing[field], (int, float))
                self.assertGreaterEqual(timing[field], 0)
        serialized = json.dumps(timing, ensure_ascii=False)
        for private_value in ("仅用于测试的敏感正文", "sensitive-message-id", "friend",
                              "owner", "test-app", "test-secret", "group-member"):
            self.assertNotIn(private_value, serialized)
