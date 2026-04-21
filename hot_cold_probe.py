"""Hot/cold data probing and migration planning.

This module provides a minimal, dependency-free strategy engine that can be
embedded into a service which reads from Redis and RocksDB. It focuses on
deciding *what* to migrate between tiers based on expected TP99 gain.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, Iterable, List, Tuple


class Tier(str, Enum):
    """Current storage tier for a key."""

    REDIS = "redis"
    ROCKS = "rocks"


@dataclass(frozen=True)
class KeyMetrics:
    """Aggregated metrics for a single key in one scheduling cycle."""

    key: str
    qps_ewma: float
    p99_rocks_ms: float
    p99_redis_ms: float
    miss_penalty: float = 1.0
    value_size_bytes: int = 1
    write_churn: float = 0.0
    tier: Tier = Tier.ROCKS


@dataclass
class StrategyConfig:
    """Tunable policy knobs for migration planning."""

    promote_score_threshold: float = 0.0010
    demote_score_threshold: float = 0.0002
    min_qps_for_promotion: float = 5.0
    cold_qps_threshold: float = 0.8
    max_write_churn_for_promotion: float = 3.0
    hot_cycles_required: int = 2
    cold_cycles_required: int = 3
    max_promotions_per_cycle: int = 200
    max_demotions_per_cycle: int = 200
    redis_promotion_budget_bytes: int = 128 * 1024 * 1024
    migration_network_budget_bytes: int = 256 * 1024 * 1024


@dataclass(frozen=True)
class MigrationAction:
    """One planned migration action."""

    key: str
    action: str
    score: float
    estimated_tp99_gain_ms: float
    value_size_bytes: int
    reason: str


@dataclass
class MigrationPlan:
    """Planner output for a single cycle."""

    cycle: int
    actions: List[MigrationAction] = field(default_factory=list)
    promoted: int = 0
    demoted: int = 0
    skipped: Dict[str, int] = field(default_factory=dict)


class HotColdStrategyEngine:
    """Decides what keys should move between RocksDB and Redis."""

    def __init__(self, config: StrategyConfig | None = None) -> None:
        self.config = config or StrategyConfig()
        self._hot_streak: Dict[str, int] = {}
        self._cold_streak: Dict[str, int] = {}
        self._cycle = 0

    @staticmethod
    def score(metrics: KeyMetrics) -> float:
        """Expected gain per byte with churn penalty.

        score = qps_ewma * (p99_rocks - p99_redis) * miss_penalty
                / (value_size_bytes * (1 + write_churn))
        """

        latency_gain = max(metrics.p99_rocks_ms - metrics.p99_redis_ms, 0.0)
        numerator = max(metrics.qps_ewma, 0.0) * latency_gain * max(metrics.miss_penalty, 0.0)
        denominator = max(metrics.value_size_bytes, 1) * (1.0 + max(metrics.write_churn, 0.0))
        return numerator / denominator

    def plan_cycle(self, samples: Iterable[KeyMetrics], redis_free_bytes: int) -> MigrationPlan:
        """Build a migration plan for current cycle.

        Args:
            samples: key-level metrics snapshot for this cycle.
            redis_free_bytes: currently available Redis memory.
        """

        self._cycle += 1
        plan = MigrationPlan(cycle=self._cycle)

        promote_candidates: List[Tuple[float, KeyMetrics]] = []
        demote_candidates: List[Tuple[float, KeyMetrics]] = []

        for sample in samples:
            score = self.score(sample)
            hot, cold = self._update_streaks(sample, score)

            if sample.tier == Tier.ROCKS and hot:
                promote_candidates.append((score, sample))
            if sample.tier == Tier.REDIS and cold:
                demote_candidates.append((score, sample))

        demote_candidates.sort(key=lambda item: item[0])  # coldest first
        promote_candidates.sort(key=lambda item: item[0], reverse=True)

        # Step 1: demote cold keys first to reclaim memory.
        reclaimed_bytes, used_network = self._add_demotions(demote_candidates, plan)

        # Step 2: promote profitable keys within memory and network budgets.
        promotion_mem_cap = min(
            self.config.redis_promotion_budget_bytes,
            max(redis_free_bytes, 0) + reclaimed_bytes,
        )
        self._add_promotions(promote_candidates, plan, promotion_mem_cap, used_network)
        return plan

    def _update_streaks(self, sample: KeyMetrics, score: float) -> Tuple[bool, bool]:
        is_hot = (
            score >= self.config.promote_score_threshold
            and sample.qps_ewma >= self.config.min_qps_for_promotion
            and sample.write_churn <= self.config.max_write_churn_for_promotion
        )
        is_cold = score <= self.config.demote_score_threshold or sample.qps_ewma <= self.config.cold_qps_threshold

        if is_hot:
            self._hot_streak[sample.key] = self._hot_streak.get(sample.key, 0) + 1
        else:
            self._hot_streak[sample.key] = 0

        if is_cold:
            self._cold_streak[sample.key] = self._cold_streak.get(sample.key, 0) + 1
        else:
            self._cold_streak[sample.key] = 0

        hot_ready = self._hot_streak[sample.key] >= self.config.hot_cycles_required
        cold_ready = self._cold_streak[sample.key] >= self.config.cold_cycles_required
        return hot_ready, cold_ready

    def _add_demotions(self, candidates: List[Tuple[float, KeyMetrics]], plan: MigrationPlan) -> Tuple[int, int]:
        reclaimed_bytes = 0
        network_used = 0
        for score, sample in candidates:
            if plan.demoted >= self.config.max_demotions_per_cycle:
                self._skip(plan, "demotion_limit")
                break

            size = max(sample.value_size_bytes, 1)
            if network_used + size > self.config.migration_network_budget_bytes:
                self._skip(plan, "network_budget")
                break

            network_used += size
            reclaimed_bytes += size
            plan.demoted += 1
            plan.actions.append(
                MigrationAction(
                    key=sample.key,
                    action="demote",
                    score=score,
                    estimated_tp99_gain_ms=0.0,
                    value_size_bytes=size,
                    reason="cold key in redis; move back to rocks",
                )
            )
        return reclaimed_bytes, network_used

    def _add_promotions(
        self,
        candidates: List[Tuple[float, KeyMetrics]],
        plan: MigrationPlan,
        promotion_mem_cap: int,
        network_used: int,
    ) -> None:
        mem_used = 0
        for score, sample in candidates:
            if plan.promoted >= self.config.max_promotions_per_cycle:
                self._skip(plan, "promotion_limit")
                break

            size = max(sample.value_size_bytes, 1)
            if mem_used + size > promotion_mem_cap:
                self._skip(plan, "memory_budget")
                continue
            if network_used + size > self.config.migration_network_budget_bytes:
                self._skip(plan, "network_budget")
                continue

            mem_used += size
            network_used += size
            plan.promoted += 1
            estimated_gain = max(sample.p99_rocks_ms - sample.p99_redis_ms, 0.0)
            plan.actions.append(
                MigrationAction(
                    key=sample.key,
                    action="promote",
                    score=score,
                    estimated_tp99_gain_ms=estimated_gain,
                    value_size_bytes=size,
                    reason="hot key in rocks; promote to redis",
                )
            )

    @staticmethod
    def _skip(plan: MigrationPlan, reason: str) -> None:
        plan.skipped[reason] = plan.skipped.get(reason, 0) + 1


def demo_plan() -> MigrationPlan:
    """Small deterministic example used in docs and manual verification."""

    engine = HotColdStrategyEngine(
        StrategyConfig(
            hot_cycles_required=1,
            cold_cycles_required=1,
            promote_score_threshold=0.0008,
            demote_score_threshold=0.0002,
            redis_promotion_budget_bytes=1024 * 1024,
            migration_network_budget_bytes=2 * 1024 * 1024,
        )
    )

    samples = [
        KeyMetrics(
            key="user:42:profile",
            qps_ewma=220.0,
            p99_rocks_ms=18.0,
            p99_redis_ms=1.2,
            miss_penalty=1.5,
            value_size_bytes=350,
            write_churn=0.1,
            tier=Tier.ROCKS,
        ),
        KeyMetrics(
            key="order:archive:2020",
            qps_ewma=0.4,
            p99_rocks_ms=14.0,
            p99_redis_ms=1.2,
            value_size_bytes=1100,
            write_churn=0.0,
            tier=Tier.REDIS,
        ),
    ]
    return engine.plan_cycle(samples, redis_free_bytes=3000)


if __name__ == "__main__":
    plan = demo_plan()
    for action in plan.actions:
        print(
            f"{action.action.upper():7} key={action.key} score={action.score:.6f} "
            f"size={action.value_size_bytes}B reason={action.reason}"
        )
    print(f"promoted={plan.promoted}, demoted={plan.demoted}, skipped={plan.skipped}")
