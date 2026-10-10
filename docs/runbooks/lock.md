# "another run is in progress" (exit 3)

Each profile run holds `profiles/<name>/data/.autopilot.lock` (`flock`, released when the process
exits, even on a crash). Exit 3 means another `cyp run` for that profile is still going
(`pgrep -fl "cyp.*run"`). Wait for it; a long Strava backoff or a first-time backfill can take
minutes. A stale lock file without a process is harmless (the OS lock is gone).
