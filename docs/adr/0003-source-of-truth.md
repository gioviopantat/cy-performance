# ADR-0003 · intervals.icu is the load ledger **and** stream source; Strava is secondary

**Status**: accepted · 2026-10-02 · amended 2026-10-02 after confirming the Garmin Edge 850 is linked directly to intervals.icu

## Context
- intervals.icu already ingests from Strava, Garmin and Rouvy, computes training load, eFTP,
  CTL/ATL, zones, intervals, and holds the calendar we write to.
- Since Strava's Nov-2024 API agreement, intervals.icu does **not** expose Strava-origin
  activity files/streams through its API. The athlete confirmed the Edge 850 is connected
  **directly** to intervals.icu, so Garmin rides arrive as Garmin-origin activities with full
  streams and intervals available via the icu API. Rouvy rides also sync to icu directly.
- Strava still holds official segments, segment efforts and PRs, and remains a fallback for
  streams on any activity that only exists there (e.g. manual uploads).

## Decision
1. Training load, fitness/fatigue, FTP/eFTP/zones, wellness and planned events are read from
   intervals.icu and treated as canonical. Our PMC is a simulator that must agree with icu ±1.
2. Streams come from **intervals.icu** (`GET /activity/{id}/streams.json`) through a
   source-agnostic `StreamStore`. Strava streams are fetched only when icu has none for a
   matched activity (`stream_files.source` records which).
3. Strava is used for segments, segment efforts, PRs, and as the optional `RIDE.LOG`
   description target. It can be disabled entirely (`STRAVA_ENABLED=false`) without losing
   analysis or planning.
4. Every activity row stores both ids and the match method. Duplicates (same ride via Garmin
   and via Strava) collapse onto the Garmin-origin icu activity.

## Consequences
- No dual-ledger drift arguments with the athlete's own intervals.icu charts.
- Strava sync shrinks to a low-priority job (segments/PRs); rate limits stop mattering.
- Matcher still needed to attach Strava segment efforts to icu activities.
- Legal exposure to Strava's AI clause is minimised by never passing Strava streams/text to an LLM.
