# intervals.icu key missing or rejected

1. `uv run cyp profile show <name>` → `icu API key: set|MISSING`.
2. Missing: add `INTERVALS_API_KEY=` to `profiles/<name>/.env` (mode 600). The athlete finds it
   in intervals.icu → Settings → Developer Settings → API key.
3. 401/403: the key was regenerated. Replace it in the profile `.env`. Then
   `uv run cyp --profile <name> run --no-write`.
4. The key must belong to the profile's athlete (`profile.yaml: icu_athlete_id`), else the write
   guard stops publishing: [write-guard.md](write-guard.md).
