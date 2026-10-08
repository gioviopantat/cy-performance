"""PreToolUse(Bash) guard: ask before anything that can write a real calendar or Strava ride.

Run by ``ask_before_writes.sh``. Prints a permission decision ``ask`` (with a reason) or
nothing (allow). Fails closed: input it cannot parse is an ``ask`` (ADR-0007 §6).

Each shell segment (split on ``;`` ``&&`` ``||`` ``|`` ``&`` and newlines, comments dropped)
is judged on its own, so ``cyp run # --no-write`` or ``cyp run && echo --no-write`` still ask.
"""

from __future__ import annotations

import json
import re
import shlex
import sys
from pathlib import Path

WRITE_PATHS = ("/v1/autopilot", "strava-description", "/plan/commit")
POST_FLAGS = ("-d", "-F", "--form", "--json", "--post-data", "--post-file", "POST", "-XPOST")
WRITE_FUNCS = re.compile(r"\b(run_profile|run_all|publish_plan|strava_write|push_description)\b")
PYTHON = re.compile(r"(^|/)python[0-9.]*$")


def _segments(command: str) -> list[list[str]]:
    try:
        return _shell_segments(command)
    except ValueError:  # unbalanced quotes (e.g. a heredoc body): split crudely, still per segment
        parts = re.split(r"[;&|\n()]+", re.sub(r"(^|\s)#[^\n]*", " ", command))
        return [p.replace('"', " ").replace("'", " ").split() for p in parts if p.strip()]


def _shell_segments(command: str) -> list[list[str]]:
    lexer = shlex.shlex(command.replace("\n", " ; "), posix=True, punctuation_chars=";&|()<>")
    lexer.whitespace_split = True
    out: list[list[str]] = [[]]
    for tok in lexer:
        if tok and set(tok) <= set(";&|()"):
            out.append([])
        else:
            out[-1].append(tok)
    return [s for s in out if s]


def _cyp_args(seg: list[str]) -> list[str] | None:
    for i, tok in enumerate(seg):
        if tok == "cyp" or tok.endswith("/cyp"):
            return seg[i + 1 :]
        if tok == "-m" and i + 1 < len(seg) and seg[i + 1] in ("cyp", "cyp.cli"):
            return seg[i + 2 :]
    return None


def _reason(seg: list[str], whole: str) -> str:
    args = _cyp_args(seg)
    if args is not None:
        if "--confirm-write" in args or "--write" in args:
            return "this cyp command writes to a calendar or a Strava ride"
        if any(a == "promote" for a in args) and "profile" in args:
            return "cyp profile promote lets the autopilot write a real calendar"
        if "schedule" in args and ({"install", "uninstall"} & set(args)):
            return "this changes the daily schedule"
        if "run" in args and "--no-write" not in args:
            return "cyp run writes calendars of profiles in apply mode (add --no-write)"
    http = bool(seg) and Path(seg[0]).name in ("curl", "http", "https", "xh", "wget")
    if (
        http
        and any(p in t for t in seg for p in WRITE_PATHS)
        and any(t in POST_FLAGS or t.startswith(("--data", "-d")) for t in seg[1:])
    ):
        return "this HTTP call can write a calendar or a Strava ride"
    if any(PYTHON.search(t) for t in seg):
        code = " ".join(seg)
        for t in seg:
            if t.endswith(".py") and Path(t).is_file():
                code += Path(t).read_text(encoding="utf-8", errors="replace")
        if WRITE_FUNCS.search(code) or (WRITE_FUNCS.search(whole) and "<<" in whole):
            return "this Python code calls a calendar or Strava write function"
    return ""


def decide(raw: str) -> str:
    """The reason to ask for the hook input ``raw``, or ``""`` to allow."""
    try:
        command = json.loads(raw)["tool_input"]["command"]
        if not isinstance(command, str):
            raise TypeError
        segments = _segments(command)
    except Exception:  # noqa: BLE001 - fail closed on anything unparseable
        return "the write guard could not parse this command"
    for seg in segments:
        reason = _reason(seg, command)
        if reason:
            return reason
    return ""


if __name__ == "__main__":
    why = decide(sys.stdin.read())
    if why:
        print(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "ask",
                        "permissionDecisionReason": why,
                    }
                }
            )
        )
