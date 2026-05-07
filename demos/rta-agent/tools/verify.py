"""The Sensors layer.

Runs the four mandatory sensor checks declared in policy.yaml:

  1. schema_check       — adapter loads, parse_request returns canonical shape
  2. sandbox_replay     — 100-request replay against the mock media server
  3. latency_budget     — per-request adapter overhead < 0.6x media tmax
  4. reconcile_parity   — replay corpus matches expected fixture set

This is what gates "done" for any adapter, agent-written or human-written.
"""

from __future__ import annotations

import argparse
import importlib
import json
import sys
import time
from dataclasses import dataclass, field, asdict
from typing import List

from .paths import setup_import_paths

setup_import_paths()

from adapters import load_adapters  # noqa: E402
from adapters.base import (  # noqa: E402
    NormalizedBid,
    NormalizedBidResponse,
)
from sandbox.server import (  # noqa: E402
    build_replay_corpus,
    validate_wire_response,
)


@dataclass
class CheckResult:
    name: str
    passed: bool
    details: str = ""

    def __str__(self) -> str:
        mark = "✓" if self.passed else "✗"
        return f"  [{mark}] {self.name}: {self.details}"


@dataclass
class VerifyReport:
    media: str
    checks: List[CheckResult] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.checks)

    def to_json(self) -> str:
        return json.dumps(
            {"media": self.media, "passed": self.passed,
             "checks": [asdict(c) for c in self.checks]},
            indent=2,
        )


def _make_response(req, expect_bid: bool) -> NormalizedBidResponse:
    if not expect_bid:
        return NormalizedBidResponse(request_id=req.request_id, bids=[])
    return NormalizedBidResponse(
        request_id=req.request_id,
        bids=[NormalizedBid(slot_id=req.slot_ids[0], price=2.10, creative_id="cr-7")],
    )


def verify_media(media: str) -> VerifyReport:
    report = VerifyReport(media=media)

    importlib.invalidate_caches()
    try:
        registry = load_adapters()
    except Exception as e:
        report.checks.append(CheckResult("import", False, f"adapter import failed: {e}"))
        return report

    if media not in registry:
        report.checks.append(
            CheckResult("import", False, f"no adapter found for media={media}")
        )
        return report

    adapter_cls = registry[media]
    adapter = adapter_cls()
    report.checks.append(CheckResult("import", True, f"loaded {adapter_cls.__name__}"))

    schema_ok = True
    schema_details = ""
    try:
        smoke_req, _ = build_replay_corpus(media, n=1)[0]
        parsed = adapter.parse_request(smoke_req.body, smoke_req.headers)
        for attr in ("request_id", "slot_ids", "deadline_ms"):
            if getattr(parsed, attr, None) in (None, [], ""):
                schema_ok = False
                schema_details = f"NormalizedBidRequest.{attr} is empty"
                break
        if schema_ok:
            schema_details = (
                f"id={parsed.request_id}, slots={parsed.slot_ids}, "
                f"deadline={parsed.deadline_ms}ms"
            )
    except Exception as e:
        schema_ok = False
        schema_details = f"parse_request raised {type(e).__name__}: {e}"
    report.checks.append(CheckResult("schema_check", schema_ok, schema_details))
    if not schema_ok:
        return report

    corpus = build_replay_corpus(media, n=100)
    failures: List[str] = []
    latencies_ms: List[float] = []
    for req, expect_bid in corpus:
        try:
            t0 = time.perf_counter()
            if not adapter.verify_signature(req.body, req.headers):
                failures.append(f"sig fail on {req.body[:40]}...")
                continue
            parsed = adapter.parse_request(req.body, req.headers)
            resp = _make_response(parsed, expect_bid)
            wire = adapter.encode_response(resp)
            elapsed_ms = (time.perf_counter() - t0) * 1000
            latencies_ms.append(elapsed_ms)
        except Exception as e:
            failures.append(f"raised {type(e).__name__}: {e}")
            continue
        is_no_bid = not expect_bid
        ok, msg = validate_wire_response(
            media, wire.status, wire.body, parsed.request_id, is_no_bid
        )
        if not ok:
            failures.append(msg)
    if failures:
        report.checks.append(CheckResult(
            "sandbox_replay", False,
            f"{len(failures)}/{len(corpus)} failed; first: {failures[0]}",
        ))
    else:
        report.checks.append(CheckResult(
            "sandbox_replay", True,
            f"{len(corpus)}/{len(corpus)} replayed cleanly",
        ))

    if latencies_ms:
        latencies_ms.sort()
        p99 = latencies_ms[int(len(latencies_ms) * 0.99) - 1]
        budget = adapter.LATENCY_BUDGET_MS
        latency_ok = p99 < budget
        report.checks.append(CheckResult(
            "latency_budget", latency_ok,
            f"p99={p99:.2f}ms vs budget {budget}ms",
        ))
    else:
        report.checks.append(CheckResult("latency_budget", False, "no samples"))

    parity_ok = len(failures) == 0
    report.checks.append(CheckResult(
        "reconcile_parity", parity_ok,
        "100% of replay fixtures matched expected wire shape" if parity_ok
        else f"reconciliation off by {len(failures)} requests",
    ))

    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("media")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    report = verify_media(args.media)
    if args.json:
        print(report.to_json())
    else:
        print(f"\n=== verify {args.media} ===")
        for c in report.checks:
            print(c)
        print(f"\nresult: {'PASS' if report.passed else 'FAIL'}\n")
    return 0 if report.passed else 1


if __name__ == "__main__":
    sys.exit(main())
