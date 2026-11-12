import asyncio
import copy
import unittest
from unittest.mock import AsyncMock, Mock

from bridge import ChatBridge
from config_store import Config
from qq_transport import QQMessage


def private(text, msg="private", user="owner"):
    return QQMessage("c2c", user, user, msg, text)


def group(text, msg="group-message", chat="group-a", user="member-a"):
    return QQMessage("group", user, chat, msg, text)


class GroupTests(unittest.IsolatedAsyncioTestCase):
    def build(self, groups=()):
        backend = Mock(reply=AsyncMock(return_value="群内回答"))
        persist = Mock()
        service = ChatBridge(Config("123", "key", owner_id="owner", group_ids=list(groups)),
                             backend, persist=persist, emit_event=Mock())
        service.transport = Mock(send=AsyncMock())
        return service, backend, persist

    async def drain(self, service):
        task = asyncio.create_task(service.consume())
        try:
            await asyncio.wait_for(service.queue.join(), 2)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_only_paired_private_owner_can_get_group_code(self):
        service, backend, _ = self.build()
        await service.handle(private("/group", user="stranger"))
        await service.handle(group("/group"))
        self.assertEqual(service.group_code, "")
        service.transport.send.assert_not_awaited()
        await service.handle(private("/group"))
        self.assertEqual(len(service.group_code), 12)
        self.assertIn(service.group_code, service.transport.send.call_args.args[1])
        backend.reply.assert_not_awaited()

    async def test_code_binds_one_group_once_and_html_at_prefix_works(self):
        service, _, persist = self.build()
        await service.handle(private("/group"))
        code = service.group_code
        await service.handle(group(f"<@!123456> /bind {code}"))
        self.assertEqual(service.config.group_ids, ["group-a"])
        self.assertEqual(persist.call_args.args[0].group_ids, ["group-a"])
        await service.handle(group(f"/bind {code}", chat="group-b", msg="second"))
        self.assertEqual(service.config.group_ids, ["group-a"])
        self.assertTrue(service.queue.empty())

    async def test_expired_or_invalid_code_does_not_authorize(self):
        service, _, persist = self.build()
        await service.handle(private("/group"))
        service.group_code_time -= 301
        await service.handle(group(f"/bind {service.group_code}"))
        await service.handle(group("/bind 000000000000", msg="other"))
        self.assertEqual(service.config.group_ids, [])
        persist.assert_not_called()

    async def test_persistence_failure_does_not_grant_group(self):
        service, _, persist = self.build()
        await service.handle(private("/group"))
        persist.side_effect = OSError()
        code = service.group_code
        with self.assertRaises(OSError):
            await service.handle(group(f"/bind {code}"))
        self.assertEqual(service.config.group_ids, [])
        self.assertEqual(service.group_code, code)

    async def test_unknown_group_is_silent_and_has_no_model_call(self):
        service, backend, _ = self.build()
        await service.handle(group("你好"))
        self.assertTrue(service.queue.empty())
        backend.reply.assert_not_awaited()
        service.transport.send.assert_not_awaited()

    async def test_private_and_each_group_context_are_isolated(self):
        service, backend, _ = self.build(("group-a", "group-b"))
        seen = []

        async def reply(history, text, **options):
            seen.append((copy.deepcopy(history), text, options))
            return "回答"

        backend.reply.side_effect = reply
        for item in (private("私聊秘密", msg="p1"), group("群一的话", msg="g1"),
                     group("群二的话", chat="group-b", msg="g2"),
                     group("群一的后续", msg="g3", user="member-b")):
            await service.handle(item)
            await self.drain(service)
        self.assertEqual(seen[1][0], [])
        self.assertEqual(seen[2][0], [])
        self.assertIn("群一的话", str(seen[3][0]))
        self.assertNotIn("私聊秘密", str(seen[3]))
        self.assertNotIn("群二的话", str(seen[3]))
        self.assertEqual(seen[1][2], {"group": True})
        self.assertNotIn("群一", str(service.history))

    async def test_group_reset_only_clears_its_own_history(self):
        service, backend, _ = self.build(("group-a", "group-b"))
        service.history[:] = [{"role": "user", "content": "private"}]
        service.group_histories = {"group-a": [{"role": "user", "content": "a"}],
                                   "group-b": [{"role": "user", "content": "b"}]}
        await service.handle(group("/reset"))
        await self.drain(service)
        self.assertEqual(service.group_histories["group-a"], [])
        self.assertEqual(len(service.group_histories["group-b"]), 1)
        self.assertEqual(len(service.history), 1)
        backend.reply.assert_not_awaited()

    async def test_group_cannot_manage_bindings_or_send_commands_to_model(self):
        service, backend, _ = self.build(("group-a",))
        for index, text in enumerate(("/group", "/groups", "/ungroup 1")):
            await service.handle(group(text, msg=str(index)))
        await self.drain(service)
        self.assertEqual(service.config.group_ids, ["group-a"])
        self.assertEqual(service.group_code, "")
        backend.reply.assert_not_awaited()

    async def test_revoke_drops_queued_requests_and_clears_group_history(self):
        service, backend, _ = self.build(("group-a",))
        service.group_histories["group-a"] = [{"role": "user", "content": "old"}]
        await service.handle(group("排队中的提问"))
        await service.handle(private("/ungroup 1"))
        await self.drain(service)
        self.assertEqual(service.config.group_ids, [])
        self.assertNotIn("group-a", service.group_histories)
        backend.reply.assert_not_awaited()

    async def test_revoke_then_rebind_cannot_revive_old_inflight_response(self):
        service, backend, _ = self.build(("group-a",))
        started, finish = asyncio.Event(), asyncio.Event()

        async def reply(*_, **__):
            started.set()
            await finish.wait()
            return "旧问题的回答，不应发送"

        backend.reply.side_effect = reply
        worker = asyncio.create_task(service.consume())
        try:
            await service.handle(group("旧问题"))
            await asyncio.wait_for(started.wait(), 1)
            await service.handle(private("/ungroup 1", msg="revoke"))
            await service.handle(private("/group", msg="code"))
            await service.handle(group(f"/bind {service.group_code}", msg="rebind"))
            finish.set()
            await asyncio.wait_for(service.queue.join(), 1)
            sent = [call.args[1] for call in service.transport.send.await_args_list]
            self.assertNotIn("旧问题的回答，不应发送", sent)
            self.assertEqual(service.group_histories.get("group-a", []), [])
        finally:
            finish.set()
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
