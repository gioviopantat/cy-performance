# Plan needs review

Guardrails (docs/05 §5) could not be satisfied, so the autopilot published a diff only.

1. `uv run cyp --profile <name> plan --dry-run` lists `NEEDS REVIEW <rule> <date>: <detail>`.
2. Usual causes: availability too low for the week's TSS target, a LIMITED note in the icu
   calendar, an FTP change mid-block. Adjust `availability` in the profile's `athlete.yaml` or the
   calendar note, then `uv run cyp --profile <name> run --no-write`.
3. Never bypass by forcing `--apply`: the guardrails are the safety net (ADR-0004).
