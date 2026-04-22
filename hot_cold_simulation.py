"""End-to-end simulation wiring collector, planner, and executor.

This is a deterministic toy workload intended to show the full PoC loop. It is
also reused by the tests to validate the integration.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Dict, List, Tuple

from hot_cold_executor import ExecutorConfig, ExecutorMode, MigrationExecutor
from hot_cold_hybrid import HybridConfig, HybridPlanner
from hot_cold_metrics import MetricsCollector, ReadEvent, WindowAggregator, WriteEvent
from hot_cold_probe import HotColdStrategyEngine, StrategyConfig, Tier
from hot_cold_storage import FakeStoreConfig, InMemoryKVStore, TieredCache
from hot_cold_topk import TopKTracker


@dataclass
class SimulationSummary:
    before_p99_ms: float
    after_p99_ms: float
    bucket_promoted: int
    key_promoted: int
    bytes_moved: int
    hit_rate_after: float
    skew_by_bucket: Dict[str, float]


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
        "catalog:item": [f"catalog:item:{i}" for i in range(40)],
        "audit:log": [f"audit:log:{i}" for i in range(20)],
    }
    read_weights = {
        "user:profile": 0.55,
        "catalog:item": 0.40,
        "audit:log": 0.05,
    }
    value_sizes = {
        "user:profile": 256,
        "catalog:item": 512,
        "audit:log": 4096,
    }
    # Hot keys concentrated inside catalog:item, simulating a skewed bucket.
    hot_keys_per_bucket: Dict[str, List[str]] = {
        "catalog:item": [f"catalog:item:{i}" for i in range(2)],
    }

    for bucket, keys in buckets.items():
        payload = b"x" * value_sizes[bucket]
        for k in keys:
            rocks.set(k, payload)

    collector = MetricsCollector(topk=TopKTracker(capacity=64))
    aggregator = WindowAggregator(window_seconds=30.0, ewma_alpha=0.6)
    planner = HybridPlanner(
        bucket_engine=HotColdStrategyEngine(
            StrategyConfig(
                hot_cycles_required=1,
                cold_cycles_required=1,
                promote_score_threshold=0.0005,
                demote_score_threshold=0.00005,
                min_qps_for_promotion=1.0,
                redis_promotion_budget_bytes=512 * 1024,
                migration_network_budget_bytes=2 * 1024 * 1024,
            )
        ),
        key_engine=HotColdStrategyEngine(
            StrategyConfig(
                hot_cycles_required=1,
                cold_cycles_required=1,
                promote_score_threshold=0.0001,
                demote_score_threshold=0.00001,
                min_qps_for_promotion=0.5,
                redis_promotion_budget_bytes=256 * 1024,
                migration_network_budget_bytes=1 * 1024 * 1024,
            )
        ),
        config=HybridConfig(high_skew_threshold=0.4, low_skew_threshold=0.1, key_top_n_per_bucket=8),
    )
    executor = MigrationExecutor(
        cache=cache,
        config=ExecutorConfig(
            mode=ExecutorMode.PROMOTE_ONLY,
            max_bytes_per_run=1024 * 1024,
            max_ops_per_run=50,
        ),
    )

    latency_before = _run_workload(
        cache, collector, buckets, read_weights, hot_keys_per_bucket, rng, requests=600, tier=Tier.ROCKS
    )

    bucket_snapshot, topk_snapshot, key_to_bucket = collector.snapshot_and_reset()
    samples = aggregator.build_samples(bucket_snapshot, topk_snapshot, key_to_bucket)
    plan = planner.plan(samples, redis_free_bytes=512 * 1024)
    execution = executor.apply_hybrid(plan, bucket_keys=buckets)

    latency_after, hits, total = _measure_workload(
        cache, buckets, read_weights, hot_keys_per_bucket, rng, requests=600
    )

    return SimulationSummary(
        before_p99_ms=_percentile(latency_before, 99.0) * 1000.0,
        after_p99_ms=_percentile(latency_after, 99.0) * 1000.0,
        bucket_promoted=len(plan.bucket_actions),
        key_promoted=len([a for a in plan.key_actions if a.action == "promote"]),
        bytes_moved=execution.bytes_moved,
        hit_rate_after=hits / total if total else 0.0,
        skew_by_bucket=dict(samples.skew_by_bucket),
    )


def _choose_full_key(
    bucket: str,
    buckets: Dict[str, List[str]],
    hot_keys_per_bucket: Dict[str, List[str]],
    rng: random.Random,
    hot_key_bias: float = 0.8,
) -> str:
    hot = hot_keys_per_bucket.get(bucket)
    if hot and rng.random() < hot_key_bias:
        return rng.choice(hot)
    return rng.choice(buckets[bucket])


def _run_workload(
    cache: TieredCache,
    collector: MetricsCollector,
    buckets: Dict[str, List[str]],
    read_weights: Dict[str, float],
    hot_keys_per_bucket: Dict[str, List[str]],
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
        full_key = _choose_full_key(bucket, buckets, hot_keys_per_bucket, rng)
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
                full_key=full_key,
            )
        )
        if rng.random() < 0.05:
            collector.record_write(
                WriteEvent(
                    bucket_key=bucket,
                    value_size_bytes=len(value) if value else 0,
                    full_key=full_key,
                )
            )
    return latencies


def _measure_workload(
    cache: TieredCache,
    buckets: Dict[str, List[str]],
    read_weights: Dict[str, float],
    hot_keys_per_bucket: Dict[str, List[str]],
    rng: random.Random,
    requests: int,
) -> Tuple[List[float], int, int]:
    latencies: List[float] = []
    hits = 0

    bucket_list = list(read_weights.keys())
    weights = [read_weights[b] for b in bucket_list]

    for _ in range(requests):
        bucket = rng.choices(bucket_list, weights=weights, k=1)[0]
        full_key = _choose_full_key(bucket, buckets, hot_keys_per_bucket, rng)
        _, redis_latency, rocks_latency, hit = cache.get(bucket, full_key)
        if hit:
            hits += 1
        latencies.append(redis_latency + rocks_latency)
    return latencies, hits, requests


if __name__ == "__main__":
    summary = run_simulation()
    print(
        f"bucket_promoted={summary.bucket_promoted} key_promoted={summary.key_promoted} "
        f"bytes_moved={summary.bytes_moved}"
    )
    print(f"p99_before={summary.before_p99_ms:.3f}ms p99_after={summary.after_p99_ms:.3f}ms")
    print(f"hit_rate_after={summary.hit_rate_after:.2%}")
    for bucket, skew in summary.skew_by_bucket.items():
        print(f"skew[{bucket}]={skew:.2%}")
