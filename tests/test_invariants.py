"""CLAUDE.md invariants that are not covered elsewhere (ADR-0007 §3)."""

from __future__ import annotations

import ast
import json
import socket
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "cyp"

#: The only modules allowed to call ``resolve_features`` directly (ADR-0008 amendment);
#: everything else asks ``ctx.features()``.
FLAG_READERS = {"settings.py", "services/context.py", "cli/profiles.py", "jobs/runner.py"}


def test_flags_are_read_only_through_sanctioned_functions() -> None:
    offenders = []
    for path in SRC.rglob("*.py"):
        rel = path.relative_to(SRC).as_posix()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call):
                name = getattr(node.func, "id", getattr(node.func, "attr", ""))
                if name == "resolve_features" and rel not in FLAG_READERS:
                    offenders.append(rel)
    assert not offenders, f"use ctx.features() instead of resolve_features in {offenders}"


def test_tests_cannot_open_sockets() -> None:
    with pytest.raises(RuntimeError, match="network"):
        socket.create_connection(("192.0.2.1", 80), timeout=0.1)  # TEST-NET-1


@pytest.mark.parametrize(
    ("command", "asks"),
    [
        ("uv run cyp run --all", True),
        ("uv run cyp --profile dad run", True),
        ("python -m cyp.cli run --all", True),
        ("uv run cyp --profile dad plan --apply --confirm-write", True),
        ("CYP_PROFILE=dad uv run cyp publish spike --date 2030-01-01 --confirm-write", True),
        ("uv run cyp profile promote dad --yes", True),
        ("uv run cyp schedule install", True),
        ("uv run cyp --profile dad run --no-write", False),
        ("uv run cyp profile list", False),
        ("uv run cyp schedule status", False),
        ("uv run poe check", False),
        # bypasses found in review: --no-write must belong to the same `cyp run`
        ("uv run cyp run --json # --no-write", True),
        ("uv run cyp run && echo --no-write", True),
        ("uv run cyp run --no-write && uv run cyp run --all", True),
        ("uv run cyp --profile dad run --no-write | tail -5", False),
        ("uv run cyp ride-log push 1 --text-file x.txt --confirm-write", True),
        # the HTTP write endpoints
        ('curl -X POST localhost:8765/v1/autopilot -d \'{"write":true,"confirm":true}\'', True),
        (
            "curl -s localhost:8765/v1/activities/1/strava-description "
            "-H 'Content-Type: application/json' --data '{\"confirm\":true}'",
            True,
        ),
        ("curl -s localhost:8765/v1/autopilot/runs", False),
        ("curl -s localhost:8765/v1/meta", False),
        # Python that calls the write functions
        ("uv run python -c 'from cyp.jobs.runner import run_profile; run_profile(1)'", True),
        ("uv run python -c 'import cyp; print(cyp.__version__)'", False),
        ("python3 - <<'EOF'\nfrom cyp.services.publish import publish_plan\nEOF", True),
        ("grep -rn run_profile src", False),
        ('git commit -m "fix: don\'t run cyp run without care"', False),
    ],
)
def test_claude_hook_asks_before_calendar_writes(command: str, asks: bool) -> None:
    hook = ROOT / ".claude" / "hooks" / "ask_before_writes.sh"
    out = subprocess.run(
        ["sh", str(hook)],
        input=json.dumps({"tool_input": {"command": command}}),
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    decision = json.loads(out)["hookSpecificOutput"]["permissionDecision"] if out else None
    assert (decision == "ask") is asks


def test_claude_hook_fails_closed_on_bad_input() -> None:
    hook = ROOT / ".claude" / "hooks" / "ask_before_writes.sh"
    out = subprocess.run(
        ["sh", str(hook)], input="not json", capture_output=True, text=True, check=True
    ).stdout
    assert json.loads(out)["hookSpecificOutput"]["permissionDecision"] == "ask"
