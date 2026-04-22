import unittest

from hot_cold_executor import ExecutorConfig, ExecutorMode, MigrationExecutor
from hot_cold_hybrid import HybridConfig, HybridPlanner
from hot_cold_metrics import MetricsCollector, ReadEvent, WindowAggregator, WriteEvent
from hot_cold_probe import HotColdStrategyEngine, StrategyConfig, Tier
from hot_cold_storage import FakeStoreConfig, InMemoryKVStore, TieredCache
from hot_cold_topk import TopKTracker


class TopKTrackerTests(unittest.TestCase):
    def test_capacity_is_respected_and_hot_keys_are_kept(self) -> None:
        tracker = TopKTracker(capacity=3)
        for _ in range(20):
            tracker.observe_read("hot", latency_us=100, hit=True, value_size_bytes=10)
        for i in range(10):
            tracker.observe_read(f"cold{i}", latency_us=100, hit=True, value_size_bytes=10)

        stats = {s.key: s for s in tracker.snapshot_and_reset()}
        self.assertIn("hot", stats)
        self.assertEqual(stats["hot"].reads, 20)
        self.assertLessEqual(len(stats), 3)

    def test_snapshot_clears_state(self) -> None:
        tracker = TopKTracker(capacity=2)
        tracker.observe_write("k", value_size_bytes=5)
        self.assertEqual(len(tracker.snapshot_and_reset()), 1)
        self.assertEqual(len(tracker.snapshot_and_reset()), 0)


class HybridPipelineTests(unittest.TestCase):
    def _build_collector(self) -> MetricsCollector:
        return MetricsCollector(topk=TopKTracker(capacity=16))

    def test_skew_index_detects_hot_key_inside_bucket(self) -> None:
        collector = self._build_collector()
        for _ in range(90):
            collector.record_read(
                ReadEvent(
                    bucket_key="catalog",
                    latency_us=200,
                    hit=False,
                    value_size_bytes=256,
                    rocks_latency_us=6000,
                    full_key="catalog:hot",
                )
            )
        for i in range(10):
            collector.record_read(
                ReadEvent(
                    bucket_key="catalog",
                    latency_us=200,
                    hit=False,
                    value_size_bytes=256,
                    rocks_latency_us=6000,
                    full_key=f"catalog:cold{i}",
                )
            )

        bucket_snapshot, topk_snapshot, key_to_bucket = collector.snapshot_and_reset()
        aggregator = WindowAggregator(window_seconds=60.0, ewma_alpha=1.0)
        samples = aggregator.build_samples(bucket_snapshot, topk_snapshot, key_to_bucket)

        self.assertIn("catalog", samples.skew_by_bucket)
        self.assertGreater(samples.skew_by_bucket["catalog"], 0.8)
        self.assertTrue(any(s.key == "catalog:hot" for s in samples.key_samples))

    def test_hybrid_planner_prefers_key_promote_for_skewed_bucket(self) -> None:
        collector = self._build_collector()
        for _ in range(200):
            collector.record_read(
                ReadEvent(
                    bucket_key="catalog",
                    latency_us=200,
                    hit=False,
                    value_size_bytes=256,
                    rocks_latency_us=8000,
                    full_key="catalog:hot",
                )
            )
        for i in range(30):
            collector.record_read(
                ReadEvent(
                    bucket_key="catalog",
                    latency_us=200,
                    hit=False,
                    value_size_bytes=256,
                    rocks_latency_us=8000,
                    full_key=f"catalog:cold{i}",
                )
            )

        bucket_snapshot, topk_snapshot, key_to_bucket = collector.snapshot_and_reset()
        aggregator = WindowAggregator(window_seconds=60.0, ewma_alpha=1.0)
        samples = aggregator.build_samples(bucket_snapshot, topk_snapshot, key_to_bucket)

        planner = HybridPlanner(
            bucket_engine=HotColdStrategyEngine(
                StrategyConfig(
                    hot_cycles_required=1,
                    cold_cycles_required=1,
                    promote_score_threshold=10.0,
                    demote_score_threshold=0.0,
                    min_qps_for_promotion=10_000,
                    redis_promotion_budget_bytes=1024 * 1024,
                    migration_network_budget_bytes=1024 * 1024,
                )
            ),
            key_engine=HotColdStrategyEngine(
                StrategyConfig(
                    hot_cycles_required=1,
                    cold_cycles_required=1,
                    promote_score_threshold=0.0,
                    demote_score_threshold=0.0,
                    min_qps_for_promotion=0.1,
                    redis_promotion_budget_bytes=1024 * 1024,
                    migration_network_budget_bytes=1024 * 1024,
                )
            ),
            config=HybridConfig(high_skew_threshold=0.4, low_skew_threshold=0.1, key_top_n_per_bucket=4),
        )

        plan = planner.plan(samples, redis_free_bytes=1024 * 1024)

        self.assertEqual(plan.bucket_actions, [])
        self.assertTrue(any(a.key == "catalog:hot" and a.action == "promote" for a in plan.key_actions))

    def test_executor_applies_key_level_promote(self) -> None:
        redis = InMemoryKVStore(FakeStoreConfig(name="redis", read_latency_us=100, write_latency_us=100))
        rocks = InMemoryKVStore(FakeStoreConfig(name="rocks", read_latency_us=1000, write_latency_us=1500))
        rocks.set("catalog:hot", b"payload")
        rocks.set("catalog:cold0", b"payload")
        cache = TieredCache(redis=redis, rocks=rocks)

        collector = self._build_collector()
        for _ in range(300):
            collector.record_read(
                ReadEvent(
                    bucket_key="catalog",
                    latency_us=200,
                    hit=False,
                    value_size_bytes=len(b"payload"),
                    rocks_latency_us=9000,
                    full_key="catalog:hot",
                )
            )
        for _ in range(20):
            collector.record_read(
                ReadEvent(
                    bucket_key="catalog",
                    latency_us=200,
                    hit=False,
                    value_size_bytes=len(b"payload"),
                    rocks_latency_us=9000,
                    full_key="catalog:cold0",
                )
            )

        bucket_snapshot, topk_snapshot, key_to_bucket = collector.snapshot_and_reset()
        aggregator = WindowAggregator(window_seconds=60.0, ewma_alpha=1.0)
        samples = aggregator.build_samples(bucket_snapshot, topk_snapshot, key_to_bucket)

        planner = HybridPlanner(
            bucket_engine=HotColdStrategyEngine(
                StrategyConfig(
                    hot_cycles_required=1,
                    cold_cycles_required=1,
                    promote_score_threshold=10.0,
                    demote_score_threshold=0.0,
                    min_qps_for_promotion=10_000,
                )
            ),
            key_engine=HotColdStrategyEngine(
                StrategyConfig(
                    hot_cycles_required=1,
                    cold_cycles_required=1,
                    promote_score_threshold=0.0,
                    demote_score_threshold=0.0,
                    min_qps_for_promotion=3.0,
                )
            ),
            config=HybridConfig(high_skew_threshold=0.4, low_skew_threshold=0.1, key_top_n_per_bucket=4),
        )
        plan = planner.plan(samples, redis_free_bytes=1024 * 1024)

        executor = MigrationExecutor(
            cache,
            ExecutorConfig(mode=ExecutorMode.PROMOTE_ONLY, max_bytes_per_run=1024 * 1024, max_ops_per_run=10),
        )
        executor.apply_hybrid(plan, bucket_keys={"catalog": ["catalog:hot", "catalog:cold0"]})

        self.assertTrue(redis.exists("catalog:hot"))
        self.assertFalse(redis.exists("catalog:cold0"))
        self.assertFalse(cache.is_promoted("catalog"))


if __name__ == "__main__":
    unittest.main()
