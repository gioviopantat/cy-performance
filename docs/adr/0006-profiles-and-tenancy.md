# ADR-0006 · Profiles: one engine, one workspace per athlete; tenancy as a layer on top

**Status**: accepted · 2026-10-07

## Context
A second athlete (the owner's father) should get the same daily autopilot: sync → analyse →
plan → write to his intervals.icu calendar → Garmin. He should install nothing and never see a
terminal; intervals.icu + his head unit are his whole UI. The owner's Mac is always on.
More athletes (friends, a club) are plausible later, but today there are two.

The code is single-athlete by construction: `Settings` reads one `.env`, `AppContext` holds one
DB engine + one `athlete.yaml`, and `dataset.CACHE` is process-wide. The schema is only partly
athlete-scoped (`activities`, `goals`, `planned_workouts`… have `athlete_id`; `activity_metrics`,
`blocks`, `week_plans`, `publish_log`, `sync_cursors`… do not). A shared multi-tenant DB would
touch every layer from `store` to `services`.

## Decision
Tenancy is added in layers *above* the engine; the engine (core → publish) stays single-athlete.

```
L4  Tenant service (future)   cyp serve: sign-up, intervals.icu OAuth, profile registry in a DB
L3  Scheduler                 launchd (macOS) → `cyp run --all` at 05:30 local, catch-up on wake
L2  Runner                    `cyp run [--profile P | --all]`: one subprocess per profile,
                              per-profile lock, failures isolated, summary + exit code
L1  Profile                   profiles/<slug>/  = .env (secrets) + athlete.yaml + data/
L0  Engine (unchanged)        AppContext(settings, athlete_config) → services → store/planning/publish
```

1. **Profile = workspace directory.** `profiles/<slug>/{.env, athlete.yaml, data/}`. Selected by
   `--profile <slug>` or `CYP_PROFILE`. Resolution builds `Settings` from the root `.env`
   (shared defaults: timezone, LLM key) overlaid by `profiles/<slug>/.env`, with
   `CYP_DATA_DIR`/`CYP_DB_URL` forced inside the profile. No profile selected = today's layout
   (`.env`, `config/athlete.yaml`, `data/`) so nothing breaks; `cyp profile adopt <slug>` moves it.
2. **A `ProfileStore` protocol** (`list`, `get`, `create`) with a `FilesystemProfileStore` today.
   Everything above L1 addresses profiles only through it, so L4 can swap in a DB-backed store.
3. **Runner isolates by process.** `run --all` spawns `cyp run --profile <slug>` per profile, so
   `dataset.CACHE`, engines and logging never mix and one crash cannot affect the other athlete.
4. **Write guard.** Each profile records the icu athlete id its key resolved to on creation.
   Publishing refuses when the key now resolves to a different athlete (never write one
   person's plan onto another person's calendar).
5. **Per-profile write mode.** `athlete.yaml: planner.mode` stays the switch. New profiles start
   in `propose`; `cyp profile promote <slug>` flips to `apply` after the owner reviews a week.
   The scheduled runner passes the confirmation flag only for profiles in `apply`.
6. **Onboarding asks only what icu cannot answer.** `cyp profile add <slug>`: API key (validated
   live) → FTP, weight, zones, timezone pulled from icu → asks goal + weekly availability →
   writes minimal `athlete.yaml` (climbs, data_quality, planner knobs optional with defaults)
   → init, migrate, backfill, first plan in `propose`.
7. **Strava is off by default for new profiles** (`STRAVA_ENABLED=false`); icu is primary
   (ADR-0003).
8. **Privacy: personal data never enters the public repo.** The code repo is public by design;
   anything about a real person (weight, FTP, goals, routes, device serials, keys, ride data)
   lives only under `profiles/`, which the public repo git-ignores. `profiles/` may be its own
   *private* git repo (history of athlete intent without publishing it). The public repo ships
   `config/athlete.example.yaml`, and docs/tests use invented numbers. A test fails if
   `config/athlete.yaml` or anything under `profiles/` is tracked.

## Consequences
- Zero schema change and zero engine change to support N athletes on one machine.
- Each profile can be backed up, moved or deleted as one directory.
- Cross-athlete queries (club dashboards) are not possible until L4; acceptable.
- L4 path when needed: add `athlete_id` to the remaining tables, one DB, `DbProfileStore`, icu
  OAuth instead of pasted keys, APScheduler in `cyp serve` instead of launchd. L0 call sites
  already go through `AppContext`, so the change is contained to `store` + `services/context`.

## Alternatives rejected
- **Install on the second athlete's PC**: Python/uv on a non-technical user's machine, updates,
  remote debugging, machine asleep at 05:30. Rejected.
- **Multi-tenant DB now**: large refactor for two users; revisit at L4.
- **Two git clones**: works, but duplicates code updates and schedulers. Rejected for profiles.
