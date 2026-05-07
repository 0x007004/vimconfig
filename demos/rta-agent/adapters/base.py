from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class NormalizedBidRequest:
    """Internal canonical bid request shape, decoupled from any media format."""

    request_id: str
    slot_ids: List[str]
    device_id: Optional[str]
    device_id_kind: Optional[str]
    floor_price: float
    deadline_ms: int
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class NormalizedBid:
    slot_id: str
    price: float
    creative_id: str


@dataclass
class NormalizedBidResponse:
    request_id: str
    bids: List[NormalizedBid]

    @property
    def is_no_bid(self) -> bool:
        return len(self.bids) == 0


@dataclass
class WireResponse:
    """What the adapter returns to the caller of handle()."""

    status: int
    headers: Dict[str, str]
    body: Optional[str]


class RTAAdapter(ABC):
    """One adapter per media platform.

    Each subclass MUST set MEDIA and implement the four abstract methods.
    """

    MEDIA: str = "<override-me>"
    LATENCY_BUDGET_MS: int = 100

    @abstractmethod
    def parse_request(self, body: str, headers: Dict[str, str]) -> NormalizedBidRequest:
        """Parse media-specific JSON into our canonical shape."""

    @abstractmethod
    def verify_signature(self, body: str, headers: Dict[str, str]) -> bool:
        """Return True iff signature is valid. Implementations should be
        constant-time where possible."""

    @abstractmethod
    def encode_response(self, resp: NormalizedBidResponse) -> WireResponse:
        """Encode our canonical response into the media's expected wire format,
        including the no-bid case."""

    @abstractmethod
    def device_id_for_bidding(self, req: NormalizedBidRequest) -> Optional[str]:
        """The id used to look up audience/frequency. None means do-not-bid."""

    def health_check_request(self) -> Tuple[str, str, Dict[str, str]]:
        """Return (method, url, headers) for a health probe. Optional."""
        return ("GET", "/health", {})
