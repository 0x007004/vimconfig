"""Hybrid planner combining bucket-level and key-level decisions.

For each bucket it picks one of three modes based on how concentrated the
read QPS is (the "skew index"):

* ``bucket_promote`` - load is spread across the bucket, promote the whole
  prefix so every key inside it benefits.
* ``key_promote`` - load is dominated by a small number of hot keys, only
  promote those keys instead of the whole bucket.
* ``skip`` - neither the bucket nor its hot keys meet the promotion
  thresholds in this cycle.

Demotions always fall back to the bucket-level engine.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List

from hot_cold_metrics import AggregatedSamples
from hot_cold_probe import (
    HotColdStrategyEngine,
    KeyMetrics,
    MigrationAction,
    MigrationPlan,
    StrategyConfig,
)


@dataclass
class HybridConfig:
    """Knobs controlling the bucket-vs-key decision."""

    high_skew_threshold: float = 0.5
    low_skew_threshold: float = 0.1
    key_top_n_per_bucket: int = 32


@dataclass
class HybridPlan:
    """Planner output mixing bucket-level and key-level actions."""

    cycle: int
    bucket_actions: List[MigrationAction] = field(default_factory=list)
    key_actions: List[MigrationAction] = field(default_factory=list)
    skipped: Dict[str, int] = field(default_factory=dict)

    @property
    def actions(self) -> List[MigrationAction]:
        return self.bucket_actions + self.key_actions


class HybridPlanner:
    """Glues bucket-level engine + key-level engine behind one API."""

    def __init__(
        self,
        bucket_engine: HotColdStrategyEngine,
        key_engine: HotColdStrategyEngine,
        config: HybridConfig | None = None,
    ) -> None:
        self._bucket_engine = bucket_engine
        self._key_engine = key_engine
        self._config = config or HybridConfig()
        self._cycle = 0

    def plan(self, samples: AggregatedSamples, redis_free_bytes: int) -> HybridPlan:
        self._cycle += 1
        plan = HybridPlan(cycle=self._cycle)

        bucket_plan = self._bucket_engine.plan_cycle(samples.bucket_samples, redis_free_bytes)
        bucket_promote_names = {a.key for a in bucket_plan.actions if a.action == "promote"}

        keys_by_bucket: Dict[str, List[KeyMetrics]] = {}
        for sample in samples.key_samples:
            bucket_key = samples.key_to_bucket.get(sample.key, "")
            keys_by_bucket.setdefault(bucket_key, []).append(sample)

        key_candidates: List[KeyMetrics] = []
        for bucket_key, bucket_keys in keys_by_bucket.items():
            skew = samples.skew_by_bucket.get(bucket_key, 0.0)
            if skew < self._config.low_skew_threshold:
                self._skip(plan, "skew_too_low")
                continue
            if bucket_key in bucket_promote_names and skew < self._config.high_skew_threshold:
                self._skip(plan, "bucket_covers_key")
                continue
            bucket_keys.sort(key=self._key_engine.score, reverse=True)
            key_candidates.extend(bucket_keys[: self._config.key_top_n_per_bucket])

        for action in bucket_plan.actions:
            if action.action == "promote":
                plan.bucket_actions.append(action)
            else:
                plan.key_actions.append(action)

        if key_candidates:
            key_plan = self._key_engine.plan_cycle(key_candidates, redis_free_bytes)
            for action in key_plan.actions:
                if action.action == "promote":
                    plan.key_actions.append(action)

        for reason, count in bucket_plan.skipped.items():
            plan.skipped[reason] = plan.skipped.get(reason, 0) + count
        return plan

    @staticmethod
    def _skip(plan: HybridPlan, reason: str) -> None:
        plan.skipped[reason] = plan.skipped.get(reason, 0) + 1


def default_hybrid_planner() -> HybridPlanner:
    """Convenience factory used by the simulation and examples."""

    bucket_engine = HotColdStrategyEngine(
        StrategyConfig(
            hot_cycles_required=1,
            cold_cycles_required=1,
            promote_score_threshold=0.0005,
            demote_score_threshold=0.00005,
            min_qps_for_promotion=1.0,
        )
    )
    key_engine = HotColdStrategyEngine(
        StrategyConfig(
            hot_cycles_required=1,
            cold_cycles_required=1,
            promote_score_threshold=0.0001,
            demote_score_threshold=0.00001,
            min_qps_for_promotion=0.5,
        )
    )
    return HybridPlanner(bucket_engine, key_engine)
