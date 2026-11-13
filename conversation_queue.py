"""Bounded conversation scheduling with one active item per conversation."""
from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable, Hashable
from typing import Generic, TypeVar

T = TypeVar("T")
_MISSING = object()


class ConversationQueue(Generic[T]):
    """Keep each conversation ordered while allowing other conversations to run."""

    def __init__(self, *, maxsize: int = 8, key: Callable[[T], Hashable]):
        self.maxsize = maxsize
        self._key = key
        self._pending: dict[Hashable, deque[T]] = {}
        self._active: dict[Hashable, T] = {}
        self._ready: asyncio.Queue[Hashable] = asyncio.Queue()
        self._pending_count = 0
        self._unfinished_count = 0
        self._finished = asyncio.Event()
        self._finished.set()

    def put_nowait(self, item: T) -> None:
        if self.maxsize > 0 and self._pending_count >= self.maxsize:
            raise asyncio.QueueFull
        conversation = self._key(item)
        pending = self._pending.setdefault(conversation, deque())
        pending.append(item)
        self._pending_count += 1
        self._unfinished_count += 1
        self._finished.clear()
        if len(pending) == 1 and conversation not in self._active:
            self._ready.put_nowait(conversation)

    async def get(self) -> T:
        conversation = await self._ready.get()
        self._ready.task_done()
        pending = self._pending[conversation]
        item = pending.popleft()
        if not pending:
            del self._pending[conversation]
        self._active[conversation] = item
        self._pending_count -= 1
        return item

    def task_done(self, item: T) -> None:
        conversation = self._key(item)
        if self._active.get(conversation, _MISSING) is not item:
            raise ValueError("task_done() requires the active conversation item")
        del self._active[conversation]
        if conversation in self._pending:
            self._ready.put_nowait(conversation)
        self._unfinished_count -= 1
        if self._unfinished_count == 0:
            self._finished.set()

    def qsize(self) -> int:
        return self._pending_count

    def empty(self) -> bool:
        return self._pending_count == 0

    async def join(self) -> None:
        await self._finished.wait()
