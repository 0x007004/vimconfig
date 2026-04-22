"""Event collection and window aggregation for the hot/cold PoC.

The collector is designed to be embedded in service read/write paths. It only
keeps aggregated counters per ``(bucket_key,)``, so it is safe to run with a
large number of underlying keys.

In production ``bucket_key`` is typically derived from the real key (prefix,
tenant, or hash-bucket). This keeps cardinality bounded while still being
actionable for promote/demote decisions.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional

from hot_cold_probe import KeyMetrics, Tier


@dataclass
class ReadEvent:
    bucket_key: str
    latency_us: float
    hit: bool
    value_size_bytes: int
    rocks_latency_us: Optional[float] = None


@dataclass
class WriteEvent:
    bucket_key: str
    value_size_bytes: int


@dataclass
class _BucketState:
    reads: int = 0
    hits: int = 0
    writes: int = 0
    total_value_size: int = 0
    redis_latencies_us: List[float] = field(default_factory=list)
    rocks_latencies_us: List[float] = field(default_factory=list)
    current_tier: Tier = Tier.ROCKS


class MetricsCollector:
    """Thread-unsafe by design; callers wrap with whatever sync they need.

    The collector is cheap: O(1) per event, with bounded memory per bucket
    because latency samples are downsampled to ``sample_cap``.
    """

    def __init__(self, sample_cap: int = 512) -> None:
        self._buckets: Dict[str, _BucketState] = {}
        self._sample_cap = sample_cap

    def record_read(self, event: ReadEvent) -> None:
        state = self._buckets.setdefault(event.bucket_key, _BucketState())
        state.reads += 1
        if event.hit:
            state.hits += 1
        state.total_value_size += max(event.value_size_bytes, 0)
        self._record_sample(state.redis_latencies_us, event.latency_us)
        if event.rocks_latency_us is not None:
            self._record_sample(state.rocks_latencies_us, event.rocks_latency_us)

    def record_write(self, event: WriteEvent) -> None:
        state = self._buckets.setdefault(event.bucket_key, _BucketState())
        state.writes += 1
        state.total_value_size += max(event.value_size_bytes, 0)

    def set_tier(self, bucket_key: str, tier: Tier) -> None:
        state = self._buckets.setdefault(bucket_key, _BucketState())
        state.current_tier = tier

    def _record_sample(self, bucket: List[float], value: float) -> None:
        if len(bucket) < self._sample_cap:
            bucket.append(value)
        else:
            # Reservoir-ish: overwrite oldest slot to keep bounded memory.
            bucket[len(bucket) % self._sample_cap] = value

    def snapshot_and_reset(self) -> Dict[str, _BucketState]:
        snapshot = self._buckets
        self._buckets = {}
        return snapshot


class WindowAggregator:
    """Turns a raw collector snapshot into ``KeyMetrics`` samples.

    The aggregator keeps EWMA state across windows so promotion/demotion
    decisions are based on smoothed load rather than per-minute spikes.
    """

    def __init__(self, ewma_alpha: float = 0.4, window_seconds: float = 60.0) -> None:
        if not 0.0 < ewma_alpha <= 1.0:
            raise ValueError("ewma_alpha must be in (0, 1]")
        self._ewma_alpha = ewma_alpha
        self._window_seconds = window_seconds
        self._qps_ewma: Dict[str, float] = {}
        self._write_churn_ewma: Dict[str, float] = {}

    def build_samples(self, snapshot: Dict[str, _BucketState]) -> List[KeyMetrics]:
        samples: List[KeyMetrics] = []
        for bucket_key, state in snapshot.items():
            qps = state.reads / self._window_seconds
            writes_per_sec = state.writes / self._window_seconds

            prev_qps = self._qps_ewma.get(bucket_key, qps)
            prev_churn = self._write_churn_ewma.get(bucket_key, writes_per_sec)

            qps_ewma = self._ewma(prev_qps, qps)
            churn_ewma = self._ewma(prev_churn, writes_per_sec)

            self._qps_ewma[bucket_key] = qps_ewma
            self._write_churn_ewma[bucket_key] = churn_ewma

            total_ops = max(state.reads + state.writes, 1)
            avg_size = int(state.total_value_size / total_ops) if state.total_value_size else 1
            miss_rate = 1.0 - (state.hits / state.reads if state.reads else 0.0)

            samples.append(
                KeyMetrics(
                    key=bucket_key,
                    qps_ewma=qps_ewma,
                    p99_rocks_ms=_percentile_ms(state.rocks_latencies_us, 99.0),
                    p99_redis_ms=_percentile_ms(state.redis_latencies_us, 99.0),
                    miss_penalty=1.0 + miss_rate,
                    value_size_bytes=max(avg_size, 1),
                    write_churn=churn_ewma,
                    tier=state.current_tier,
                )
            )
        return samples

    def _ewma(self, previous: float, current: float) -> float:
        return self._ewma_alpha * current + (1.0 - self._ewma_alpha) * previous


def _percentile_ms(samples: Iterable[float], percentile: float) -> float:
    data = sorted(samples)
    if not data:
        return 0.0
    if percentile <= 0.0:
        return data[0] / 1000.0
    if percentile >= 100.0:
        return data[-1] / 1000.0
    rank = (percentile / 100.0) * (len(data) - 1)
    low = int(math.floor(rank))
    high = int(math.ceil(rank))
    if low == high:
        return data[low] / 1000.0
    fraction = rank - low
    interpolated = data[low] + (data[high] - data[low]) * fraction
    return interpolated / 1000.0
