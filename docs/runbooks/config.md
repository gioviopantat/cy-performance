# Invalid athlete.yaml or feature id

`uv run cyp --profile <name> doctor` prints the first validation error. Compare with
`config/athlete.example.yaml`. Unknown ids under `features:` are rejected on purpose; the valid
ids are in [docs/09-features.md](../09-features.md). `CYP_FEATURES` entries must be `<id>=on|off`.
