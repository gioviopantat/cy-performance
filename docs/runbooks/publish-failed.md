# Publishing failed or was blocked

| Message | Meaning | Fix |
|---------|---------|-----|
| `PublishFailedError: intervals.icu write failed: …` | icu rejected or failed the bulk write (5xx, 429, bad payload) | usually transient: `uv run cyp --profile <name> run`; repeated → check https://intervals.icu status, then the payload in `publish_log` |
| `CHECK CALENDAR: N event(s) need review` (stage ok) | written, but read-back found events missing or load off target | open the athlete's icu calendar for those days; `publish_log` rows with status `needs_review` |
| `upsert mode unknown` | the profile DB never stored the publish mode | `uv run cyp --profile <name> publish spike --date <future day> --confirm-write` (writes and deletes one test event) |
| `feature publish.calendar is off` | switched off on purpose | `features:` in the profile's `athlete.yaml` |
| `… missing or invalid: not writing` | athlete.yaml broken while writing | [config.md](config.md) |
| `WriteGuardError` | key / athlete mismatch | [write-guard.md](write-guard.md) |
