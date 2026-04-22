"""End-to-end simulation wiring collector, planner, and executor.

This is a deterministic toy workload intended to show the full PoC loop. It is
also reused by the tests to validate the integration.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Dict, List, Tuple

from hot_cold_executor import ExecutorConfig, ExecutorMode, MigrationExecutor
from hot_cold_metrics import MetricsCollector, ReadEvent, WindowAggregator, WriteEvent
from hot_cold_probe import HotColdStrategyEngine, StrategyConfig, Tier
from hot_cold_storage import FakeStoreConfig, InMemoryKVStore, TieredCache


@dataclass
class SimulationSummary:
    before_p99_ms: float
    after_p99_ms: float
    promoted: int
    demoted: int
    bytes_moved: int
    hit_rate_after: float


def _percentile(samples: List[float], percentile: float) -> float:
    if not samples:
        return 0.0
    data = sorted(samples)
    rank = int(percentile / 100.0 * (len(data) - 1))
    return data[rank]


def run_simulation(seed: int = 7) -> SimulationSummary:
    rng = random.Random(seed)

    redis = InMemoryKVStore(FakeStoreConfig(name="redis", read_latency_us=200, write_latency_us=200))
    rocks = InMemoryKVStore(
        FakeStoreConfig(
            name="rocks",
            read_latency_us=3_000,
            write_latency_us=4_000,
            jitter_us=1_500,
            tp99_spike_multiplier=5.0,
        )
    )
    cache = TieredCache(redis=redis, rocks=rocks)

    buckets = {
        "user:profile": [f"user:profile:{i}" for i in range(20)],
        "catalog:item": [f"catalog:item:{i}" for i in range(20)],
        "audit:log": [f"audit:log:{i}" for i in range(20)],
    }
    read_weights = {
        "user:profile": 0.70,
        "catalog:item": 0.25,
        "audit:log": 0.05,
    }
    value_sizes = {
        "user:profile": 256,
        "catalog:item": 512,
        "audit:log": 4096,
    }

    for bucket, keys in buckets.items():
        payload = b"x" * value_sizes[bucket]
        for k in keys:
            rocks.set(k, payload)

    collector = MetricsCollector()
    aggregator = WindowAggregator(window_seconds=30.0, ewma_alpha=0.6)
    engine = HotColdStrategyEngine(
        StrategyConfig(
            hot_cycles_required=1,
            cold_cycles_required=1,
            promote_score_threshold=0.0005,
            demote_score_threshold=0.00005,
            min_qps_for_promotion=1.0,
            redis_promotion_budget_bytes=512 * 1024,
            migration_network_budget_bytes=2 * 1024 * 1024,
        )
    )
    executor = MigrationExecutor(
        cache=cache,
        config=ExecutorConfig(
            mode=ExecutorMode.PROMOTE_ONLY,
            max_bytes_per_run=1024 * 1024,
            max_ops_per_run=50,
        ),
    )

    latency_before = _run_workload(cache, collector, buckets, read_weights, rng, requests=600, tier=Tier.ROCKS)

    snapshot = collector.snapshot_and_reset()
    samples = aggregator.build_samples(snapshot)
    plan = engine.plan_cycle(samples, redis_free_bytes=512 * 1024)
    execution = executor.apply(plan, bucket_keys=buckets)

    latency_after, hits, total = _measure_workload(cache, buckets, read_weights, rng, requests=600)

    return SimulationSummary(
        before_p99_ms=_percentile(latency_before, 99.0) * 1000.0,
        after_p99_ms=_percentile(latency_after, 99.0) * 1000.0,
        promoted=plan.promoted,
        demoted=plan.demoted,
        bytes_moved=execution.bytes_moved,
        hit_rate_after=hits / total if total else 0.0,
    )


def _run_workload(
    cache: TieredCache,
    collector: MetricsCollector,
    buckets: Dict[str, List[str]],
    read_weights: Dict[str, float],
    rng: random.Random,
    requests: int,
    tier: Tier,
) -> List[float]:
    latencies: List[float] = []
    for bucket_key in buckets:
        collector.set_tier(bucket_key, tier)

    bucket_list = list(read_weights.keys())
    weights = [read_weights[b] for b in bucket_list]

    for _ in range(requests):
        bucket = rng.choices(bucket_list, weights=weights, k=1)[0]
        full_key = rng.choice(buckets[bucket])
        value, redis_latency, rocks_latency, hit = cache.get(bucket, full_key)
        total_latency = redis_latency + rocks_latency
        latencies.append(total_latency)
        collector.record_read(
            ReadEvent(
                bucket_key=bucket,
                latency_us=redis_latency * 1_000_000,
                hit=hit,
                value_size_bytes=len(value) if value else 0,
                rocks_latency_us=(rocks_latency * 1_000_000) if not hit else None,
            )
        )
        if rng.random() < 0.05:
            collector.record_write(
                WriteEvent(bucket_key=bucket, value_size_bytes=len(value) if value else 0)
            )
    return latencies


def _measure_workload(
    cache: TieredCache,
    buckets: Dict[str, List[str]],
    read_weights: Dict[str, float],
    rng: random.Random,
    requests: int,
) -> Tuple[List[float], int, int]:
    latencies: List[float] = []
    hits = 0

    bucket_list = list(read_weights.keys())
    weights = [read_weights[b] for b in bucket_list]

    for _ in range(requests):
        bucket = rng.choices(bucket_list, weights=weights, k=1)[0]
        full_key = rng.choice(buckets[bucket])
        _, redis_latency, rocks_latency, hit = cache.get(bucket, full_key)
        if hit:
            hits += 1
        latencies.append(redis_latency + rocks_latency)
    return latencies, hits, requests


if __name__ == "__main__":
    summary = run_simulation()
    print(f"promoted={summary.promoted} demoted={summary.demoted} bytes_moved={summary.bytes_moved}")
    print(f"p99_before={summary.before_p99_ms:.3f}ms p99_after={summary.after_p99_ms:.3f}ms")
    print(f"hit_rate_after={summary.hit_rate_after:.2%}")
