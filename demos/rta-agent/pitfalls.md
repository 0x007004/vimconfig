# RTA Pitfalls — Known Gotchas

> Living document. Every "we got bit by this" lesson lands here.
> The agent reads this BEFORE generating any new adapter.
> Mapping: Anthropic Harness Engineering → "Guides (feedforward)".

## P-001 — Empty no-bid response format varies by media

| Media | No-bid format |
|-------|---------------|
| FoxAds | HTTP 204, empty body |
| OwlAds | HTTP 200, `{"nbr": 1}` |
| BearAds | HTTP 200, `{"id": "<request_id>", "nbr": 0}` (must echo id) |

**Why it bites**: Returning HTTP 200 with an empty body to FoxAds gets you
auto-throttled. Forgetting to echo `id` to BearAds is silently treated as a
malformed response and decremented from your QPS quota.

## P-002 — DeviceID priority is not universal

The "right" device id to use varies:

- Android: `oaid` > `androidid` > (never use `imei` post-2023)
- iOS: `idfa` (only when `att_status == authorized`) > `idfv`

Some media (OwlAds) require the *original* idfa string with hyphens.
Some media (BearAds) require it sha256-hashed lowercase.

## P-003 — Timestamp window is signed, not just timestamp

Most signature schemes hash `request_body + timestamp + secret`. If your
local clock drifts more than the media's tolerance window (FoxAds: 60s,
OwlAds: 30s, BearAds: 10s), every request 401s.

**Mitigation**: Always run NTP sync. Add a sensor that compares
`time.time()` against an HTTP `Date:` header from the media's health endpoint.

## P-004 — Latency budget != timeout

If the media's contract says "100ms timeout", your real budget is roughly
`100ms - (network RTT) - (their parsing time) - safety margin ≈ 60-70ms`.

Always size your internal latency budget at **0.6x of the media-side timeout**.

## P-005 — Reconciliation drift > 5% means you're getting deducted

If `our_recorded_bids / their_recorded_bids < 0.95` over a 1h window, the
media is silently deducting (扣量). Don't let the BD team find out from
Monday's invoice — the agent should sensor-check this hourly.

## P-006 — QPS limits are per-region, not global

Most media publish a single QPS number but enforce it per their POP
(point of presence). If you load-balance across regions, you may need
to negotiate a separate quota per region or your overall throughput will
be capped at the smallest region's allocation.
