# ADR-0008 · Feature flags: one registry, resolved per profile

**Status**: accepted · 2026-10-07

## Context
Capabilities will keep growing (weather-aware indoor/outdoor, LLM narration, notifications,
Strava, new report types). Different athletes need different subsets (the owner uses Strava, a
second athlete does not), and a new capability must be switchable off without a code change
when it misbehaves. Ad-hoc env switches (`STRAVA_ENABLED`, `CYP_LLM_ENABLED`) do not scale and
are invisible to an agent reading the code.

## Decision
1. **One registry**: `cyp/core/features.py` `REGISTRY` lists every flag with id
   (`<stage>.<name>`), stage, default, zh-TW title, summary and `requires`. Pure, layer 0.
2. **Per-profile values** in `athlete.yaml` → `features: {id: bool}`; unknown ids fail
   validation (typos never silently read as "off").
3. **Resolution order**: registry default → legacy env (`STRAVA_ENABLED=false`) →
   `athlete.yaml` → `CYP_FEATURES="id=on,id=off"` (one-off runs, debugging). A flag whose
   `requires` are off is off, and `cyp features` says why.
4. **One way to ask**: `ctx.features().enabled("id")` (unknown id raises). Lower layers that
   predate flags receive plain settings via `settings.with_feature_switches()`.
5. **Documented**: every id appears in [docs/09-features.md](../09-features.md);
   `tests/test_docs.py` fails otherwise.
6. **Every new optional capability ships behind a flag** (default chosen deliberately), and the
   autopilot stage list is driven by flags, not by `if` chains on settings.

## Consequences
- `cyp features` / `cyp profile show` explain exactly what a run will do and why.
- Turning a misbehaving capability off is a one-line YAML edit (or `CYP_FEATURES` for one run).
- Flags are not a permission system: writing to a calendar still needs `planner.mode: apply`
  and the write guard (ADR-0006).

## Amendment (2026-10-07)
- Read paths: code asks `ctx.features()`. Three sanctioned direct callers of
  `settings.resolve_features()` exist (enforced by `tests/test_invariants.py`): the bridge
  `settings.with_feature_switches()`, read-only listings in `cli/profiles.py`, and
  `jobs/runner.notify_failure()` (which must work when athlete.yaml is broken).
- Scope: flags drive the autopilot (`cyp run`) and `publish_plan`. Explicit one-off commands
  (`cyp analyze`, `cyp daily`, `cyp report`) run what they are asked to, except that every sync
  honours `sync.strava` and every publish honours `publish.calendar`.
- `notify.macos` defaults to on; `CYP_LLM_ENABLED` was removed (the narrator will get a flag).
