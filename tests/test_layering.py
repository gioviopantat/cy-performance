"""Architecture guard: a package may import only from its own layer or lower ones.

Layers (low -> high). Keeping this order acyclic is what keeps the code maintainable:
pure domain at the bottom, I/O in the middle, entry points at the top.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "cyp"

LAYERS: dict[str, int] = {
    "core": 0,
    "settings": 1,
    "logging": 1,
    "schemas": 1,
    "profiles": 1,
    "store": 2,
    "dataset": 3,
    "ingest": 3,
    "analysis": 4,
    "planning": 5,
    "reports": 5,
    "publish": 6,
    "jobs": 7,
    "services": 7,
    "llm": 7,
    "cli": 8,
    "api": 8,
    "devtools": 8,
}


def _package(rel: Path) -> str:
    return rel.parts[0].removesuffix(".py")


def _imports(path: Path) -> set[str]:
    out: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("cyp."):
            out.add(node.module.split(".")[1])
        elif isinstance(node, ast.Import):
            out |= {a.name.split(".")[1] for a in node.names if a.name.startswith("cyp.")}
    return out


def test_every_package_has_a_layer() -> None:
    pkgs = {_package(p.relative_to(SRC)) for p in SRC.rglob("*.py")} - {"__init__", "__main__"}
    assert pkgs <= set(LAYERS), f"add to LAYERS: {sorted(pkgs - set(LAYERS))}"


def test_no_upward_imports() -> None:
    bad: list[str] = []
    for path in SRC.rglob("*.py"):
        rel = path.relative_to(SRC)
        me = _package(rel)
        if me not in LAYERS:
            continue
        for dep in _imports(path):
            if dep in LAYERS and dep != me and LAYERS[dep] > LAYERS[me]:
                bad.append(f"{rel} ({me}, L{LAYERS[me]}) imports {dep} (L{LAYERS[dep]})")
    assert not bad, "upward imports:\n" + "\n".join(sorted(bad))
