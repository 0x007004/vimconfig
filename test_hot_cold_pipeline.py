import unittest

from hot_cold_executor import ExecutorConfig, ExecutorMode, MigrationExecutor
from hot_cold_metrics import MetricsCollector, ReadEvent, WindowAggregator, WriteEvent
from hot_cold_probe import HotColdStrategyEngine, StrategyConfig, Tier
from hot_cold_simulation import run_simulation
from hot_cold_storage import FakeStoreConfig, InMemoryKVStore, TieredCache


class MetricsPipelineTests(unittest.TestCase):
    def test_collector_aggregates_reads_and_writes(self) -> None:
        collector = MetricsCollector()
        for _ in range(10):
            collector.record_read(
                ReadEvent(bucket_key="user", latency_us=200, hit=True, value_size_bytes=128)
            )
        for _ in range(2):
            collector.record_read(
                ReadEvent(
                    bucket_key="user",
                    latency_us=300,
                    hit=False,
                    value_size_bytes=128,
                    rocks_latency_us=15_000,
                )
            )
        collector.record_write(WriteEvent(bucket_key="user", value_size_bytes=128))
        collector.set_tier("user", Tier.ROCKS)

        snapshot = collector.snapshot_and_reset()
        aggregator = WindowAggregator(window_seconds=60.0, ewma_alpha=0.5)
        samples = aggregator.build_samples(snapshot)

        self.assertEqual(len(samples), 1)
        user = samples[0]
        self.assertEqual(user.key, "user")
        self.assertGreater(user.qps_ewma, 0.0)
        self.assertGreater(user.miss_penalty, 1.0)
        self.assertGreater(user.p99_rocks_ms, 0.0)
        self.assertEqual(user.tier, Tier.ROCKS)

    def test_executor_plan_only_does_not_touch_cache(self) -> None:
        redis = InMemoryKVStore(FakeStoreConfig(name="redis", read_latency_us=100, write_latency_us=100))
        rocks = InMemoryKVStore(FakeStoreConfig(name="rocks", read_latency_us=1000, write_latency_us=1500))
        rocks.set("k1", b"v1")
        cache = TieredCache(redis=redis, rocks=rocks)

        engine = HotColdStrategyEngine(
            StrategyConfig(
                hot_cycles_required=1,
                cold_cycles_required=1,
                promote_score_threshold=0.0001,
                min_qps_for_promotion=0.1,
            )
        )
        samples = [
            engine_sample_for_promotion(),
        ]
        plan = engine.plan_cycle(samples, redis_free_bytes=1024 * 1024)

        executor = MigrationExecutor(cache, ExecutorConfig(mode=ExecutorMode.PLAN_ONLY))
        result = executor.apply(plan, bucket_keys={"hot": ["k1"]})

        self.assertEqual(result.executed, [])
        self.assertFalse(redis.exists("k1"))

    def test_executor_promote_only_blocks_demote(self) -> None:
        redis = InMemoryKVStore(FakeStoreConfig(name="redis", read_latency_us=100, write_latency_us=100))
        rocks = InMemoryKVStore(FakeStoreConfig(name="rocks", read_latency_us=1000, write_latency_us=1500))
        rocks.set("k1", b"v1")
        rocks.set("k2", b"v2")
        cache = TieredCache(redis=redis, rocks=rocks, placement={"cold": True})
        redis.set("k2", b"v2")

        engine = HotColdStrategyEngine(
            StrategyConfig(
                hot_cycles_required=1,
                cold_cycles_required=1,
                promote_score_threshold=0.0001,
                demote_score_threshold=0.002,
                min_qps_for_promotion=0.1,
            )
        )
        samples = [
            engine_sample_for_promotion(),
            engine_sample_for_demotion(),
        ]
        plan = engine.plan_cycle(samples, redis_free_bytes=1024 * 1024)

        executor = MigrationExecutor(
            cache,
            ExecutorConfig(mode=ExecutorMode.PROMOTE_ONLY, max_bytes_per_run=1024 * 1024, max_ops_per_run=10),
        )
        result = executor.apply(plan, bucket_keys={"hot": ["k1"], "cold": ["k2"]})

        executed_actions = {a.action for a in result.executed}
        self.assertIn("promote", executed_actions)
        self.assertNotIn("demote", executed_actions)
        self.assertTrue(redis.exists("k1"))
        self.assertTrue(redis.exists("k2"))
        self.assertGreaterEqual(result.skipped.get("demote_blocked_by_mode", 0), 1)

    def test_simulation_reduces_p99(self) -> None:
        summary = run_simulation()
        self.assertGreater(summary.before_p99_ms, summary.after_p99_ms)
        self.assertGreater(summary.hit_rate_after, 0.0)


def engine_sample_for_promotion():
    from hot_cold_probe import KeyMetrics, Tier

    return KeyMetrics(
        key="hot",
        qps_ewma=100,
        p99_rocks_ms=20,
        p99_redis_ms=1,
        miss_penalty=1.2,
        value_size_bytes=256,
        write_churn=0.1,
        tier=Tier.ROCKS,
    )


def engine_sample_for_demotion():
    from hot_cold_probe import KeyMetrics, Tier

    return KeyMetrics(
        key="cold",
        qps_ewma=0.1,
        p99_rocks_ms=10,
        p99_redis_ms=1,
        miss_penalty=1.0,
        value_size_bytes=256,
        write_churn=0.0,
        tier=Tier.REDIS,
    )


if __name__ == "__main__":
    unittest.main()
