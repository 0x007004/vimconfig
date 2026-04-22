"""Migration executor with staged safety modes.

This module is intentionally conservative. The four safety modes mirror the
recommended rollout: observe first, plan second, promote only, then finally
allow both promote and demote.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, Iterable, List, Optional

from hot_cold_hybrid import HybridPlan
from hot_cold_probe import MigrationAction, MigrationPlan
from hot_cold_storage import TieredCache


class ExecutorMode(str, Enum):
    COLLECT_ONLY = "collect_only"
    PLAN_ONLY = "plan_only"
    PROMOTE_ONLY = "promote_only"
    FULL = "full"


@dataclass
class ExecutorConfig:
    mode: ExecutorMode = ExecutorMode.PLAN_ONLY
    max_bytes_per_run: int = 64 * 1024 * 1024
    max_ops_per_run: int = 500
    allowed_prefixes: Optional[List[str]] = None
    dry_run: bool = False


@dataclass
class ExecutionResult:
    mode: ExecutorMode
    executed: List[MigrationAction] = field(default_factory=list)
    skipped: Dict[str, int] = field(default_factory=dict)
    bytes_moved: int = 0


class MigrationExecutor:
    """Apply a ``MigrationPlan`` to a ``TieredCache`` under strict limits.

    The executor does not touch user data directly; it only updates placement
    and (for promote) pre-warms Redis by reading the underlying rows. That
    keeps the worker idempotent and safe to retry.
    """

    def __init__(self, cache: TieredCache, config: Optional[ExecutorConfig] = None) -> None:
        self._cache = cache
        self._config = config or ExecutorConfig()

    def apply_hybrid(
        self, plan: HybridPlan, bucket_keys: Dict[str, Iterable[str]]
    ) -> ExecutionResult:
        """Apply a hybrid plan: bucket actions promote whole prefixes,
        key actions promote single keys without touching siblings."""

        result = ExecutionResult(mode=self._config.mode)
        if self._config.mode in (ExecutorMode.COLLECT_ONLY, ExecutorMode.PLAN_ONLY):
            result.skipped[self._config.mode.value] = len(plan.actions)
            return result

        bytes_budget = self._config.max_bytes_per_run
        ops_budget = self._config.max_ops_per_run

        for action in plan.bucket_actions:
            ops_budget, bytes_budget = self._execute(
                action, bucket_keys.get(action.key, []), result, ops_budget, bytes_budget
            )
            if ops_budget <= 0:
                break

        for action in plan.key_actions:
            if action.action == "promote":
                targets = [action.key]
            else:
                targets = list(bucket_keys.get(action.key, []))
            ops_budget, bytes_budget = self._execute(
                action, targets, result, ops_budget, bytes_budget, key_level=True
            )
            if ops_budget <= 0:
                break

        return result

    def apply(self, plan: MigrationPlan, bucket_keys: Dict[str, Iterable[str]]) -> ExecutionResult:
        result = ExecutionResult(mode=self._config.mode)
        if self._config.mode == ExecutorMode.COLLECT_ONLY:
            result.skipped["collect_only"] = len(plan.actions)
            return result

        if self._config.mode == ExecutorMode.PLAN_ONLY:
            result.skipped["plan_only"] = len(plan.actions)
            return result

        bytes_budget = self._config.max_bytes_per_run
        ops_budget = self._config.max_ops_per_run

        for action in plan.actions:
            if not self._is_allowed(action.key):
                self._skip(result, "prefix_not_allowed")
                continue
            if action.action == "demote" and self._config.mode == ExecutorMode.PROMOTE_ONLY:
                self._skip(result, "demote_blocked_by_mode")
                continue
            if ops_budget <= 0:
                self._skip(result, "ops_budget_exhausted")
                break
            if action.value_size_bytes > bytes_budget:
                self._skip(result, "bytes_budget_exhausted")
                continue

            keys_for_bucket = list(bucket_keys.get(action.key, []))
            if self._config.dry_run:
                result.executed.append(action)
                result.bytes_moved += action.value_size_bytes
                ops_budget -= 1
                bytes_budget -= action.value_size_bytes
                continue

            if action.action == "promote":
                self._promote(action, keys_for_bucket)
            elif action.action == "demote":
                self._demote(action, keys_for_bucket)
            else:
                self._skip(result, f"unknown_action:{action.action}")
                continue

            result.executed.append(action)
            result.bytes_moved += action.value_size_bytes
            ops_budget -= 1
            bytes_budget -= action.value_size_bytes

        return result

    def _execute(
        self,
        action: MigrationAction,
        targets: Iterable[str],
        result: ExecutionResult,
        ops_budget: int,
        bytes_budget: int,
        key_level: bool = False,
    ) -> tuple[int, int]:
        if not self._is_allowed(action.key):
            self._skip(result, "prefix_not_allowed")
            return ops_budget, bytes_budget
        if action.action == "demote" and self._config.mode == ExecutorMode.PROMOTE_ONLY:
            self._skip(result, "demote_blocked_by_mode")
            return ops_budget, bytes_budget
        if ops_budget <= 0:
            self._skip(result, "ops_budget_exhausted")
            return ops_budget, bytes_budget
        if action.value_size_bytes > bytes_budget:
            self._skip(result, "bytes_budget_exhausted")
            return ops_budget, bytes_budget

        if self._config.dry_run:
            result.executed.append(action)
            result.bytes_moved += action.value_size_bytes
            return ops_budget - 1, bytes_budget - action.value_size_bytes

        if action.action == "promote":
            self._promote(action, targets, key_level)
        elif action.action == "demote":
            self._demote(action, targets, key_level)
        else:
            self._skip(result, f"unknown_action:{action.action}")
            return ops_budget, bytes_budget

        result.executed.append(action)
        result.bytes_moved += action.value_size_bytes
        return ops_budget - 1, bytes_budget - action.value_size_bytes

    def _promote(
        self,
        action: MigrationAction,
        targets: Iterable[str],
        key_level: bool = False,
    ) -> None:
        if not key_level:
            self._cache.mark_promoted(action.key)
        for full_key in targets:
            value, _ = self._cache.rocks.get(full_key)
            if value is not None:
                self._cache.redis.set(full_key, value)

    def _demote(
        self,
        action: MigrationAction,
        targets: Iterable[str],
        key_level: bool = False,
    ) -> None:
        if not key_level:
            self._cache.mark_demoted(action.key)
        for full_key in targets:
            self._cache.redis.delete(full_key)

    def _is_allowed(self, bucket_key: str) -> bool:
        prefixes = self._config.allowed_prefixes
        if not prefixes:
            return True
        return any(bucket_key.startswith(prefix) for prefix in prefixes)

    @staticmethod
    def _skip(result: ExecutionResult, reason: str) -> None:
        result.skipped[reason] = result.skipped.get(reason, 0) + 1
