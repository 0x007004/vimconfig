import unittest

from hot_cold_probe import HotColdStrategyEngine, KeyMetrics, StrategyConfig, Tier


class HotColdProbeTests(unittest.TestCase):
    def test_score_prefers_high_gain_small_object(self) -> None:
        hot_small = KeyMetrics(
            key="a",
            qps_ewma=120,
            p99_rocks_ms=20,
            p99_redis_ms=1,
            miss_penalty=1.0,
            value_size_bytes=128,
            write_churn=0.1,
            tier=Tier.ROCKS,
        )
        hot_large = KeyMetrics(
            key="b",
            qps_ewma=120,
            p99_rocks_ms=20,
            p99_redis_ms=1,
            miss_penalty=1.0,
            value_size_bytes=4096,
            write_churn=0.1,
            tier=Tier.ROCKS,
        )
        self.assertGreater(
            HotColdStrategyEngine.score(hot_small),
            HotColdStrategyEngine.score(hot_large),
        )

    def test_promote_and_demote_in_same_cycle(self) -> None:
        engine = HotColdStrategyEngine(
            StrategyConfig(
                hot_cycles_required=1,
                cold_cycles_required=1,
                promote_score_threshold=0.001,
                demote_score_threshold=0.0002,
                redis_promotion_budget_bytes=1024 * 1024,
                migration_network_budget_bytes=1024 * 1024,
            )
        )

        samples = [
            KeyMetrics(
                key="hot-key",
                qps_ewma=100,
                p99_rocks_ms=30,
                p99_redis_ms=1,
                miss_penalty=1.2,
                value_size_bytes=256,
                write_churn=0.1,
                tier=Tier.ROCKS,
            ),
            KeyMetrics(
                key="cold-key",
                qps_ewma=0.2,
                p99_rocks_ms=10,
                p99_redis_ms=1,
                miss_penalty=1.0,
                value_size_bytes=256,
                write_churn=0.0,
                tier=Tier.REDIS,
            ),
        ]
        plan = engine.plan_cycle(samples, redis_free_bytes=0)
        actions = {(a.key, a.action) for a in plan.actions}
        self.assertIn(("hot-key", "promote"), actions)
        self.assertIn(("cold-key", "demote"), actions)
        self.assertEqual(plan.promoted, 1)
        self.assertEqual(plan.demoted, 1)

    def test_memory_budget_blocks_oversized_promotion(self) -> None:
        engine = HotColdStrategyEngine(
            StrategyConfig(
                hot_cycles_required=1,
                cold_cycles_required=1,
                promote_score_threshold=0.0001,
                redis_promotion_budget_bytes=100,
                migration_network_budget_bytes=1024,
            )
        )
        samples = [
            KeyMetrics(
                key="huge-hot",
                qps_ewma=1000,
                p99_rocks_ms=20,
                p99_redis_ms=1,
                value_size_bytes=2048,
                write_churn=0.0,
                tier=Tier.ROCKS,
            )
        ]
        plan = engine.plan_cycle(samples, redis_free_bytes=100)
        self.assertEqual(plan.promoted, 0)
        self.assertGreaterEqual(plan.skipped.get("memory_budget", 0), 1)

    def test_streak_requirement_prevents_flapping(self) -> None:
        engine = HotColdStrategyEngine(
            StrategyConfig(
                hot_cycles_required=2,
                cold_cycles_required=2,
                promote_score_threshold=0.001,
                redis_promotion_budget_bytes=1024,
                migration_network_budget_bytes=1024,
            )
        )
        sample = KeyMetrics(
            key="candidate",
            qps_ewma=80,
            p99_rocks_ms=15,
            p99_redis_ms=1,
            value_size_bytes=128,
            write_churn=0.1,
            tier=Tier.ROCKS,
        )
        plan1 = engine.plan_cycle([sample], redis_free_bytes=1000)
        plan2 = engine.plan_cycle([sample], redis_free_bytes=1000)
        self.assertEqual(plan1.promoted, 0)
        self.assertEqual(plan2.promoted, 1)


if __name__ == "__main__":
    unittest.main()
