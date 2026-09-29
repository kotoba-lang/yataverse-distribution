# Lake inventory rollover

Capture a new candidate with `capture_lake_inventory.py`. It walks the public
canonical listing by cursor, writes only compact CID/size rows, checks strict
CID order, and checkpoints after each fsynced page. A stopped or failed run can
be resumed with the same command; an uncheckpointed partial page is truncated.
The final file is published only when the walk finishes and contains every CID
and size from the prior inventory. For example:

```sh
python3 deploy/capture_lake_inventory.py \
  --old-inventory /path/to/inventory-20260926.jsonl \
  --old-sha256 f616962875a0850efa824b53be45fc22edce39f4c280a32cb80b72a55b020188 \
  --output /path/to/inventory-candidate-a.jsonl \
  --state /path/to/inventory-candidate-a.state.json
```

R2 cursor pagination is not snapshot isolation: writes during a walk can be
missed behind its cursor. Capture a second candidate into different files and
require the same row count, total bytes, and SHA-256 before a rollover. If
they differ, retain both candidates and repeat after the writer settles.
Even matching walks establish a bounded stable observation, not an atomic
write cutover or proof that the canonical writer is independent of R2.
For an actively written bucket, pass the same past
`--cutoff-utc YYYY-MM-DDTHH:MM:SSZ` to both walks. Each page still advances across every
CID, but the candidate contains only blocks whose R2 upload timestamp is no
later than the cutoff. The checkpoint tracks the last seen CID separately
from the last included CID so a newer key cannot make resume repeat or skip a
page. A writer that backdates or rewrites objects still requires separate
investigation; this is a bounded listing fence, not a database transaction.

After both complete captures have the same digest, derive the new-CID-only
inventory for a separate CAR lane. `diff_lake_inventory.py` rechecks both
completion receipts and file hashes, requires every old CID and size, and
publishes a compact delta plus a receipt with block count, bytes, digest, and
oversized-row count:

```sh
python3 deploy/diff_lake_inventory.py \
  --old-inventory /path/to/inventory-20260926.jsonl \
  --old-sha256 f616962875a0850efa824b53be45fc22edce39f4c280a32cb80b72a55b020188 \
  --candidate-a /path/to/inventory-candidate-a.jsonl \
  --candidate-a-state /path/to/inventory-candidate-a.state.json \
  --candidate-b /path/to/inventory-candidate-b.jsonl \
  --candidate-b-state /path/to/inventory-candidate-b.state.json \
  --output /path/to/inventory-delta.jsonl \
  --receipt /path/to/inventory-delta.receipt.json
```

Do not start a delta CAR shipper until its source Kubo node actually has the
new CIDs and any oversized rows have a separately verified recovery path.
The old dated CAR jobs keep their original inventory and checkpoints while
the delta is copied; changing their inventory in place would shift row
positions underneath live cursors.

For the 2026-09-29 fenced delta, `xavier-lake-delta-read.service` serves the
977-row, SHA-256-pinned delta on Xavier loopback port 8091. Its replication
uses a separate checkpoint directory and the operator-local R2 bootstrap
bridge, whose allowlist is expanded only after two complete candidate walks
agree. The existing full-inventory reader on port 8090 and its timer retain
the 2026-09-26 identity until a separate full-snapshot rollover is verified.

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
