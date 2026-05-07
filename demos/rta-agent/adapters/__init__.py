from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import Dict, Type

from .base import RTAAdapter


def load_adapters() -> Dict[str, Type[RTAAdapter]]:
    """Discover all RTAAdapter subclasses in this package.

    Scans the package directory directly (not pkgutil) so newly-written
    adapter files are picked up without needing a process restart, which
    matters for the agent's "write code, then verify" loop.

    Note: re-fetches RTAAdapter from sys.modules each call so it stays
    correct even if `adapters` was reloaded between calls (otherwise
    issubclass() would compare against a stale base class).
    """
    importlib.invalidate_caches()
    base_mod = importlib.import_module(f"{__name__}.base")
    base_cls = base_mod.RTAAdapter
    registry: Dict[str, Type[RTAAdapter]] = {}
    pkg_dir = Path(__file__).resolve().parent
    for path in sorted(pkg_dir.glob("*.py")):
        if path.name in ("base.py", "__init__.py"):
            continue
        module_name = f"{__name__}.{path.stem}"
        if module_name in sys.modules:
            mod = importlib.reload(sys.modules[module_name])
        else:
            mod = importlib.import_module(module_name)
        for attr in dir(mod):
            obj = getattr(mod, attr)
            if (
                isinstance(obj, type)
                and issubclass(obj, base_cls)
                and obj is not base_cls
            ):
                registry[obj.MEDIA] = obj
    return registry
