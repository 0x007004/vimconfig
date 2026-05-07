---
id: rta.common.deviceid_priority
keywords: [oaid, idfa, androidid, deviceid, device_id, imei, sha256, md5]
created_by: lisi
created_at: 2026-01-22
source: feishu-msg-449281
verified: true
hit_count: 14
---

# Device ID priority and hashing rules

## Problem
Each media takes device ids in a different shape (raw / md5 / sha256) and
some refuse to bid if the wrong kind is sent.

## Answer
General order of preference on Android:
1. **oaid** (preferred, if available)
2. **androidid** (fallback)
3. **NEVER use imei** — Android 10+ blocks it; using it is a privacy violation

What format to send:
- FoxAds: raw oaid string
- OwlAds: md5 lowercase hex of raw oaid (`oaid_md5` field)
- BearAds: sha256 lowercase hex of raw oaid (`oaid_sha256` field) — note,
  the media already pre-hashes; do NOT double-hash on our side

iOS:
- idfa only when `att_status == authorized`
- otherwise idfv

## Counter-example (the gold)
We once sent OwlAds a raw oaid in the `oaid_md5` field. Bids were 100%
rejected with cryptic `bad_device_format` errors. The error message did
NOT say "this is not a hash" — we discovered it only by diffing the
field against the protocol spec.

## Notes
The agent's adapter generation should always cross-check the device-id
field name against the protocol spec verbatim. Don't assume the field
name implies the format.
