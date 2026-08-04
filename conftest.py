"""
conftest.py
────────────
Pytest bootstrap for the standalone data_importer repo.

When the package is used inside the Mosaic main repo it lives at
src/data_importer/ and is imported as `src.data_importer.*`.
As a standalone repo, it lives at the project root and is imported
as `data_importer.*`.

This conftest bridges the gap by aliasing `src.data_importer` → `data_importer`
in sys.modules so that internal imports like
`from src.data_importer.base_fetcher import Fetcher`
resolve correctly in the standalone context.
"""

import sys
import types
import os

# Make sure the repo root's parent is in sys.path so `import data_importer` works
_repo_root = os.path.dirname(os.path.abspath(__file__))
_parent = os.path.dirname(_repo_root)
if _parent not in sys.path:
    sys.path.insert(0, _parent)

# Create a virtual `src` namespace package that delegates to this repo root
if "src" not in sys.modules:
    _src = types.ModuleType("src")
    _src.__path__ = [os.path.join(_repo_root, "_nonexistent_")]  # empty namespace
    _src.__package__ = "src"
    sys.modules["src"] = _src

# Register src.data_importer as an alias for this package
import data_importer as _di  # noqa: E402

sys.modules.setdefault("src.data_importer", _di)
# Pre-populate common sub-module aliases so lazy imports resolve first time
for _name in list(sys.modules):
    if _name.startswith("data_importer."):
        sys.modules.setdefault("src." + _name, sys.modules[_name])
