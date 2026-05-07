"""The two reference adapters must always pass verify.

If this test ever fails, something has regressed in the harness itself.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools.verify import verify_media


def test_foxads_reference_passes_verify() -> None:
    report = verify_media("foxads")
    assert report.passed, report.to_json()


def test_owlads_reference_passes_verify() -> None:
    report = verify_media("owlads")
    assert report.passed, report.to_json()
