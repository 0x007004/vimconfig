# OwlAds RTA Protocol v3.0

**Status**: Integrated (reference)
**Adapter**: `adapters/owlads.py`
**Last updated**: 2026-02-28

## Endpoint

```
POST https://rta.owlads.example/v3/auction
Content-Type: application/json
Authorization: OwlAds <api_key>
X-Sig: <signature>
X-Ts: <unix_milliseconds>
```

## Bid Request (incoming)

```json
{
  "request_id": "owl-xxx",
  "slots": [{"slot_id": "owl-slot-9", "floor_price": 0.5}],
  "device_info": {
    "oaid_md5": "...",
    "android_id_md5": "...",
    "platform": "android"
  },
  "geo": {"country": "CN", "region": "31"},
  "deadline_ms": 80
}
```

## Bid Response (we send)

If bidding:
```json
{
  "request_id": "owl-xxx",
  "bids": [{"slot_id": "owl-slot-9", "price": 2.10, "creative_id": "c-12"}]
}
```

If not bidding: **HTTP 200**, body `{"nbr": 1}`.

## Signature

```
sig = sha256(api_key + ":" + body + ":" + ts_ms)
```

Note: timestamp is **milliseconds**, not seconds. Tolerance ±30s.

## Rate limits

- 8,000 QPS global, but enforced per-PoP (CN-East, CN-North, CN-South)
- 80ms deadline (yes, tighter than FoxAds)

## Required device id

OwlAds pre-hashes device ids. Send them as MD5 lowercase hex.
**Do not** send raw oaid/androidid — they will reject the bid.
