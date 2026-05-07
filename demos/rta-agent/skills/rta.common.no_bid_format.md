---
id: rta.common.no_bid_format
keywords: [no_bid, nbr, empty, response, 204, echo, id]
created_by: zhangsan
created_at: 2026-02-15
source: pr#1042
verified: true
hit_count: 8
---

# No-bid response format varies and is unforgiving

## Problem
Each media has its own no-bid wire format and silently penalizes wrong shapes.

## Answer
Read the protocol spec carefully. Common patterns observed:

- **HTTP 204, empty body** (FoxAds-style)
- **HTTP 200, `{"nbr": 1}`** (OwlAds-style)
- **HTTP 200, `{"id": <echoed>, "nbr": 0}`** — request id MUST be echoed back

## Counter-example (the gold)
We sent OwlAds an HTTP 204 once. Their system did NOT 4xx — it accepted the
response but counted it as malformed and started throttling our QPS down by
10% per hour. Took two days to notice via win-rate dropping.

**The lesson**: silent acceptance is the most dangerous failure mode in RTA.
Always validate the no-bid shape against the spec, every time.

## Notes
This is also covered in `pitfalls.md` P-001 — when adding a new media, check
that section first.
