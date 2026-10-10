# Runbooks: when the morning run fails

Start here:

```bash
uv run cyp schedule status                       # agent loaded? "last exit code"
tail -50 ~/Library/Logs/cy-performance/autopilot.log
uv run cyp profile list                          # last run per profile (ok / failed)
uv run cyp --profile <name> run --no-write       # reproduce safely (no calendar writes)
```

Each failed stage prints `failed <stage> <ExceptionType>: <message>`. Find the message below.

| Symptom | Page |
|---------|------|
| `INTERVALS_API_KEY is not set`, 401/403 from intervals.icu | [icu-key.md](icu-key.md) |
| failed `config` / `lock` / `setup` stage, `FAILED (exit N)`, unknown profile | [run-failed.md](run-failed.md) |
| `PublishFailedError`, `CHECK CALENDAR`, `upsert mode unknown` | [publish-failed.md](publish-failed.md) |
| `WriteGuardError`, "refusing to write", "not syncing" | [write-guard.md](write-guard.md) |
| `another run is in progress` (exit 3) | [lock.md](lock.md) |
| `nodename nor servname provided` in sync and publish, every profile | [network.md](network.md) |
| agent not loaded / never ran / ran late | [schedule.md](schedule.md) |
| plan `NEEDS REVIEW`, publish "diff only (plan needs review)" | [needs-review.md](needs-review.md) |
| Strava 429 / token expired | [strava.md](strava.md) |
| `invalid athlete config`, unknown feature | [config.md](config.md) |
