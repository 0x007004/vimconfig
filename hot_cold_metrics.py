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
from typing import Dict, Iterable, List, Optional, Tuple

from hot_cold_probe import KeyMetrics, Tier
from hot_cold_topk import KeyStat, TopKTracker


@dataclass
class ReadEvent:
    bucket_key: str
    latency_us: float
    hit: bool
    value_size_bytes: int
    rocks_latency_us: Optional[float] = None
    full_key: Optional[str] = None


@dataclass
class WriteEvent:
    bucket_key: str
    value_size_bytes: int
    full_key: Optional[str] = None


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

    The collector is cheap: O(1) per event. It keeps two parallel views:

    * bucket-level state (``_buckets``) covering the whole key space,
    * a per-key heavy-hitter tracker (``_topk``) with bounded memory that
      surfaces individual hot keys living inside those buckets.

    Heavy-hitter tracking is optional: pass ``topk=None`` to disable it.
    """

    def __init__(self, sample_cap: int = 512, topk: Optional[TopKTracker] = None) -> None:
        self._buckets: Dict[str, _BucketState] = {}
        self._sample_cap = sample_cap
        self._topk = topk
        self._key_to_bucket: Dict[str, str] = {}

    def record_read(self, event: ReadEvent) -> None:
        state = self._buckets.setdefault(event.bucket_key, _BucketState())
        state.reads += 1
        if event.hit:
            state.hits += 1
        state.total_value_size += max(event.value_size_bytes, 0)
        self._record_sample(state.redis_latencies_us, event.latency_us)
        if event.rocks_latency_us is not None:
            self._record_sample(state.rocks_latencies_us, event.rocks_latency_us)

        if self._topk is not None and event.full_key is not None:
            self._key_to_bucket[event.full_key] = event.bucket_key
            self._topk.observe_read(
                key=event.full_key,
                latency_us=event.latency_us,
                hit=event.hit,
                value_size_bytes=event.value_size_bytes,
                rocks_latency_us=event.rocks_latency_us,
            )

    def record_write(self, event: WriteEvent) -> None:
        state = self._buckets.setdefault(event.bucket_key, _BucketState())
        state.writes += 1
        state.total_value_size += max(event.value_size_bytes, 0)

        if self._topk is not None and event.full_key is not None:
            self._key_to_bucket[event.full_key] = event.bucket_key
            self._topk.observe_write(key=event.full_key, value_size_bytes=event.value_size_bytes)

    def set_tier(self, bucket_key: str, tier: Tier) -> None:
        state = self._buckets.setdefault(bucket_key, _BucketState())
        state.current_tier = tier

    def _record_sample(self, bucket: List[float], value: float) -> None:
        if len(bucket) < self._sample_cap:
            bucket.append(value)
        else:
            # Reservoir-ish: overwrite oldest slot to keep bounded memory.
            bucket[len(bucket) % self._sample_cap] = value

    def snapshot_and_reset(self) -> Tuple[Dict[str, _BucketState], List[KeyStat], Dict[str, str]]:
        bucket_snapshot = self._buckets
        self._buckets = {}
        topk_snapshot: List[KeyStat] = []
        if self._topk is not None:
            topk_snapshot = self._topk.snapshot_and_reset()
        key_to_bucket = self._key_to_bucket
        self._key_to_bucket = {}
        return bucket_snapshot, topk_snapshot, key_to_bucket


@dataclass
class AggregatedSamples:
    """Aggregator output: bucket-level and key-level views for one window."""

    bucket_samples: List[KeyMetrics]
    key_samples: List[KeyMetrics]
    skew_by_bucket: Dict[str, float]
    key_to_bucket: Dict[str, str]


class WindowAggregator:
    """Turns a raw collector snapshot into ``KeyMetrics`` samples.

    The aggregator keeps EWMA state across windows so promotion/demotion
    decisions are based on smoothed load rather than per-minute spikes.

    It produces two views:

    * **bucket_samples**: coarse, covers the whole key space.
    * **key_samples**: fine, only for heavy hitters tracked by ``TopKTracker``.

    Skew index (``Top-K reads / bucket reads``) tells the planner whether a
    bucket's QPS is dominated by a handful of keys. High skew means prefer
    per-key promotion; low skew means prefer bucket-level promotion.
    """

    def __init__(self, ewma_alpha: float = 0.4, window_seconds: float = 60.0) -> None:
        if not 0.0 < ewma_alpha <= 1.0:
            raise ValueError("ewma_alpha must be in (0, 1]")
        self._ewma_alpha = ewma_alpha
        self._window_seconds = window_seconds
        self._qps_ewma: Dict[str, float] = {}
        self._write_churn_ewma: Dict[str, float] = {}
        self._key_qps_ewma: Dict[str, float] = {}
        self._key_churn_ewma: Dict[str, float] = {}

    def build_samples(
        self,
        bucket_snapshot: Dict[str, _BucketState],
        topk_snapshot: Optional[List[KeyStat]] = None,
        key_to_bucket: Optional[Dict[str, str]] = None,
    ) -> AggregatedSamples:
        bucket_samples = self._build_bucket_samples(bucket_snapshot)
        key_samples, skew = self._build_key_samples(
            topk_snapshot or [],
            key_to_bucket or {},
            bucket_snapshot,
        )
        return AggregatedSamples(
            bucket_samples=bucket_samples,
            key_samples=key_samples,
            skew_by_bucket=skew,
            key_to_bucket=key_to_bucket or {},
        )

    def _build_bucket_samples(self, snapshot: Dict[str, _BucketState]) -> List[KeyMetrics]:
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

    def _build_key_samples(
        self,
        topk_snapshot: List[KeyStat],
        key_to_bucket: Dict[str, str],
        bucket_snapshot: Dict[str, _BucketState],
    ) -> Tuple[List[KeyMetrics], Dict[str, float]]:
        samples: List[KeyMetrics] = []
        reads_by_bucket_from_topk: Dict[str, int] = {}

        for stat in topk_snapshot:
            bucket_key = key_to_bucket.get(stat.key, "")
            reads_by_bucket_from_topk[bucket_key] = (
                reads_by_bucket_from_topk.get(bucket_key, 0) + stat.reads
            )

            qps = stat.reads / self._window_seconds
            writes_per_sec = stat.writes / self._window_seconds

            prev_qps = self._key_qps_ewma.get(stat.key, qps)
            prev_churn = self._key_churn_ewma.get(stat.key, writes_per_sec)
            qps_ewma = self._ewma(prev_qps, qps)
            churn_ewma = self._ewma(prev_churn, writes_per_sec)
            self._key_qps_ewma[stat.key] = qps_ewma
            self._key_churn_ewma[stat.key] = churn_ewma

            total_ops = max(stat.reads + stat.writes, 1)
            avg_size = int(stat.total_value_size / total_ops) if stat.total_value_size else 1
            miss_rate = 1.0 - (stat.hits / stat.reads if stat.reads else 0.0)

            bucket_state = bucket_snapshot.get(bucket_key)
            tier = bucket_state.current_tier if bucket_state is not None else Tier.ROCKS

            samples.append(
                KeyMetrics(
                    key=stat.key,
                    qps_ewma=qps_ewma,
                    p99_rocks_ms=_percentile_ms(stat.rocks_latencies_us, 99.0),
                    p99_redis_ms=_percentile_ms(stat.redis_latencies_us, 99.0),
                    miss_penalty=1.0 + miss_rate,
                    value_size_bytes=max(avg_size, 1),
                    write_churn=churn_ewma,
                    tier=tier,
                )
            )

        skew: Dict[str, float] = {}
        for bucket_key, state in bucket_snapshot.items():
            topk_reads = reads_by_bucket_from_topk.get(bucket_key, 0)
            skew[bucket_key] = topk_reads / state.reads if state.reads else 0.0
        return samples, skew

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
