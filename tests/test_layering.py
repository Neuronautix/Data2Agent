"""The dependency arrows point one way, and only one way.

`ingest` and `evidence` are the core; `profiles` sit on the core; `mcp` sits on
both. If the core ever depends on a profile, `structured` stops being a control
condition and the benchmark starts measuring our plumbing instead of the models.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "data2agent"

# layer -> packages it must never import
FORBIDDEN = {
    "ingest": {"profiles", "mcp"},
    "evidence": {"profiles", "mcp"},
    "profiles": {"mcp"},
}


def _imported_roots(path: Path) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom):
            # Relative imports resolve inside data2agent, so the first component
            # of the module path is the sibling package being reached for.
            roots.add((node.module or "").split(".")[0])
        elif isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[-1] for alias in node.names)
    return roots


@pytest.mark.parametrize("layer", sorted(FORBIDDEN))
def test_layer_does_not_reach_upwards(layer: str):
    banned = FORBIDDEN[layer]
    offenders = [
        f"{path.relative_to(SRC)} imports {sorted(_imported_roots(path) & banned)}"
        for path in sorted((SRC / layer).rglob("*.py"))
        if _imported_roots(path) & banned
    ]
    assert not offenders, "layering violation: " + "; ".join(offenders)


def _third_party_roots(path: Path) -> set[str]:
    """Absolute, non-stdlib imports. Relative imports stay inside the package."""
    import sys

    stdlib = set(sys.stdlib_module_names)
    roots: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom):
            if node.level:  # relative: a sibling module, not a dependency
                continue
            names = {(node.module or "").split(".")[0]}
        elif isinstance(node, ast.Import):
            names = {alias.name.split(".")[0] for alias in node.names}
        else:
            continue
        roots |= {name for name in names if name and name not in stdlib and name != "data2agent"}
    return roots


def test_the_ingest_core_uses_only_the_standard_library():
    """Ten-year reproducibility: a dataset must re-ingest from a bare CPython."""
    offenders = [
        f"{path.relative_to(SRC)}: {sorted(_third_party_roots(path))}"
        for path in sorted((SRC / "ingest").rglob("*.py"))
        if _third_party_roots(path)
    ]
    assert not offenders, "third-party imports in the deterministic core: " + "; ".join(offenders)
