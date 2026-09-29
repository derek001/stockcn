"""Trading strategy plugin package."""
from __future__ import annotations

import importlib
import inspect
import pkgutil
from pathlib import Path

from .base import Strategy, get_strategy  # noqa: F401

_REGISTRY: dict[str, Strategy] = {}
_FOUND = False


def discover() -> dict[str, Strategy]:
    """Import every module in this package and collect Strategy subclasses."""
    global _FOUND
    if not _FOUND:
        pkg_dir = Path(__file__).parent
        for m in pkgutil.iter_modules([str(pkg_dir)]):
            if m.name.startswith("_") or m.name == "base":
                continue
            module = importlib.import_module(f"{__name__}.{m.name}")
            for _, cls in inspect.getmembers(module, inspect.isclass):
                if (issubclass(cls, Strategy) and cls is not Strategy
                        and getattr(cls, "id", "")):
                    _REGISTRY[cls.id] = cls()
        _FOUND = True
    return _REGISTRY
