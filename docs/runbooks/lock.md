# "another run is in progress" (exit 3)

Each profile run holds `profiles/<name>/data/.autopilot.lock` (`flock`, released when the process
exits, even on a crash). Exit 3 means another `cyp run` for that profile is still going
(`pgrep -fl "cyp.*run"`). Wait for it; a long Strava backoff or a first-time backfill can take
minutes. A stale lock file without a process is harmless (the OS lock is gone).

The ride watcher (`cyp watch`, [watch.md](watch.md)) takes the same lock for a few seconds every
10 minutes. It never waits: a held lock skips that tick. The morning run waits up to 10 minutes
for a watcher tick to finish before it reports the lock.
