---
id: rta.common.timeout_budget
keywords: [tmax, timeout, deadline, latency, budget, p99, slo]
created_by: wangwu
created_at: 2026-03-04
source: pr#1187
verified: true
hit_count: 6
---

# Real adapter latency budget = 0.6 × media tmax

## Problem
The "100ms timeout" stated in protocol docs is the END-TO-END deadline as
measured by the media's edge. Our adapter's wall-clock budget is much smaller.

## Answer
Rule of thumb:
```
real_budget_ms = media_tmax_ms × 0.6
```

Reasoning:
- Network RTT (CN intra-region): 10–25ms
- Media-side parsing/queueing: ~10ms
- Safety margin: 5–10ms

So a 100ms tmax leaves ~60ms; an 80ms tmax leaves ~50ms; a 50ms tmax leaves
~30ms (and at that point you should question whether to bid in this region
at all).

The adapter's `LATENCY_BUDGET_MS` class attribute should be set to this
real budget. The verify sensor will fail any adapter whose p99 exceeds it.

## Counter-example (the gold)
We once shipped an adapter for a 50ms-tmax media with `LATENCY_BUDGET_MS = 50`.
Sandbox passed (sandbox is in-process, network = 0ms). Production p99 sat at
55ms-65ms. We weren't timing out per request, but the media's edge was timing
out on receipt and our win rate looked anomalously low for a week.

## Notes
Prefer 0.5x for any cross-region path; only 0.6x is safe for intra-region.
