"""Per-key heavy hitter tracker (Space-Saving algorithm).

Used alongside bucket-level aggregation to surface individual hot keys without
paying for full key-level cardinality. For a stream of ``N`` events with
capacity ``k``, Space-Saving guarantees any true top-k key whose frequency is
above ``N / k`` will be tracked, with an over-estimate error bounded by the
minimum counter currently in the table.

This module is intentionally dependency-free so it can be embedded in any
service layer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class KeyStat:
    """Per-key counters tracked by :class:`TopKTracker`.

    All fields are raw counters; rate/p99/etc. are derived later by the
    aggregator so the tracker itself stays O(1) per event.
    """

    key: str
    reads: int = 0
    hits: int = 0
    writes: int = 0
    total_value_size: int = 0
    rocks_latencies_us: List[float] = field(default_factory=list)
    redis_latencies_us: List[float] = field(default_factory=list)
    error: int = 0


class TopKTracker:
    """Space-Saving top-k heavy hitter tracker.

    Only ``capacity`` distinct keys are retained at any moment. When a new key
    arrives and the table is full, we evict the current minimum and inherit
    its count as the new key's error bound. This keeps memory bounded while
    still guaranteeing detection of sufficiently hot keys.
    """

    def __init__(self, capacity: int = 1024, latency_sample_cap: int = 64) -> None:
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        self._capacity = capacity
        self._latency_sample_cap = latency_sample_cap
        self._table: Dict[str, KeyStat] = {}

    def observe_read(
        self,
        key: str,
        latency_us: float,
        hit: bool,
        value_size_bytes: int,
        rocks_latency_us: Optional[float] = None,
    ) -> None:
        stat = self._touch(key)
        stat.reads += 1
        if hit:
            stat.hits += 1
        stat.total_value_size += max(value_size_bytes, 0)
        self._record_sample(stat.redis_latencies_us, latency_us)
        if rocks_latency_us is not None:
            self._record_sample(stat.rocks_latencies_us, rocks_latency_us)

    def observe_write(self, key: str, value_size_bytes: int) -> None:
        stat = self._touch(key)
        stat.writes += 1
        stat.total_value_size += max(value_size_bytes, 0)

    def snapshot_and_reset(self) -> List[KeyStat]:
        snapshot = list(self._table.values())
        self._table = {}
        return snapshot

    def _touch(self, key: str) -> KeyStat:
        stat = self._table.get(key)
        if stat is not None:
            return stat

        if len(self._table) < self._capacity:
            stat = KeyStat(key=key)
            self._table[key] = stat
            return stat

        victim_key = self._find_min_key()
        victim = self._table.pop(victim_key)
        inherited = victim.reads + victim.writes
        stat = KeyStat(key=key, error=inherited)
        self._table[key] = stat
        return stat

    def _find_min_key(self) -> str:
        min_key = next(iter(self._table))
        min_count = self._table[min_key].reads + self._table[min_key].writes
        for k, v in self._table.items():
            total = v.reads + v.writes
            if total < min_count:
                min_key = k
                min_count = total
        return min_key

    def _record_sample(self, bucket: List[float], value: float) -> None:
        if len(bucket) < self._latency_sample_cap:
            bucket.append(value)
        else:
            bucket[len(bucket) % self._latency_sample_cap] = value
