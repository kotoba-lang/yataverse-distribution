# Lake inventory rollover

`replicate_lake.py` normally refuses a checkpoint whose inventory digest differs
from `--inventory-sha256`. A newer lake snapshot can put new CIDs before the
current numeric cursor, so changing the digest without rewinding would skip
blocks. This rollover verifies both complete JSONL snapshots, requires every
old CID and size in the new snapshot, checks that the local read API serves the
new digest, saves the old checkpoint, then rewinds to row zero. Already pinned
blocks are checked; only missing bytes are copied. Every listing page must
carry the expected inventory digest or replication stops before fetching it.

To roll both nodes forward, keep the previous snapshot file available and
prepare a newly hashed, counted snapshot on each node. Update `serve_lake.py`
on both nodes before updating `replicate_lake.py` so current pages include
`inventory-sha256`. Stop each replica timer and let its current batch finish.
Install the new snapshot under a new path, update each reader unit's
`--inventory`, `--sha256`, and `--count`, then restart its reader. Verify
`GET /health` and the first listing page report the new digest. Update the
replica unit's `--inventory-path` and `--inventory-sha256`, adding
`--previous-inventory-path` and `--previous-inventory-sha256` for the old
snapshot. Restart one replica at a time and verify its
`checkpoint.pre-inventory-<old-sha>.json` backup, marker, and page progress.
Keep the old snapshot and checkpoint backup through the first successful
new-inventory cycle. Restart the timers after both nodes have advanced.

The rollover fails closed if an old CID disappears or changes size, either
snapshot digest differs, or a reader still serves the old inventory. It does
not infer that the canonical source is append-only or that a current D1/R2
export has been acquired. A snapshot must be obtained and independently
verified before this procedure is used in production.
