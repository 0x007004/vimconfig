"""FoxAds RTA adapter — see protocols/foxads.md."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Dict, Optional

from .base import (
    NormalizedBid,
    NormalizedBidRequest,
    NormalizedBidResponse,
    RTAAdapter,
    WireResponse,
)


SECRET = b"foxads-shared-secret-demo"
TS_TOLERANCE_S = 60


class FoxAdsAdapter(RTAAdapter):
    MEDIA = "foxads"
    LATENCY_BUDGET_MS = 60

    def parse_request(self, body: str, headers: Dict[str, str]) -> NormalizedBidRequest:
        data = json.loads(body)
        imps = data.get("imp", [])
        device = data.get("device", {})
        return NormalizedBidRequest(
            request_id=data["id"],
            slot_ids=[i["tagid"] for i in imps],
            device_id=device.get("oaid") or device.get("androidid"),
            device_id_kind="oaid" if device.get("oaid") else (
                "androidid" if device.get("androidid") else None
            ),
            floor_price=0.0,
            deadline_ms=int(data.get("tmax", 100)),
            raw=data,
        )

    def verify_signature(self, body: str, headers: Dict[str, str]) -> bool:
        sig = headers.get("X-FoxAds-Sign", "")
        ts = headers.get("X-FoxAds-Timestamp", "")
        if not sig or not ts:
            return False
        try:
            ts_int = int(ts)
        except ValueError:
            return False
        if abs(time.time() - ts_int) > TS_TOLERANCE_S:
            return False
        expected = hmac.new(
            SECRET, f"{body}.{ts}".encode("utf-8"), hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(sig, expected)

    def encode_response(self, resp: NormalizedBidResponse) -> WireResponse:
        if resp.is_no_bid:
            return WireResponse(status=204, headers={}, body=None)
        body = json.dumps({
            "id": resp.request_id,
            "seatbid": [{
                "bid": [
                    {"price": b.price, "adid": b.creative_id} for b in resp.bids
                ]
            }],
        })
        return WireResponse(
            status=200,
            headers={"Content-Type": "application/json"},
            body=body,
        )

    def device_id_for_bidding(self, req: NormalizedBidRequest) -> Optional[str]:
        if req.device_id_kind == "imei":
            return None
        return req.device_id
