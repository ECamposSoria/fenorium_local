"""Reusable live-data clients for Par research and the Phase 2 engine."""

from importlib import import_module
from pathlib import Path
import sys

# Several clients read the chain registry in `engine/`. The research scripts import this package
# as `par_sources` with only `research/` on the path, so the repository root is added here, once,
# for every one of them.
_ROOT = str(Path(__file__).resolve().parents[2])
if _ROOT not in sys.path:
    sys.path.append(_ROOT)

__all__ = ["CacheMissError", "HttpClient", "Instrument", "QualityTier", "SourceError"]


def __getattr__(name: str):
    if name in ("CacheMissError", "HttpClient", "SourceError"):
        module = import_module(".http", __name__)
    elif name in ("Instrument", "QualityTier"):
        module = import_module(".normalize", __name__)
    else:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(module, name)
    globals()[name] = value
    return value
