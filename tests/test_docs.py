"""Docs cannot drift from code (ADR-0007).

Checked across README.md, CLAUDE.md, docs/**/*.md and .claude/skills/*/SKILL.md:
layer tables, the feature table (ids, defaults, requirements), repo paths, Markdown links,
every ``cyp`` command line (subcommands and options must exist) and ``docs/NN §N`` references.
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path
from typing import Any

import typer

from cyp.cli import app
from cyp.core.features import REGISTRY
from tests.test_layering import LAYERS
from tests.test_privacy import FORBIDDEN_DIRS, FORBIDDEN_FILES

ROOT = Path(__file__).resolve().parents[1]
DOCS = sorted(
    [
        ROOT / "README.md",
        ROOT / "CLAUDE.md",
        *list((ROOT / "docs").rglob("*.md")),
        *list((ROOT / ".claude" / "skills").rglob("SKILL.md")),
    ]
)
GLOBAL_OPTIONS = {"--profile": True, "--version": False, "--help": False}


def _rel(p: Path) -> str:
    return p.relative_to(ROOT).as_posix()


# ----------------------------------------------------------------------------- layers


def _layers_from_table(text: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for m in re.finditer(r"^\| (\d) \| (.+?) \|$", text, re.M):
        for pkg in re.findall(r"`(\w+)`", m.group(2)):
            out[pkg] = int(m.group(1))
    return out


def test_architecture_layer_table_matches_layering_test() -> None:
    doc = (ROOT / "docs" / "01-architecture.md").read_text(encoding="utf-8")
    assert _layers_from_table(doc) == LAYERS, "update docs/01 §5.7.1 or tests/test_layering.py"


def test_claude_md_layer_line_matches() -> None:
    text = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    block = text.split("## Layers", 1)[1].split("##", 1)[0]
    found: dict[str, int] = {}
    for m in re.finditer(r"(\d) ((?:`\w+` ?)+)", block):
        for pkg in re.findall(r"`(\w+)`", m.group(2)):
            found[pkg] = int(m.group(1))
    assert found == LAYERS, "update the Layers line in CLAUDE.md"


# ---------------------------------------------------------------------------- features


def test_feature_table_matches_registry() -> None:
    doc = (ROOT / "docs" / "09-features.md").read_text(encoding="utf-8")
    rows = {
        m.group(1): (m.group(2).strip(), set(re.findall(r"`([\w.]+)`", m.group(3))))
        for m in re.finditer(r"^\| `([\w.]+)` \| (on|off) \|([^|]*)\|", doc, re.M)
    }
    expected = {f.id: ("on" if f.default else "off", set(f.requires)) for f in REGISTRY}
    assert rows == expected, "docs/09-features.md table != core/features.py (id/default/requires)"


# ------------------------------------------------------------------------- paths/links


def test_paths_and_links_resolve() -> None:
    missing = []
    for doc in DOCS:
        text = doc.read_text(encoding="utf-8")
        for path in re.findall(r"`((?:src|docs|tests|\.claude|config)/[\w./-]+)`", text):
            private = path in FORBIDDEN_FILES or path.startswith(FORBIDDEN_DIRS)
            if "<" not in path and not private and not (ROOT / path.rstrip("/")).exists():
                missing.append(f"{_rel(doc)}: {path}")
        for link in re.findall(r"\]\(([^)\s]+)\)", text):
            target = link.split("#", 1)[0]
            if not target or "://" in target or target.startswith("mailto:"):
                continue
            if not (doc.parent / target).exists():
                missing.append(f"{_rel(doc)}: link {link}")
    assert not missing, missing


def test_section_references_exist() -> None:
    bad = []
    for doc in DOCS:
        text = doc.read_text(encoding="utf-8")
        for num, sec in re.findall(r"docs/(\d\d)[\w-]*(?:\.md)? §(\d+)", text):
            target = next((ROOT / "docs").glob(f"{num}-*.md"), None)
            heads = target.read_text(encoding="utf-8") if target else ""
            if not re.search(rf"^##+ {sec}[.\s]", heads, re.M):
                bad.append(f"{_rel(doc)}: docs/{num} §{sec}")
    assert not bad, bad


def test_agents_md_points_at_claude_md() -> None:
    agents = ROOT / "AGENTS.md"
    assert agents.is_symlink() and agents.resolve() == (ROOT / "CLAUDE.md").resolve()


# ----------------------------------------------------------------------------- commands


def _command_lines(text: str) -> list[str]:
    lines = re.findall(r"`((?:uv run )?cyp [^`]+)`", text)
    for block in re.findall(r"```(?:bash|sh)?\n(.*?)```", text, re.S):
        lines += [ln for ln in block.splitlines() if re.match(r"\s*(uv run )?cyp ", ln)]
    return lines


def _check(line: str) -> str | None:
    line = re.sub(r"\s+#.*$", "", line.strip())  # drop shell comments
    line = re.sub(r"[\[\]]", " ", line).replace("...", " ").replace("…", " ")
    try:
        tokens = shlex.split(line.split("|")[0])
    except ValueError:
        return None
    tokens = tokens[tokens.index("cyp") + 1 :]
    cmd: Any = typer.main.get_command(app)
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok in GLOBAL_OPTIONS:
            i += 2 if GLOBAL_OPTIONS[tok] else 1
            continue
        if hasattr(cmd, "commands") and not tok.startswith("-"):
            if "<" in tok or tok not in cmd.commands:
                return None if "<" in tok else f"unknown command {tok!r}"
            cmd = cmd.commands[tok]
            i += 1
            continue
        break
    known = {
        o for p in cmd.params for o in (*getattr(p, "opts", ()), *getattr(p, "secondary_opts", ()))
    } | {"--help"}
    for tok in tokens[i:]:
        if tok.startswith("--"):
            name = tok.split("=", 1)[0]
            if "<" not in name and name not in known:
                return f"unknown option {name!r} for {cmd.name!r}"
    return None


def test_documented_cyp_commands_exist() -> None:
    bad = []
    # Accepted ADRs are historical records (never rewritten): their decision text may name
    # commands that were since replaced; their amendments say so.
    for doc in (d for d in DOCS if "adr" not in d.relative_to(ROOT).parts):
        for line in _command_lines(doc.read_text(encoding="utf-8")):
            problem = _check(line)
            if problem:
                bad.append(f"{_rel(doc)}: `{line.strip()}`: {problem}")
    assert not bad, "\n".join(bad)
