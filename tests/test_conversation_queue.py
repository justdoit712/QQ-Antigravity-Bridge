import asyncio
import unittest
from dataclasses import dataclass

from conversation_queue import ConversationQueue


@dataclass(frozen=True)
class Item:
    conversation: str
    text: str


class ConversationQueueTests(unittest.IsolatedAsyncioTestCase):
    def build(self, maxsize=8):
        return ConversationQueue[Item](maxsize=maxsize, key=lambda item: item.conversation)

    async def test_same_conversation_waits_until_active_item_finishes(self):
        queue = self.build()
        first, second = Item("a", "first"), Item("a", "second")
        queue.put_nowait(first)
        queue.put_nowait(second)
        self.assertIs(await queue.get(), first)
        waiting = asyncio.create_task(queue.get())
        try:
            await asyncio.sleep(0)
            self.assertFalse(waiting.done())
            queue.task_done(first)
            self.assertIs(await asyncio.wait_for(waiting, 1), second)
            queue.task_done(second)
            await asyncio.wait_for(queue.join(), 1)
        finally:
            waiting.cancel()
            await asyncio.gather(waiting, return_exceptions=True)

    async def test_busy_conversation_does_not_occupy_another_worker(self):
        queue = self.build()
        first, second, other = Item("a", "first"), Item("a", "second"), Item("b", "other")
        for item in (first, second, other):
            queue.put_nowait(item)
        self.assertIs(await queue.get(), first)
        self.assertIs(await asyncio.wait_for(queue.get(), 1), other)
        queue.task_done(other)
        queue.task_done(first)
        self.assertIs(await queue.get(), second)
        queue.task_done(second)
        await asyncio.wait_for(queue.join(), 1)

    async def test_ready_conversations_run_in_arrival_order(self):
        queue = self.build()
        first, next_same = Item("a", "first"), Item("a", "next")
        other, last = Item("b", "other"), Item("c", "last")
        for item in (first, next_same, other, last):
            queue.put_nowait(item)
        self.assertIs(await queue.get(), first)
        queue.task_done(first)
        for expected in (other, last, next_same):
            self.assertIs(await queue.get(), expected)
            queue.task_done(expected)
        await asyncio.wait_for(queue.join(), 1)

    async def test_capacity_counts_all_pending_items_and_excludes_active_items(self):
        queue = self.build(maxsize=2)
        first, next_same, other = Item("a", "first"), Item("a", "next"), Item("b", "other")
        queue.put_nowait(first)
        queue.put_nowait(next_same)
        with self.assertRaises(asyncio.QueueFull):
            queue.put_nowait(other)
        self.assertIs(await queue.get(), first)
        self.assertEqual(queue.qsize(), 1)
        queue.put_nowait(other)
        self.assertEqual(queue.qsize(), 2)
        with self.assertRaises(asyncio.QueueFull):
            queue.put_nowait(Item("c", "overflow"))
        self.assertIs(await queue.get(), other)
        queue.task_done(other)
        queue.task_done(first)
        self.assertIs(await queue.get(), next_same)
        queue.task_done(next_same)
        await asyncio.wait_for(queue.join(), 1)

    async def test_join_waits_for_active_items_even_when_queue_is_empty(self):
        queue = self.build()
        item = Item("a", "first")
        queue.put_nowait(item)
        self.assertIs(await queue.get(), item)
        self.assertTrue(queue.empty())
        joining = asyncio.create_task(queue.join())
        try:
            await asyncio.sleep(0)
            self.assertFalse(joining.done())
            queue.task_done(item)
            await asyncio.wait_for(joining, 1)
        finally:
            joining.cancel()
            await asyncio.gather(joining, return_exceptions=True)

    async def test_invalid_completion_preserves_active_item_and_join_state(self):
        queue = self.build()
        item = Item("a", "first")
        queue.put_nowait(item)
        self.assertIs(await queue.get(), item)
        for invalid in (Item("a", "first"), Item("b", "other")):
            with self.assertRaises(ValueError):
                queue.task_done(invalid)
        queue.task_done(item)
        with self.assertRaises(ValueError):
            queue.task_done(item)
        await asyncio.wait_for(queue.join(), 1)

    async def test_cancelled_get_leaves_conversation_ready_for_next_worker(self):
        queue = self.build()
        item, next_same = Item("a", "first"), Item("a", "next")
        queue.put_nowait(item)
        queue.put_nowait(next_same)
        self.assertIs(await queue.get(), item)
        waiting = asyncio.create_task(queue.get())
        await asyncio.sleep(0)
        waiting.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiting
        queue.task_done(item)
        self.assertIs(await asyncio.wait_for(queue.get(), 1), next_same)
        queue.task_done(next_same)
        await asyncio.wait_for(queue.join(), 1)

    async def test_cancelled_notified_get_does_not_consume_the_ready_item(self):
        queue = self.build()
        waiting = asyncio.create_task(queue.get())
        await asyncio.sleep(0)
        item = Item("a", "first")
        queue.put_nowait(item)
        waiting.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiting
        self.assertIs(await asyncio.wait_for(queue.get(), 1), item)
        queue.task_done(item)
        await asyncio.wait_for(queue.join(), 1)

    async def test_completed_conversations_release_scheduling_state(self):
        queue = self.build()
        for index in range(20):
            item = Item(str(index), "first")
            queue.put_nowait(item)
            self.assertIs(await queue.get(), item)
            queue.task_done(item)
        await asyncio.wait_for(queue.join(), 1)
        self.assertEqual(queue._pending, {})
        self.assertEqual(queue._active, {})
        self.assertTrue(queue._ready.empty())

    async def test_none_item_and_unbounded_capacity(self):
        queue = ConversationQueue(maxsize=0, key=lambda item: "same")
        for _ in range(20):
            queue.put_nowait(None)
        self.assertEqual(queue.qsize(), 20)
        for _ in range(20):
            item = await queue.get()
            self.assertIsNone(item)
            queue.task_done(item)
        await asyncio.wait_for(queue.join(), 1)
