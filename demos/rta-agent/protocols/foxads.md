# FoxAds RTA Protocol v2.1

**Status**: Integrated (reference)
**Adapter**: `adapters/foxads.py`
**Last updated**: 2026-01-15

## Endpoint

```
POST https://rta.foxads.example/v2/bid
Content-Type: application/json
X-FoxAds-Sign: <hmac_sha256_hex>
X-FoxAds-Timestamp: <unix_seconds>
```

## Bid Request (incoming)

```json
{
  "id": "abc123",
  "imp": [{"id": "1", "tagid": "slot-42"}],
  "device": {
    "oaid": "...",
    "androidid": "...",
    "ua": "Mozilla/5.0 ..."
  },
  "user": {"uid": "fox-user-7788"},
  "at": 2,
  "tmax": 100
}
```

## Bid Response (we send)

If bidding:
```json
{
  "id": "abc123",
  "seatbid": [{"bid": [{"price": 1.23, "adid": "creative-9"}]}]
}
```

If not bidding: **HTTP 204, empty body**.

## Signature

```
sign = hmac_sha256(secret, body + "." + timestamp)
```

Timestamp tolerance: ±60s.

## Rate limits

- 5,000 QPS per region
- 100ms hard timeout

## Required device id

Android: `oaid` preferred, fall back to `androidid`. Never use `imei`.
