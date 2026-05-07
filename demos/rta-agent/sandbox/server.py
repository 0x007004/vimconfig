"""In-process mock RTA media servers.

This stands in for the real media-side sandbox endpoints, so the agent's
verification step can run end-to-end without any network or credentials.

Each media here mirrors ONE published protocol from protocols/<media>.md.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

from adapters.foxads import SECRET as FOX_SECRET
from adapters.owlads import API_KEY as OWL_API_KEY


BEAR_TOKEN = "bearads-bearer-token-demo"
BEAR_SIGNING_SECRET = b"bearads-signing-secret-demo"


@dataclass
class MockRequest:
    body: str
    headers: Dict[str, str]


@dataclass
class MockResponse:
    status: int
    body: Optional[str]
    headers: Dict[str, str]


def _build_foxads_request(slot: str, request_id: str = "fox-req-1") -> MockRequest:
    body = json.dumps({
        "id": request_id,
        "imp": [{"id": "1", "tagid": slot}],
        "device": {"oaid": "oaid-fox-001", "androidid": "aid-001", "ua": "demo"},
        "user": {"uid": "fox-user-1"},
        "at": 2,
        "tmax": 100,
    })
    ts = str(int(time.time()))
    sig = hmac.new(
        FOX_SECRET, f"{body}.{ts}".encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return MockRequest(
        body=body,
        headers={"X-FoxAds-Sign": sig, "X-FoxAds-Timestamp": ts},
    )


def _build_owlads_request(slot: str, request_id: str = "owl-req-1") -> MockRequest:
    body = json.dumps({
        "request_id": request_id,
        "slots": [{"slot_id": slot, "floor_price": 0.5}],
        "device_info": {
            "oaid_md5": hashlib.md5(b"oaid-owl-001").hexdigest(),
            "android_id_md5": hashlib.md5(b"aid-001").hexdigest(),
            "platform": "android",
        },
        "geo": {"country": "CN", "region": "31"},
        "deadline_ms": 80,
    })
    ts_ms = str(int(time.time() * 1000))
    sig = hashlib.sha256(f"{OWL_API_KEY}:{body}:{ts_ms}".encode("utf-8")).hexdigest()
    return MockRequest(
        body=body,
        headers={
            "Authorization": f"OwlAds {OWL_API_KEY}",
            "X-Sig": sig,
            "X-Ts": ts_ms,
        },
    )


def _build_bearads_request(slot: str, request_id: str = "bear-req-1") -> MockRequest:
    body = json.dumps({
        "id": request_id,
        "ad_slots": [{"slot_id": slot, "floor": 0.8, "size": "640x960"}],
        "device": {
            "oaid_sha256": hashlib.sha256(b"oaid-bear-001").hexdigest(),
            "platform": "android",
            "model": "Mi 14",
        },
        "ip": "203.0.113.42",
        "tmax_ms": 80,
    })
    nonce = secrets.token_hex(8)
    body_sha = hashlib.sha256(body.encode("utf-8")).hexdigest()
    sig = hmac.new(
        BEAR_SIGNING_SECRET,
        f"{nonce}.{body_sha}".encode("utf-8"),
        hashlib.sha512,
    ).hexdigest()
    return MockRequest(
        body=body,
        headers={
            "X-Bear-Auth": BEAR_TOKEN,
            "X-Bear-Nonce": nonce,
            "X-Bear-Sign": sig,
        },
    )


REQUEST_BUILDERS: Dict[str, Callable[[str, str], MockRequest]] = {
    "foxads": _build_foxads_request,
    "owlads": _build_owlads_request,
    "bearads": _build_bearads_request,
}


def build_replay_corpus(media: str, n: int = 100, no_bid_ratio: float = 0.5) -> List[Tuple[MockRequest, bool]]:
    """Return [(request, expect_bid)] pairs for replay testing.

    `expect_bid` is the expected adapter decision for this fixture; we use it to
    drive the response shape (no-bid vs bid) so encode_response is exercised
    on both branches.
    """
    builder = REQUEST_BUILDERS[media]
    out: List[Tuple[MockRequest, bool]] = []
    for i in range(n):
        slot = f"slot-{i % 5}"
        req = builder(slot, f"{media}-replay-{i:03d}")
        expect_bid = (i / n) >= no_bid_ratio
        out.append((req, expect_bid))
    return out


def validate_wire_response(media: str, status: int, body: Optional[str], request_id: str, is_no_bid: bool) -> Tuple[bool, str]:
    """Server-side validator: would the real media accept this wire response?

    This is what makes the demo non-trivial — we encode each media's actual
    quirks (P-001 from pitfalls.md) so a wrong adapter implementation gets
    caught here, not in production.
    """
    if media == "foxads":
        if is_no_bid:
            if status != 204 or body not in (None, ""):
                return False, "FoxAds no-bid must be HTTP 204 with empty body (P-001)"
        else:
            if status != 200:
                return False, f"FoxAds bid expected 200, got {status}"
            try:
                json.loads(body or "")
            except Exception as e:
                return False, f"FoxAds bid response body is not JSON: {e}"
        return True, "ok"

    if media == "owlads":
        if status != 200:
            return False, f"OwlAds always returns 200, got {status}"
        try:
            payload = json.loads(body or "")
        except Exception as e:
            return False, f"OwlAds body is not JSON: {e}"
        if is_no_bid and payload.get("nbr") != 1:
            return False, "OwlAds no-bid must contain {\"nbr\":1} (P-001)"
        return True, "ok"

    if media == "bearads":
        if status != 200:
            return False, f"BearAds always returns 200, got {status}"
        try:
            payload = json.loads(body or "")
        except Exception as e:
            return False, f"BearAds body is not JSON: {e}"
        if payload.get("id") != request_id:
            return False, "BearAds response MUST echo request id (P-001)"
        if is_no_bid:
            if payload.get("nbr") != 0:
                return False, "BearAds no-bid must include {\"nbr\":0}"
        else:
            bids = payload.get("bids") or []
            if not bids:
                return False, "BearAds bid response must include non-empty bids[]"
            for b in bids:
                if "price_cents" not in b:
                    return False, "BearAds uses price_cents (integer), not price"
                if not isinstance(b["price_cents"], int):
                    return False, "BearAds price_cents must be integer cents, not float"
        return True, "ok"

    return False, f"Unknown media {media}"
