"""Minimal async fake Redis for admin/setup tests.

Only implements the operations used by the admin and setup routers:
``get``, ``set``, ``incr``, ``expire``, ``delete``. Values are stored
as bytes to match the real ``redis.asyncio`` client's return type.
"""

from __future__ import annotations


class FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}
        self.ttls: dict[str, int] = {}

    async def get(self, key: str) -> bytes | None:
        return self.store.get(key)

    async def set(self, key: str, value: str | bytes, ex: int | None = None) -> None:
        self.store[key] = value.encode() if isinstance(value, str) else value
        if ex is not None:
            self.ttls[key] = ex

    async def incr(self, key: str) -> int:
        current = int(self.store.get(key, b"0"))
        current += 1
        self.store[key] = str(current).encode()
        return current

    async def expire(self, key: str, seconds: int) -> bool:
        if key in self.store:
            self.ttls[key] = seconds
            return True
        return False

    async def getdel(self, key: str) -> bytes | None:
        value = self.store.pop(key, None)
        self.ttls.pop(key, None)
        return value

    async def delete(self, *keys: str) -> int:
        count = 0
        for key in keys:
            if key in self.store:
                del self.store[key]
                self.ttls.pop(key, None)
                count += 1
        return count
