"""Storage abstraction layer for the hot/cold PoC.

The real deployment plugs in concrete Redis / RocksDB clients. For the PoC we
ship in-memory fakes that are deterministic and cheap to use in tests.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Dict, Optional, Protocol, Tuple


class KVStore(Protocol):
    name: str

    def get(self, key: str) -> Tuple[Optional[bytes], float]:
        """Return ``(value, latency_seconds)``. ``value`` is ``None`` on miss."""

    def set(self, key: str, value: bytes) -> float:
        ...

    def delete(self, key: str) -> float:
        ...


@dataclass
class FakeStoreConfig:
    name: str
    read_latency_us: float
    write_latency_us: float
    jitter_us: float = 0.0
    tp99_spike_multiplier: float = 1.0


class InMemoryKVStore:
    """Deterministic fake store used by the simulation demo."""

    def __init__(self, config: FakeStoreConfig) -> None:
        self.name = config.name
        self._config = config
        self._data: Dict[str, bytes] = {}
        self._tick = 0

    def get(self, key: str) -> Tuple[Optional[bytes], float]:
        latency = self._latency(self._config.read_latency_us)
        return self._data.get(key), latency

    def set(self, key: str, value: bytes) -> float:
        self._data[key] = value
        return self._latency(self._config.write_latency_us)

    def delete(self, key: str) -> float:
        self._data.pop(key, None)
        return self._latency(self._config.write_latency_us)

    def exists(self, key: str) -> bool:
        return key in self._data

    def _latency(self, base_us: float) -> float:
        self._tick += 1
        noise = (self._tick % 7) * self._config.jitter_us / 7.0
        spike = self._config.tp99_spike_multiplier if self._tick % 100 == 0 else 1.0
        return (base_us + noise) * spike / 1_000_000.0


class TieredCache:
    """Cache-aside facade that exposes Redis as cache and Rocks as source of truth.

    Writes go to Rocks and invalidate Redis. Reads try Redis first; on miss, we
    fall back to Rocks and opportunistically refill Redis for placement keys.
    """

    def __init__(
        self,
        redis: InMemoryKVStore,
        rocks: InMemoryKVStore,
        placement: Optional[Dict[str, bool]] = None,
    ) -> None:
        self.redis = redis
        self.rocks = rocks
        self._placement = placement if placement is not None else {}

    def mark_promoted(self, bucket_key: str) -> None:
        self._placement[bucket_key] = True

    def mark_demoted(self, bucket_key: str) -> None:
        self._placement[bucket_key] = False

    def is_promoted(self, bucket_key: str) -> bool:
        return self._placement.get(bucket_key, False)

    def get(self, bucket_key: str, full_key: str) -> Tuple[Optional[bytes], float, float, bool]:
        """Return ``(value, redis_latency_s, rocks_latency_s_or_zero, hit)``."""

        value, redis_latency = self.redis.get(full_key)
        if value is not None:
            return value, redis_latency, 0.0, True

        value, rocks_latency = self.rocks.get(full_key)
        if value is not None and self.is_promoted(bucket_key):
            self.redis.set(full_key, value)
        return value, redis_latency, rocks_latency, False

    def set(self, bucket_key: str, full_key: str, value: bytes) -> None:
        self.rocks.set(full_key, value)
        if self.is_promoted(bucket_key):
            self.redis.set(full_key, value)
        else:
            self.redis.delete(full_key)


def monotonic_time() -> float:
    return time.monotonic()
