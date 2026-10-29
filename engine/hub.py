"""Сессии и лимиты для публичного показа.

Локально (shared) все каналы работают с одним помощником: подтвердили в Telegram, увидели на экране.
На хостинге (sessions) у каждого посетителя своё состояние, чтобы гости не мешали друг другу."""
from __future__ import annotations

import threading
import time
from collections import OrderedDict, deque
from typing import Callable, Deque, Dict, Optional, Tuple

from .service import Assistant


class Hub:
    def __init__(self, factory: Callable[[], Assistant], sessions: bool = False, max_sessions: int = 60, ttl: int = 7200):
        self._factory, self.sessions, self.max_sessions, self.ttl = factory, sessions, max_sessions, ttl
        self._guard = threading.Lock()
        self._items: "OrderedDict[str, list]" = OrderedDict()  # sid -> [assistant, lock, last_seen]
        self._shared = None

    def get(self, sid: str, init: Optional[Callable[[Assistant], None]] = None) -> Tuple[Assistant, threading.Lock]:
        with self._guard:
            if not self.sessions:
                if self._shared is None:
                    self._shared = [self._factory(), threading.Lock(), 0.0]
                    if init:
                        init(self._shared[0])
                return self._shared[0], self._shared[1]
            now = time.time()
            for key in [k for k, v in self._items.items() if now - v[2] > self.ttl]:
                del self._items[key]
            item = self._items.get(sid)
            if item is None:
                while len(self._items) >= self.max_sessions:
                    self._items.popitem(last=False)  # вытесняем самую давнюю
                item = self._items[sid] = [self._factory(), threading.Lock(), now]
                if init:
                    init(item[0])
            item[2] = now
            self._items.move_to_end(sid)
            return item[0], item[1]

    def __len__(self) -> int:
        return len(self._items)


class RateLimiter:
    """Скользящее окно: не больше `limit` запросов за `window` секунд с одного адреса."""

    def __init__(self, limit: int, window: float = 60.0):
        self.limit, self.window = limit, window
        self._hits: Dict[str, Deque[float]] = {}
        self._guard = threading.Lock()

    def allow(self, who: str, now: float = None) -> bool:
        now = time.time() if now is None else now
        with self._guard:
            q = self._hits.setdefault(who, deque())
            while q and now - q[0] > self.window:
                q.popleft()
            if len(q) >= self.limit:
                return False
            q.append(now)
            if len(self._hits) > 5000:  # не копим память на чужих адресах
                self._hits = {k: v for k, v in self._hits.items() if v and now - v[-1] <= self.window}
            return True
