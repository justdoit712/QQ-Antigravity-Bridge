import asyncio
import copy
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

from bridge import ChatBridge, run_bridge
from config_store import Config
from history_store import ConversationKey, HistoryError, HistoryStore
from qq_transport import QQMessage


def private(text, message_id, user="owner"):
    return QQMessage("c2c", user, user, message_id, text)


def group(text, message_id, chat_id="group-a"):
    return QQMessage("group", "group-member", chat_id, message_id, text)


class PersistentChatTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "history.sqlite3"
        self.config = Config("123", "test-secret", owner_id="owner",
                             group_ids=["group-a", "group-b"],
                             private_user_ids=["friend-a", "friend-b"])

    def build(self, config=None):
        archive = HistoryStore(self.path)
        self.addCleanup(archive.close)
        backend = Mock(reply=AsyncMock(return_value="已收到"))
        service = ChatBridge(copy.deepcopy(config or self.config), backend,
                             archive=archive, emit_event=Mock(), persist=Mock())
        service.transport = Mock(send=AsyncMock())
        return service, backend

    async def drain(self, service):
        worker = asyncio.create_task(service.consume())
        try:
            await asyncio.wait_for(service.queue.join(), 2)
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)

    async def chat(self, service, message):
        await service.handle(message)
        await self.drain(service)

    def count(self, service, scope, chat_id):
        return service.archive.page(service.conversation(scope, chat_id))[1]

    async def test_restart_recovers_independent_private_and_group_contexts(self):
        service, _ = self.build()
        for item in (
            private("管理员的话", "owner-1"),
            private("甲的私聊", "friend-1", "friend-a"),
            private("乙的私聊", "friend-2", "friend-b"),
            group("群甲的话", "group-1"),
            group("群乙的话", "group-2", "group-b"),
        ):
            await self.chat(service, item)
        restarted, backend = self.build()
        self.assertEqual(restarted.history[0]["content"], "管理员的话")
        self.assertEqual(restarted.private_histories["friend-a"][0]["content"], "甲的私聊")
        self.assertEqual(restarted.private_histories["friend-b"][0]["content"], "乙的私聊")
        self.assertIn("群甲的话", str(restarted.group_histories["group-a"]))
        self.assertNotIn("群乙的话", str(restarted.group_histories["group-a"]))
        seen = []

        async def reply(history, text, **options):
            seen.append(copy.deepcopy(history))
            return "恢复成功"

        backend.reply.side_effect = reply
        await self.chat(restarted, private("接着聊", "friend-next", "friend-a"))
        self.assertEqual(seen[0][0]["content"], "甲的私聊")
        self.assertNotIn("管理员的话", str(seen))

    async def test_query_and_full_record_are_scoped_and_skip_model(self):
        service, backend = self.build()
        await self.chat(service, private("甲的内容", "a-1", "friend-a"))
        await self.chat(service, private("乙的内容", "b-1", "friend-b"))
        foreign = service.archive.page(service.conversation("c2c", "friend-b"))[0][0]
        backend.reply.reset_mock()
        await self.chat(service, private("/history", "a-list", "friend-a"))
        listing = service.transport.send.call_args.args[1]
        self.assertIn("甲的内容", listing)
        self.assertNotIn("乙的内容", listing)
        await self.chat(service, private(f"/history show {foreign.id}", "a-show", "friend-a"))
        self.assertIn("找不到", service.transport.send.call_args.args[1])
        await self.chat(service, private("/history search 甲的", "a-search", "friend-a"))
        self.assertIn("甲的内容", service.transport.send.call_args.args[1])
        backend.reply.assert_not_awaited()
        self.assertEqual(self.count(service, "c2c", "friend-a"), 1)

    async def test_group_queries_and_administrator_private_queries(self):
        service, backend = self.build()
        await self.chat(service, group("群甲归档", "group-a-seed"))
        await self.chat(service, group("群乙归档", "group-b-seed", "group-b"))
        backend.reply.reset_mock()
        await self.chat(service, group("/history", "group-query"))
        self.assertIn("群甲归档", service.transport.send.call_args.args[1])
        self.assertNotIn("群乙归档", service.transport.send.call_args.args[1])
        await self.chat(service, private("/history group 2", "owner-query"))
        self.assertIn("群乙归档", service.transport.send.call_args.args[1])
        await self.chat(service, private("/history group 1", "friend-query", "friend-a"))
        self.assertNotIn("群甲归档", service.transport.send.call_args.args[1])
        await self.chat(service, group("/history group 2", "cross-group-query"))
        self.assertNotIn("群乙归档", service.transport.send.call_args.args[1])
        backend.reply.assert_not_awaited()

    async def test_reset_survives_restart_and_preserves_archive(self):
        service, _ = self.build()
        await self.chat(service, private("原来的话", "before-reset", "friend-a"))
        await self.chat(service, private("另一个会话", "other", "friend-b"))
        await self.chat(service, private("/reset", "reset", "friend-a"))
        restarted, _ = self.build()
        self.assertEqual(restarted.private_histories["friend-a"], [])
        self.assertTrue(restarted.private_histories["friend-b"])
        self.assertEqual(self.count(restarted, "c2c", "friend-a"), 1)
        await self.chat(restarted, private("新的上下文", "after-reset", "friend-a"))
        final, _ = self.build()
        self.assertEqual(len(final.private_histories["friend-a"]), 2)
        self.assertEqual(final.private_histories["friend-a"][0]["content"], "新的上下文")
        self.assertEqual(self.count(final, "c2c", "friend-a"), 2)

    async def test_private_clear_is_persistent_and_blocks_replayed_message(self):
        service, _ = self.build()
        original = private("删除的文字", "old-message", "friend-a")
        await self.chat(service, original)
        await self.chat(service, private("保留的文字", "other", "friend-b"))
        await self.chat(service, private("/history clear", "clear", "friend-a"))
        restarted, backend = self.build()
        self.assertEqual(self.count(restarted, "c2c", "friend-a"), 0)
        self.assertEqual(restarted.private_histories["friend-a"], [])
        self.assertEqual(self.count(restarted, "c2c", "friend-b"), 1)
        await self.chat(restarted, original)
        backend.reply.assert_not_awaited()
        restarted.transport.send.assert_not_awaited()

    async def test_group_deletion_requires_administrator_private_command(self):
        service, backend = self.build()
        await self.chat(service, group("群甲的记录", "group-a"))
        await self.chat(service, group("群乙的记录", "group-b", "group-b"))
        backend.reply.reset_mock()
        await self.chat(service, group("/history clear", "member-clear"))
        await self.chat(service, private("/history group 1 clear", "friend-clear", "friend-a"))
        self.assertEqual(self.count(service, "group", "group-a"), 1)
        await self.chat(service, private("/history group 1 clear", "admin-clear"))
        restarted, _ = self.build()
        self.assertEqual(self.count(restarted, "group", "group-a"), 0)
        self.assertEqual(restarted.group_histories["group-a"], [])
        self.assertEqual(self.count(restarted, "group", "group-b"), 1)
        backend.reply.assert_not_awaited()

    async def test_ungroup_and_rebind_preserve_archive_with_fresh_context(self):
        service, _ = self.build()
        await self.chat(service, group("解绑前的对话", "old-group"))
        await service.handle(private("/ungroup 1", "ungroup"))
        await service.handle(private("/group", "new-code"))
        await service.handle(group(f"/bind {service.group_code}", "rebind"))
        restarted, _ = self.build(service.config)
        self.assertEqual(restarted.group_histories["group-a"], [])
        self.assertEqual(self.count(restarted, "group", "group-a"), 1)

    async def test_revoke_and_rejoin_preserve_archive_with_fresh_context(self):
        service, _ = self.build()
        await self.chat(service, private("撤销前的私聊", "old-private", "friend-a"))
        await service.handle(private("/revoke 1", "revoke"))
        await service.handle(private("/invite", "invite"))
        await service.handle(private(f"/join {service.invite_code}", "rejoin", "friend-a"))
        restarted, _ = self.build(service.config)
        self.assertEqual(restarted.private_histories["friend-a"], [])
        self.assertEqual(self.count(restarted, "c2c", "friend-a"), 1)

    async def test_administrator_repair_preserves_archive_with_fresh_context(self):
        service, _ = self.build()
        await self.chat(service, private("解除配对前的对话", "old-owner"))
        unpaired_config = replace(self.config, owner_id="", group_ids=[], private_user_ids=[])
        unpaired, backend = self.build(unpaired_config)
        await unpaired.handle(private(unpaired.code, "pair-again"))
        self.assertEqual(unpaired.config.owner_id, "owner")
        backend.reply.assert_not_awaited()
        restarted, _ = self.build(unpaired.config)
        self.assertEqual(restarted.history, [])
        self.assertEqual(self.count(restarted, "c2c", "owner"), 1)

    async def test_duplicate_after_restart_skips_generation_and_delivery(self):
        service, _ = self.build()
        original = private("只回答一次", "same-id")
        await self.chat(service, original)
        restarted, backend = self.build()
        await self.chat(restarted, original)
        backend.reply.assert_not_awaited()
        restarted.transport.send.assert_not_awaited()
        self.assertEqual(self.count(restarted, "c2c", "owner"), 1)

    async def test_failed_delivery_is_not_archived_or_restored(self):
        service, _ = self.build()
        service.transport.send.side_effect = RuntimeError("delivery failed")
        await self.chat(service, private("无法送出的问答", "failed-send"))
        self.assertEqual(service.history, [])
        self.assertEqual(self.count(service, "c2c", "owner"), 0)
        restarted, _ = self.build()
        self.assertEqual(restarted.history, [])

    async def test_failed_archive_write_does_not_commit_memory_context(self):
        service, _ = self.build()
        with patch.object(service.archive, "append", side_effect=HistoryError("聊天记录保存失败。")):
            await self.chat(service, private("保存失败的问答", "failed-archive"))
        self.assertEqual(service.history, [])
        self.assertEqual(self.count(service, "c2c", "owner"), 0)
        self.assertTrue(any(call.args[0] == "error" for call in service.emit.call_args_list))

    async def test_storage_and_error_delivery_failure_keep_worker_alive(self):
        service, _ = self.build()
        worker = asyncio.create_task(service.consume())
        try:
            service.transport.send.side_effect = RuntimeError("QQ unavailable")
            with patch.object(service.archive, "contains", side_effect=HistoryError("记录暂时不可用。")):
                await service.handle(private("读取失败", "storage-error"))
                await asyncio.wait_for(service.queue.join(), 2)
            self.assertFalse(worker.done())
            service.transport.send.side_effect = None
            await service.handle(private("下一条应继续处理", "next-message"))
            await asyncio.wait_for(service.queue.join(), 2)
            self.assertEqual(self.count(service, "c2c", "owner"), 1)
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)

    async def test_archive_and_restored_context_use_actual_truncated_reply(self):
        service, backend = self.build()
        backend.reply.return_value = "答" * 2500
        question = "问" * 6000
        await self.chat(service, private(question, "long-turn"))
        sent_answer = service.transport.send.call_args.args[1]
        saved = service.archive.page(service.conversation("c2c", "owner"))[0][0]
        self.assertLessEqual(len(sent_answer), 1800)
        self.assertEqual(saved.user_text, question)
        self.assertEqual(saved.assistant_text, sent_answer)
        restarted, _ = self.build()
        self.assertEqual(restarted.history[-1]["content"], sent_answer)

    async def test_runtime_entrypoint_creates_closes_and_recovers_archive(self):
        stores = []
        seen = []
        message_number = 0

        def open_store(path):
            store = HistoryStore(path)
            stores.append(store)
            return store

        async def reply(history, text, **options):
            seen.append(copy.deepcopy(history))
            return "入口回答"

        class Transport:
            def __init__(self, _app_id, _app_secret, on_message, _on_status):
                self.on_message = on_message

            async def send(self, _message, _text):
                pass

            async def run(self, stop):
                nonlocal message_number
                message_number += 1
                service = self.on_message.__self__
                await self.on_message(private(f"入口消息 {message_number}", f"runtime-{message_number}"))
                await service.queue.join()
                stop.set()

        backend = Mock(reply=AsyncMock(side_effect=reply), close=AsyncMock())
        with patch("bridge.ROOT", Path(self.directory.name)), \
             patch("bridge.HistoryStore", side_effect=open_store), \
             patch("bridge.AgyBackend", return_value=backend), \
             patch("bridge.watch_shutdown"), patch("bridge.emit"), patch("builtins.print"), \
             patch("qq_transport.QQTransport", Transport):
            await asyncio.wait_for(run_bridge(copy.deepcopy(self.config), daemon=True), 2)
            await asyncio.wait_for(run_bridge(copy.deepcopy(self.config), daemon=True), 2)
        self.assertTrue((Path(self.directory.name) / ".local" / "history.sqlite3").is_file())
        self.assertEqual(seen[0], [])
        self.assertEqual(seen[1][0]["content"], "入口消息 1")
        conversation = ConversationKey(self.config.app_id, self.config.owner_id, "c2c", "owner")
        for store in stores:
            with self.assertRaisesRegex(HistoryError, "已经关闭"):
                store.load_context(conversation)

    async def test_group_target_is_captured_before_group_numbers_change(self):
        service, _ = self.build()
        await self.chat(service, group("甲群记录", "a"))
        await self.chat(service, group("乙群记录", "b", "group-b"))
        await service.handle(private("/history group 2 clear", "target-b"))
        await service.handle(private("/ungroup 1", "remove-a"))
        await self.drain(service)
        self.assertEqual(self.count(service, "group", "group-b"), 0)
        self.assertEqual(self.count(service, "group", "group-a"), 1)

    async def test_stale_group_clear_cannot_apply_after_rebind(self):
        service, _ = self.build()
        await self.chat(service, group("需要保留的归档", "seed"))
        await service.handle(private("/history group 1 clear", "queued-clear"))
        await service.handle(private("/ungroup 1", "remove"))
        await service.handle(private("/group", "code"))
        await service.handle(group(f"/bind {service.group_code}", "bind"))
        await self.drain(service)
        self.assertEqual(self.count(service, "group", "group-a"), 1)

    async def test_bot_and_administrator_changes_do_not_restore_other_archive(self):
        service, _ = self.build()
        await self.chat(service, group("旧机器人记录", "old"))
        other_bot, _ = self.build(replace(self.config, app_id="456"))
        other_owner, _ = self.build(replace(self.config, owner_id="another-owner"))
        self.assertEqual(other_bot.group_histories["group-a"], [])
        self.assertEqual(other_owner.group_histories["group-a"], [])
        self.assertEqual(self.count(other_bot, "group", "group-a"), 0)
        self.assertEqual(self.count(other_owner, "group", "group-a"), 0)


if __name__ == "__main__":
    unittest.main()
