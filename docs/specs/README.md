# Specs

Write one before any change bigger than a bug fix: `docs/specs/<name>.md`. Keep it
short; it is the contract the implementation (and its review) is checked against.

```markdown
# <name>
Status: draft | accepted | done · Date

## Goal            what the athlete/owner gets
## Non-goals       what this deliberately does not do
## Design          modules touched (layer table!), data, flags (new feature id + default)
## Safety          does it write to a calendar? which guard covers it?
## Acceptance      concrete checks: commands + expected output, tests that must exist
## Docs to update  01 / 09 / ADR / README / CLAUDE.md / runbooks
```
