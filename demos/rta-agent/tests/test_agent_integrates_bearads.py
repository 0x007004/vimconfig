"""End-to-end test: agent integrates BearAds from scratch and passes verify.

This is the demo's headline test. It deletes any existing bearads adapter,
runs the full agent loop in template (no-LLM) mode, and asserts:

  - a new adapter file appears
  - verify passes on it
  - at least one new skill is sunk to skills/

If you break this, the demo no longer demonstrates the loop.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import agent  # noqa: E402
from tools.verify import verify_media  # noqa: E402


BEAR_ADAPTER = ROOT / "adapters" / "bearads.py"
SKILLS_DIR = ROOT / "skills"
ASKS_DIR = ROOT / "asks"


def _snapshot(dir_: Path) -> set[str]:
    if not dir_.exists():
        return set()
    return {p.name for p in dir_.glob("*.md") if p.name != "README.md"}


def test_agent_integrates_bearads_end_to_end() -> None:
    if BEAR_ADAPTER.exists():
        BEAR_ADAPTER.unlink()
    for stale in SKILLS_DIR.glob("rta.bearads.*.md"):
        stale.unlink()
    skills_before = _snapshot(SKILLS_DIR)
    if ASKS_DIR.exists():
        shutil.rmtree(ASKS_DIR)

    rc = agent.integrate("bearads", use_llm=False, max_asks=4)
    assert rc == 0, f"agent.integrate returned non-zero: {rc}"

    assert BEAR_ADAPTER.exists(), "agent did not write adapters/bearads.py"

    report = verify_media("bearads")
    assert report.passed, report.to_json()

    skills_after = _snapshot(SKILLS_DIR)
    new = skills_after - skills_before
    assert new, "agent did not sink any new skills"

    assert ASKS_DIR.exists() and any(ASKS_DIR.iterdir()), (
        "agent did not record any expert asks"
    )
