# BearAds RTA Protocol v1.0

**Status**: NOT YET INTEGRATED — agent target
**Adapter**: (does not exist yet)
**Last updated**: 2026-05-01

## Endpoint

```
POST https://rta.bearads.example/rta/bid
Content-Type: application/json
X-Bear-Auth: <bearer_token>
X-Bear-Nonce: <16_char_nonce>
X-Bear-Sign: <signature>
```

## Bid Request (incoming)

```json
{
  "id": "bear-req-001",
  "ad_slots": [
    {"slot_id": "bs-1", "floor": 0.8, "size": "640x960"}
  ],
  "device": {
    "oaid_sha256": "<sha256 lowercase hex of raw oaid>",
    "platform": "android",
    "model": "Mi 14"
  },
  "ip": "203.0.113.42",
  "tmax_ms": 80
}
```

## Bid Response (we send)

If bidding:
```json
{
  "id": "bear-req-001",
  "bids": [{"slot_id": "bs-1", "price_cents": 210, "creative_id": "cr-7"}]
}
```

If not bidding: **HTTP 200**, body `{"id": "bear-req-001", "nbr": 0}`.

> ⚠️ The `id` in the no-bid response **must echo the request id**. Returning
> a fixed/empty id will be silently treated as malformed and decremented from
> your QPS quota.

## Signature

```
sign = hmac_sha512(secret, nonce + "." + body_sha256_hex)
```

Notes:
- **HMAC-SHA512**, not SHA256 (most other media use 256)
- Hashed input is `nonce + "." + sha256_hex_of_body`, not body itself
- Bearer token `X-Bear-Auth` is separate from signing secret

## Rate limits

- 3,000 QPS global
- **80ms** hard deadline
- Frequency capping: max 1 bid per `oaid` per 30s

## Required device id

Android: `oaid_sha256` (already pre-hashed by media; lowercase hex of sha256 of
raw oaid). **Do not double-hash.** No fallback — if no oaid, do not bid.

## Pricing

Note: BearAds uses **price_cents** (integer cents), not float price.
A bid of 2.10 RMB is sent as `"price_cents": 210`.
