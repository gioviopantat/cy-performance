#!/bin/sh
# PreToolUse(Bash): ask before anything that can write a real calendar, a Strava ride or the
# daily schedule (ADR-0007 §6). Logic in ask_before_writes.py; without python3 it fails closed.
here=$(dirname "$0")
if command -v python3 >/dev/null 2>&1; then
  exec python3 "$here/ask_before_writes.py"
fi
cat >/dev/null
printf '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"ask","permissionDecisionReason":"python3 missing: the write guard cannot check this command"}}\n'
