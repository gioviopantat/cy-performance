# Run failed before or around the stages (`config`, `lock`, `setup`)

`cyp run` always returns a report; problems outside the normal stages show up as one failed
pseudo-stage:

| Stage | Meaning | Fix |
|-------|---------|-----|
| `config` | the profile's `athlete.yaml` or a feature id is invalid | [config.md](config.md); `uv run cyp --profile <name> doctor` |
| `lock` | another run holds the profile lock | [lock.md](lock.md) |
| `setup` | migration or startup error (message has the exception) | `uv run cyp --profile <name> db upgrade`; then re-run with `--no-write` |

`run --all` prints `FAILED (exit N)` without a report when a child process crashed: exit 124 is
the 60-minute timeout (usually a first-time backfill or a long Strava backoff; re-run), anything
else is a bug: reproduce with `uv run cyp --profile <name> run --no-write` and read
`profiles/<name>/data/logs/cyp.jsonl`.

`profile <name> not found` / `invalid profile name` (exit 2): check `uv run cyp profile list` and
`profiles/.default`.
