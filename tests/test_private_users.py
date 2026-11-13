import asyncio
import copy
import unittest
from unittest.mock import AsyncMock, Mock

from agy_backend import AgyError
from bridge import ChatBridge
from config_store import Config
from qq_transport import QQMessage


def private(text, user="owner", msg="private"):
    return QQMessage("c2c", user, user, msg, text)


class PrivateUserTests(unittest.IsolatedAsyncioTestCase):
    def build(self, users=(), groups=()):
        backend = Mock(reply=AsyncMock(return_value="回答"))
        persist = Mock()
        service = ChatBridge(
            Config("123", "key", owner_id="owner", private_user_ids=list(users),
                   group_ids=list(groups)),
            backend, persist=persist, emit_event=Mock(),
        )
        self.addCleanup(service.archive.close)
        service.transport = Mock(send=AsyncMock())
        return service, backend, persist

    async def drain(self, service):
        task = asyncio.create_task(service.consume())
        try:
            await asyncio.wait_for(service.queue.join(), 2)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_invite_authorizes_one_guest_and_preserves_owner(self):
        service, backend, persist = self.build()
        await service.handle(private("/invite"))
        code = service.invite_code
        self.assertEqual(len(code), 12)
        self.assertIn(f"/join {code}", service.transport.send.call_args.args[1])
        await service.handle(private(f"/join {code}", user="guest", msg="join"))
        self.assertEqual(service.config.owner_id, "owner")
        self.assertEqual(service.config.private_user_ids, ["guest"])
        self.assertEqual(persist.call_args.args[0].private_user_ids, ["guest"])
        self.assertEqual(service.invite_code, "")
        self.assertTrue(service.authorized(private("hello", user="owner")))
        self.assertTrue(service.authorized(private("hello", user="guest")))
        await service.handle(private(f"/join {code}", user="other", msg="replay"))
        self.assertFalse(service.authorized(private("hello", user="other")))
        self.assertEqual(service.config.private_user_ids, ["guest"])
        self.assertTrue(service.queue.empty())
        backend.reply.assert_not_awaited()

    async def test_concurrent_join_uses_invite_once(self):
        service, _, persist = self.build()
        await service.handle(private("/invite"))
        code = service.invite_code
        await asyncio.gather(
            service.handle(private(f"/join {code}", user="first", msg="one")),
            service.handle(private(f"/join {code}", user="second", msg="two")),
        )
        self.assertEqual(service.config.private_user_ids, ["first"])
        persist.assert_called_once()

    async def test_expired_rotated_and_group_invites_cannot_authorize(self):
        service, _, persist = self.build()
        await service.handle(private("/invite", msg="first"))
        old_code = service.invite_code
        await service.handle(private("/invite", msg="second"))
        new_code = service.invite_code
        self.assertNotEqual(old_code, new_code)
        await service.handle(private(f"/join {old_code}", user="guest", msg="old"))
        await service.handle(QQMessage("group", "guest", "group", "wrong-scope", f"/join {new_code}"))
        service.invite_code_time -= 301
        await service.handle(private(f"/join {new_code}", user="guest", msg="expired"))
        self.assertEqual(service.config.private_user_ids, [])
        persist.assert_not_called()

    async def test_join_persistence_failure_keeps_code_and_allows_retry(self):
        service, _, persist = self.build()
        await service.handle(private("/invite"))
        code = service.invite_code
        join = private(f"/join {code}", user="guest", msg="join")
        persist.side_effect = OSError("disk unavailable")
        with self.assertRaises(OSError):
            await service.handle(join)
        self.assertEqual(service.config.private_user_ids, [])
        self.assertEqual(service.invite_code, code)
        self.assertNotIn("guest", service.private_versions)
        persist.side_effect = None
        await service.handle(join)
        self.assertEqual(service.config.private_user_ids, ["guest"])

    async def test_owner_and_existing_user_do_not_consume_an_invite(self):
        service, _, persist = self.build(("guest",))
        await service.handle(private("/invite"))
        code = service.invite_code
        for user in ("owner", "guest"):
            await service.handle(private(f"/join {code}", user=user, msg=f"join-{user}"))
        self.assertEqual(service.invite_code, code)
        self.assertEqual(service.config.private_user_ids, ["guest"])
        persist.assert_not_called()

    async def test_admin_commands_are_blocked_for_guests_and_groups(self):
        service, backend, persist = self.build(("guest",), ("group",))
        commands = ("/invite", "/users", "/revoke 1", "/group", "/groups", "/ungroup 1")
        for index, command in enumerate(commands):
            await service.handle(private(command, user="guest", msg=f"guest-{index}"))
            await service.handle(QQMessage("group", "member", "group", f"group-{index}", command))
        await self.drain(service)
        self.assertEqual(service.config.private_user_ids, ["guest"])
        self.assertEqual(service.config.group_ids, ["group"])
        self.assertEqual(service.invite_code, "")
        self.assertEqual(service.group_code, "")
        persist.assert_not_called()
        backend.reply.assert_not_awaited()

    async def test_owner_each_guest_and_group_have_independent_contexts(self):
        service, backend, _ = self.build(("guest-a", "guest-b"), ("group",))
        seen = []

        async def reply(history, text, **options):
            seen.append((copy.deepcopy(history), text, options))
            return "回答"

        backend.reply.side_effect = reply
        for item in (
            private("owner-only", msg="owner"),
            private("a-only", user="guest-a", msg="a1"),
            private("b-only", user="guest-b", msg="b1"),
            QQMessage("group", "member", "group", "group1", "group-only"),
            private("a-followup", user="guest-a", msg="a2"),
            private("b-followup", user="guest-b", msg="b2"),
        ):
            await service.handle(item)
            await self.drain(service)
        self.assertEqual(seen[1][0], [])
        self.assertEqual(seen[2][0], [])
        self.assertEqual(seen[3][0], [])
        self.assertEqual(seen[4][0][0]["content"], "a-only")
        self.assertEqual(seen[5][0][0]["content"], "b-only")
        self.assertNotIn("owner-only", str(seen[4:]))
        self.assertNotIn("group-only", str(seen[4:]))
        self.assertNotIn("b-only", str(seen[4]))
        self.assertNotIn("a-only", str(seen[5]))

    async def test_guest_reset_clears_only_its_context(self):
        service, backend, _ = self.build(("guest-a", "guest-b"), ("group",))
        service.history[:] = [{"role": "user", "content": "owner"}]
        service.private_histories = {
            "guest-a": [{"role": "user", "content": "a"}],
            "guest-b": [{"role": "user", "content": "b"}],
        }
        service.group_histories["group"] = [{"role": "user", "content": "group"}]
        await service.handle(private("/reset", user="guest-a"))
        await self.drain(service)
        self.assertEqual(service.private_histories["guest-a"], [])
        self.assertEqual(len(service.private_histories["guest-b"]), 1)
        self.assertEqual(len(service.history), 1)
        self.assertEqual(len(service.group_histories["group"]), 1)
        backend.reply.assert_not_awaited()

    async def test_revoke_keeps_owner_and_drops_queued_guest_requests(self):
        service, backend, persist = self.build(("guest-a", "guest-b"), ("group",))
        service.private_histories["guest-a"] = [{"role": "user", "content": "old"}]
        await service.handle(private("queued question", user="guest-a"))
        await service.handle(private("/revoke 1", msg="revoke"))
        await self.drain(service)
        self.assertEqual(service.config.owner_id, "owner")
        self.assertEqual(service.config.private_user_ids, ["guest-b"])
        self.assertEqual(service.config.group_ids, ["group"])
        self.assertNotIn("guest-a", service.private_histories)
        self.assertEqual(persist.call_args.args[0].private_user_ids, ["guest-b"])
        backend.reply.assert_not_awaited()

    async def test_failed_revoke_preserves_authorization_and_history(self):
        service, _, persist = self.build(("guest",))
        service.private_histories["guest"] = [{"role": "user", "content": "old"}]
        await service.handle(private("/invite", msg="invite"))
        code = service.invite_code
        persist.side_effect = OSError("disk unavailable")
        with self.assertRaises(OSError):
            await service.handle(private("/revoke 1", msg="revoke"))
        self.assertTrue(service.authorized(private("hello", user="guest")))
        self.assertEqual(len(service.private_histories["guest"]), 1)
        self.assertEqual(service.private_versions["guest"], 0)
        self.assertEqual(service.invite_code, code)

    async def test_users_and_invalid_revoke_do_not_expose_full_ids_or_remove_owner(self):
        guest = "private-open-id-0123456789abcdef"
        service, _, persist = self.build((guest,))
        await service.handle(private("/users", msg="list"))
        listing = service.transport.send.call_args.args[1]
        self.assertIn(guest[-8:], listing)
        self.assertNotIn(guest, listing)
        for index, text in enumerate(("/revoke", "/revoke 0", "/revoke 2", "/revoke owner", "/revokefoo 1")):
            await service.handle(private(text, msg=f"invalid-{index}"))
        self.assertEqual(service.config.owner_id, "owner")
        self.assertEqual(service.config.private_user_ids, [guest])
        persist.assert_not_called()

    async def test_pending_send_finishes_before_revoke_acknowledgement(self):
        for failure in (False, True):
            with self.subTest(failure=failure):
                service, backend, _ = self.build(("guest",))
                old_result = "old error" if failure else "old reply"
                if failure:
                    backend.reply.side_effect = AgyError(old_result)
                else:
                    backend.reply.return_value = old_result
                started, finish = asyncio.Event(), asyncio.Event()
                delivered = []

                async def send(_message, text):
                    if text == old_result:
                        started.set()
                        await finish.wait()
                    delivered.append(text)

                service.transport.send.side_effect = send
                worker = asyncio.create_task(service.consume())
                revoke = None
                try:
                    await service.handle(private("old question", user="guest", msg="old"))
                    await asyncio.wait_for(started.wait(), 1)
                    revoke = asyncio.create_task(service.handle(private("/revoke 1", msg="revoke")))
                    await asyncio.sleep(0)
                    self.assertFalse(revoke.done())
                    finish.set()
                    await asyncio.wait_for(revoke, 1)
                    await asyncio.wait_for(service.queue.join(), 1)
                    self.assertEqual(delivered[0], old_result)
                    self.assertIn("已解除", delivered[1])
                    self.assertEqual(service.config.private_user_ids, [])
                    self.assertNotIn("guest", service.private_histories)
                finally:
                    finish.set()
                    worker.cancel()
                    if revoke is not None:
                        revoke.cancel()
                    await asyncio.gather(worker, *([revoke] if revoke is not None else []), return_exceptions=True)

    async def test_revoke_and_rejoin_block_old_inflight_reply_and_error(self):
        for failure in (False, True):
            with self.subTest(failure=failure):
                service, backend, _ = self.build(("guest",))
                started, finish = asyncio.Event(), asyncio.Event()

                async def reply(*_, **__):
                    started.set()
                    await finish.wait()
                    if failure:
                        raise AgyError("old request error")
                    return "old request reply"

                backend.reply.side_effect = reply
                worker = asyncio.create_task(service.consume())
                try:
                    await service.handle(private("old question", user="guest", msg="old"))
                    await asyncio.wait_for(started.wait(), 1)
                    await service.handle(private("/revoke 1", msg="revoke"))
                    await service.handle(private("/invite", msg="invite"))
                    await service.handle(private(f"/join {service.invite_code}", user="guest", msg="rejoin"))
                    finish.set()
                    await asyncio.wait_for(service.queue.join(), 1)
                    sent = [call.args[1] for call in service.transport.send.await_args_list]
                    self.assertNotIn("old request reply", sent)
                    self.assertNotIn("old request error", sent)
                    self.assertEqual(service.private_histories.get("guest", []), [])
                finally:
                    finish.set()
                    worker.cancel()
                    await asyncio.gather(worker, return_exceptions=True)
