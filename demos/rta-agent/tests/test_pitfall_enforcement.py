"""Regression: a deliberately-buggy adapter must FAIL verify.

We swap in a wrong no-bid format and a wrong price unit for BearAds and
check that the sensors catch each one. This is the demo's proof that
Sensors actually do their job — not just that they pass on the happy path.
"""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools.verify import verify_media  # noqa: E402


BEAR_ADAPTER = ROOT / "adapters" / "bearads.py"


BUGGY_BODY = '''"""BearAds buggy adapter — should fail verify (regression fixture)."""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Dict, Optional

from .base import (
    NormalizedBidRequest,
    NormalizedBidResponse,
    RTAAdapter,
    WireResponse,
)


SIGNING_SECRET = b"bear-signing-secret-demo"
BEARER_TOKEN = "bear-bearer-token-demo"


class BearadsAdapter(RTAAdapter):
    MEDIA = "bearads"
    LATENCY_BUDGET_MS = 40

    def parse_request(self, body, headers):
        data = json.loads(body)
        slots = data.get("ad_slots", [])
        device = data.get("device", {})
        return NormalizedBidRequest(
            request_id=data["id"],
            slot_ids=[s["slot_id"] for s in slots],
            device_id=device.get("oaid_sha256"),
            device_id_kind="oaid_sha256",
            floor_price=float(slots[0].get("floor", 0.0)) if slots else 0.0,
            deadline_ms=int(data.get("tmax_ms", 80)),
            raw=data,
        )

    def verify_signature(self, body, headers):
        sig = headers.get("X-Bear-Sign", "")
        nonce = headers.get("X-Bear-Nonce", "")
        bearer = headers.get("X-Bear-Auth", "")
        if not sig or not nonce or bearer != BEARER_TOKEN:
            return False
        body_sha = hashlib.sha256(body.encode("utf-8")).hexdigest()
        expected = hmac.new(
            SIGNING_SECRET,
            f"{nonce}.{body_sha}".encode("utf-8"),
            hashlib.sha512,
        ).hexdigest()
        return hmac.compare_digest(sig, expected)

    def encode_response(self, resp):
        if resp.is_no_bid:
            return WireResponse(status=204, headers={}, body=None)
        body = json.dumps({
            "id": resp.request_id,
            "bids": [
                {"slot_id": b.slot_id, "price": b.price, "creative_id": b.creative_id}
                for b in resp.bids
            ],
        })
        return WireResponse(
            status=200, headers={"Content-Type": "application/json"}, body=body,
        )

    def device_id_for_bidding(self, req):
        return req.device_id
'''


def test_buggy_adapter_is_caught_by_sensors() -> None:
    original = BEAR_ADAPTER.read_text(encoding="utf-8") if BEAR_ADAPTER.exists() else None
    try:
        BEAR_ADAPTER.write_text(textwrap.dedent(BUGGY_BODY), encoding="utf-8")
        report = verify_media("bearads")
        assert not report.passed, "buggy adapter should not pass verify"

        names = [c.name for c in report.checks if not c.passed]
        assert "sandbox_replay" in names or "reconcile_parity" in names, (
            f"expected sandbox_replay or reconcile_parity to fail; got {names}"
        )
    finally:
        if original is not None:
            BEAR_ADAPTER.write_text(original, encoding="utf-8")
        elif BEAR_ADAPTER.exists():
            BEAR_ADAPTER.unlink()
