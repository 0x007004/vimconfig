"""Workspace vs agent path split.

The demo distinguishes two roots:

  AGENT_ROOT      — where the agent CODE lives (this repo's tools/, agent.py).
                    Always derived from __file__; not user-configurable.
  WORKSPACE_ROOT  — where the OPERATED-ON codebase lives. Reads/writes
                    happen here: protocols/, adapters/, skills/, sandbox/,
                    asks/, experts.yaml, pitfalls.md, policy.yaml, CLAUDE.md.

By default WORKSPACE_ROOT == AGENT_ROOT (i.e. the demo bundles both for
ease of learning). Set RTA_WORKSPACE_ROOT to point the agent at a real
codebase:

    export RTA_WORKSPACE_ROOT=/path/to/your-rta-gateway
    python /path/to/demos/rta-agent/agent.py integrate <media>

The agent will then read/write inside your repo, while the agent code
itself stays where you cloned it.

Recommended layout for a real adoption (selección A in the conversation):

    your-rta-gateway/                ← RTA_WORKSPACE_ROOT
    ├── adapters/                    ← already exists
    ├── protocols/                   ← agent reads
    ├── skills/                      ← agent reads + writes
    ├── sandbox/                     ← your sandbox/mock
    ├── CLAUDE.md, experts.yaml, pitfalls.md, policy.yaml
    └── .agent/                      ← copy this directory's tools/ + agent.py here
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


AGENT_ROOT: Path = Path(__file__).resolve().parent.parent

_workspace_env = os.environ.get("RTA_WORKSPACE_ROOT")
WORKSPACE_ROOT: Path = (
    Path(_workspace_env).expanduser().resolve() if _workspace_env else AGENT_ROOT
)

PROTOCOLS = WORKSPACE_ROOT / "protocols"
ADAPTERS = WORKSPACE_ROOT / "adapters"
SKILLS = WORKSPACE_ROOT / "skills"
SANDBOX = WORKSPACE_ROOT / "sandbox"
ASKS = WORKSPACE_ROOT / "asks"
EXPERTS = WORKSPACE_ROOT / "experts.yaml"
PITFALLS = WORKSPACE_ROOT / "pitfalls.md"
POLICY = WORKSPACE_ROOT / "policy.yaml"
CLAUDE_MD = WORKSPACE_ROOT / "CLAUDE.md"


def setup_import_paths() -> None:
    """Make `tools.*` and the workspace's `adapters` / `sandbox` importable.

    Order matters:
      1. AGENT_ROOT first  — `tools.*` always resolves to OUR tools, even if
                             the workspace happens to have a tools/ directory.
      2. WORKSPACE_ROOT    — `adapters` and `sandbox` resolve to the user's
                             real codebase when WORKSPACE_ROOT is overridden.
    """
    agent_str = str(AGENT_ROOT)
    if agent_str not in sys.path:
        sys.path.insert(0, agent_str)
    workspace_str = str(WORKSPACE_ROOT)
    if workspace_str != agent_str and workspace_str not in sys.path:
        sys.path.insert(1, workspace_str)


def is_external_workspace() -> bool:
    return WORKSPACE_ROOT != AGENT_ROOT


def describe() -> str:
    return (
        f"AGENT_ROOT      = {AGENT_ROOT}\n"
        f"WORKSPACE_ROOT  = {WORKSPACE_ROOT}"
        f"  ({'external' if is_external_workspace() else 'demo bundled'})"
    )
