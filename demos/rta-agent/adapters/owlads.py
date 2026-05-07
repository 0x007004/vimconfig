"""OwlAds RTA adapter — see protocols/owlads.md."""

from __future__ import annotations

import hashlib
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


API_KEY = "owlads-demo-api-key"
TS_TOLERANCE_MS = 30_000


class OwlAdsAdapter(RTAAdapter):
    MEDIA = "owlads"
    LATENCY_BUDGET_MS = 50

    def parse_request(self, body: str, headers: Dict[str, str]) -> NormalizedBidRequest:
        data = json.loads(body)
        slots = data.get("slots", [])
        di = data.get("device_info", {})
        return NormalizedBidRequest(
            request_id=data["request_id"],
            slot_ids=[s["slot_id"] for s in slots],
            device_id=di.get("oaid_md5") or di.get("android_id_md5"),
            device_id_kind="oaid_md5" if di.get("oaid_md5") else (
                "android_id_md5" if di.get("android_id_md5") else None
            ),
            floor_price=float(slots[0].get("floor_price", 0.0)) if slots else 0.0,
            deadline_ms=int(data.get("deadline_ms", 80)),
            raw=data,
        )

    def verify_signature(self, body: str, headers: Dict[str, str]) -> bool:
        sig = headers.get("X-Sig", "")
        ts_ms = headers.get("X-Ts", "")
        if not sig or not ts_ms:
            return False
        try:
            ts_int = int(ts_ms)
        except ValueError:
            return False
        if abs(int(time.time() * 1000) - ts_int) > TS_TOLERANCE_MS:
            return False
        expected = hashlib.sha256(
            f"{API_KEY}:{body}:{ts_ms}".encode("utf-8")
        ).hexdigest()
        return sig == expected

    def encode_response(self, resp: NormalizedBidResponse) -> WireResponse:
        if resp.is_no_bid:
            return WireResponse(
                status=200,
                headers={"Content-Type": "application/json"},
                body=json.dumps({"nbr": 1}),
            )
        body = json.dumps({
            "request_id": resp.request_id,
            "bids": [
                {"slot_id": b.slot_id, "price": b.price, "creative_id": b.creative_id}
                for b in resp.bids
            ],
        })
        return WireResponse(
            status=200,
            headers={"Content-Type": "application/json"},
            body=body,
        )

    def device_id_for_bidding(self, req: NormalizedBidRequest) -> Optional[str]:
        return req.device_id
