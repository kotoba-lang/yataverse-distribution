# yataverse-distribution

**Geo-aware distribution for the yataverse.com bytes plane: plan WHERE a
CID's replicas live, bridge the plan into the p2p exchange semantics the
stack already ships, and name the e2e delivery path end to end.**

`yataverse.com` is the decentralized mirror zone of the kotobase bytes
plane (ADR-2609131630, com-junkawasaki root). One Worker serves both
`{cid}.ipfs.kotobase.net` and `{cid}.ipfs.yataverse.com` from one R2
bucket — the zone is a Location, not a second implementation. This repo
owns the layer that plane was missing: the replication PLANNER and the
gossip/bitswap BRIDGE for p2p delivery over that zone, so delivery does
not stop at the two HTTP gateways.

The name is a family name: the serving family of `yataverse.com`
(registered in `manifest/concept-vocabulary.edn`, com-junkawasaki root).

## What it is — and what it is NOT

| layer | owner | this repo's role |
|---|---|---|
| bytes gateways (R2 origin + mirror zone) | `net-kotobase/ipfs` Worker | describes the stages (`delivery-path`); performs nothing |
| gossip / bitswap semantics | `kotoba-lang/io-libp2p` | consumes via `gossip-bridge`; never re-implements |
| transport (QUIC/Noise overlay) | `kotoba-lang/murakumo` overlay | the adapter seam (ADR-2607023100, unlanded) |
| node inventory (`:labels {:tier :zone}`) | murakumo `fleet.edn` | input to `planner/plan-replication` |
| **replication planning + geo ranking** | **this repo** | owns |

## Modules

- `yataverse.distribution.planner` — pure planning:
  - `plan-replication(cid, inventory, factor)` — zone-round-robin replica
    assignment over the fleet's `:labels {:zone ...}` inventory. Fails
    closed: zero/negative factor, empty inventory, or inventory smaller
    than the factor throws — an under-replicated plan never ships.
  - `rank-providers` / `nearest-provider` — haversine geo ranking. A
    provider without coordinates is REFUSED, not silently ranked last.
  - `delivery-path(cid, plan)` — the e2e as data: origin bytes plane →
    yataverse mirror zone → per-replica p2p hops → IPNI discovery.
- `yataverse.distribution.gossip-bridge` — converts plans into the
  shapes `kotoba.net.gossip` / `kotoba.net.bitswap` already define
  (zone-scoped topics, announce/relay through `route-message` with the
  core's content-hash dedup, want-lists from assignments via
  `respond-to-want`). Socket-free by design; the transport adapter
  (murakumo overlay QUIC) is the remaining host shell (ADR-2607023100).

## Test

```sh
kbb --backend sci --classpath "src:test:../io-libp2p/src" \
    -e '(require (quote [yataverse.distribution.test-runner :as runner])) (runner/-main)'
```

Explicit-namespace runner; exits non-zero on any failure. Current: 14
tests / 51 assertions, 0 failures.

## Xavier replica (2026-09-26)

The owner selected `gad` and `xavier` for the first physical replication
slice. `gad` already runs Kubo. Xavier has Kubo v0.43.1 for Linux ARM64 at
`/mnt/nvme/ipfs-xavier/bin/ipfs`, with a repo on the NVMe volume. The binary
was obtained from `dist.ipfs.tech` and checked against its `.sha512` file.
The Xavier RPC API (`5001`) and HTTP gateway (`8080`) bind only to
`127.0.0.1`; the swarm listens on `4001`.

The user service is [deploy/xavier-ipfs.service](deploy/xavier-ipfs.service).
Install it as the `xavier` user after checking the binary and repo paths:

```sh
install -Dm644 deploy/xavier-ipfs.service ~/.config/systemd/user/ipfs.service
systemctl --user daemon-reload
systemctl --user enable --now ipfs.service
```

The unit was enabled and observed active after a fresh SSH connection.
`loginctl show-user xavier -p Linger` now says `Linger=yes`. A reboot/reconnect
check remains necessary before calling the replica boot-qualified.

Measured CID: `bafkreibmsfku23elxwds53iriwuwueot2rsb72pxvr35nisowufmlvf6w4`
(46 bytes, SHA-256
`2c91554d6c8bbd872eed1145a96a11d3d4641fe9f7ac77d6a24eb50ac5d4beb7`).
Xavier pinned it from gad over a direct libp2p WebRTC path. Xavier's local
gateway returned the same bytes, and `ipfs --offline cat` after daemon
shutdown returned the same digest. This proves two pinned copies and Xavier
offline custody for that probe. It does not qualify public gateway ingress,
Filecoin storage, or a service-wide failover.

## IPNS name renewal

`yataverse-apex` is an Ed25519 IPNS key in the Kubo keystores on both gad
and Xavier. Its public name is
`k51qzi5uqu5dlrtqhevrfpyrdr2c1thxug3iqys76xxbi7bgzm16mjti2bwrf2`.
The private key is not in this repository. Both nodes published the current
HTML CID, and Xavier resolved gad's record with `--nocache`.

`deploy/refresh-ipns.sh` refuses a missing or malformed CID file and a
CID that is not recursively pinned on the local node. It checks that Kubo
published exactly the expected name and CID. The two user timers renew
the record at staggered times, before its 168-hour expiry:

| node | files to install as the node user | UTC schedule |
|---|---|---|
| gad | `deploy/gad-ipns-refresh.{service,timer}` | 00:07 and 12:07 |
| Xavier | `deploy/xavier-ipns-refresh.{service,timer}` | 06:07 and 18:07 |

Install the script as `~/.local/bin/yataverse-ipns-refresh` (mode 755),
the corresponding unit files as
`~/.config/systemd/user/yataverse-ipns-refresh.{service,timer}`, and put
the locally pinned CID in `~/.local/share/yataverse/current-cid` (one
line). Then run `systemctl --user daemon-reload`,
`systemctl --user enable --now yataverse-ipns-refresh.timer`, and
`systemctl --user start yataverse-ipns-refresh.service`. The service
requires a running local Kubo daemon. Both users have `Linger=yes`.

Update the CID file on both nodes only after the new CID is pinned and
verified on both. These timers keep a verified snapshot's name alive; they
do not fetch new site revisions. The public HTTP gateway and node-loss
drill are still separate qualification steps.

## Lake block replication

`deploy/replicate_lake.py` copies the public lake block listing to a local
Kubo node in bounded batches. It checks each listed byte count, makes Kubo
rederive the original CID, reads the block back, then direct-pins it. If Kubo
rejects pinning because the dag-pb bytes cannot be decoded as protobuf, the
replicator stores the exact CID-checked bytes in a separately fsynced raw
block store. Other pin failures still stop the batch. It
records a receipt for each new block and advances the durable page cursor
only when every block on that page is accounted for. Reruns skip pinned
blocks after checking their size. The deployed listing comes from the pinned
local inventory. Xavier initially seeds bytes from the public source; gad runs
with `--peer-only` and reads bytes from Xavier's named HTTPS endpoint at its
LAN address. A missing peer block stops gad's batch without advancing its
page cursor; it never falls back to the public source. This is a two-node copy
path on one router, not a separate WAN failure domain.

### Temporary R2 bootstrap bridge

The operator host can read the same immutable blocks directly from R2 using
its existing Wrangler OAuth session. `deploy/r2_origin_bridge.py` checks the
entire pinned inventory digest on startup, accepts only listed CIDs, bounds
in-flight memory, and verifies every returned size and CID before serving a
block. It binds **127.0.0.1 only**. An SSH reverse tunnel can make it appear as
Xavier's `127.0.0.1:18095`; the OAuth token stays on the operator host.

```sh
python3 deploy/r2_origin_bridge.py \
  --inventory /path/to/inventory-20260926.jsonl \
  --sha256 f616962875a0850efa824b53be45fc22edce39f4c280a32cb80b72a55b020188 \
  --account-id 4da88288dc30d9ee257f319d3c33ecf0 \
  --bucket kotobase-graph-database-production
ssh -N -o ExitOnForwardFailure=yes \
  -R 127.0.0.1:18095:127.0.0.1:18095 root@xavier
```

Run the tunnel as a separate process. Xavier's service first tries its local
peer, then the bridge, then the existing public gateway. The batch JSON
records `origin_sources` so public fallback cannot be reported as bridge
success. Each completed invocation also writes one JSON file under
`--state-dir/batch-receipts/` with the result, source counts, checkpoint,
script digest, and systemd invocation ID when present. A failed or interrupted
batch has no completion file; an invocation ID identifies a systemd service
run but does not by itself prove that the timer triggered it. When the bridge
is unavailable, attempts are suppressed for one
minute before another probe; the public path remains usable. This operator
relay accelerates a dated bootstrap and does not qualify independent ongoing
ingress or ownership of the canonical hostname. In a bounded live probe, 100
listed blocks (6,167,497 bytes) reached Xavier through the SSH tunnel in
4.53 seconds with every CID verified. Full-lake completion remains a separate
checkpoint and audit requirement.

Run as the node owner with `IPFS_PATH` set to its local repo. For example,
gad uses `/usr/local/bin/ipfs` and `/home/gad/.ipfs`, while Xavier uses
`/mnt/nvme/ipfs-xavier/bin/ipfs` and `/mnt/nvme/ipfs-xavier/repo`.
The deployed units set `--kubo-api-url http://127.0.0.1:5001` so each block
uses the daemon's loopback RPC instead of starting a CLI process repeatedly.
Startup still reads the CLI pin list and repo status, and refuses when the RPC
daemon reports a different repo path. Every new block still requires the
expected CID and exact byte readback, followed by a confirmed Kubo pin or a
durable raw block whose sha2-256 CID is rederived independently, before its
receipt or page cursor is advanced. The RPC URL accepts loopback
only; do not publish Kubo's admin API. Omitting the option retains the CLI
path for bounded manual probes. On gad, 20 already-pinned `block stat`
measurements had 25.4 ms CLI and 1.3 ms RPC median; this measures call
overhead, not full-batch throughput. A new 38-byte gad canary and 41-byte
Xavier canary each passed CID, readback, size, and direct-pin checks through
the RPC path. Install `deploy/raw_block_store.py` beside both installed
Python scripts and give the replication and read services the same
`--raw-block-store` path on their own node. Only the observed `pin: protobuf:`
decode refusal uses this store. A corrupted or incomplete stored block
refuses on read and audit rather than counting as replicated.
Give each node its own persistent `--state-dir`. The default batch copies at
most 20 pages and 512 MB of new bytes. `--max-pages`, `--max-new-bytes`, and
`--max-block-bytes` bound an invocation; an exceeded byte budget returns a
`byte-budget` status and retains the current page cursor. A failed source,
CID mismatch, unreadable capacity, or invalid listing exits with `REFUSED`.
The script also checks actual free space on the Kubo repo filesystem before
each new block and reserves 50 GB by default (`--min-free-bytes`).

Some listed blocks exceed Kubo's standard 2 MiB Bitswap block size. The
script uses Kubo's explicit large-block option for these. Their local copy
does not establish standard Bitswap delivery; an independent large-block
transport or a compatible HTTP gateway must be qualified separately.

Before increasing Kubo `StorageMax` toward full replication, measure the
whole listing and leave enough capacity for both nodes. A bounded live
run on each node copied 3 new blocks / 786,474 bytes and verified 53 listed
blocks, stopping before the first page cursor on the 1 MB byte budget.
Subsequent bounded runs covered the first four full pages and part of the
fifth: 987 listed blocks are pinned on each node, with equal checkpoints
(`pages_total=4`, `new_blocks_total=937`, `new_bytes_total=236213699`;
the other 50 blocks were already pinned from the earlier probe).

The matching `deploy/{gad,xavier}-lake-replicate.{service,timer}` user units
run a bounded batch at staggered calendar times (gad 10/40 UTC, Xavier
25/55 UTC) and two minutes after each completed batch. The process lock
prevents concurrent batches on a node. Install the
node's service and timer as `~/.config/systemd/user/yataverse-lake-replicate.*`
and the current script as `~/.local/bin/yataverse-lake-replicate` (mode 755),
then reload and enable the timer. Each node retains its own state directory.
The timer does not change Kubo's `StorageMax`; measure the lake and physical
capacity before increasing that limit.

Keep Xavier ahead of gad before switching gad to `--peer-only`. Stop gad's
timer and active service, install the peer-only service unit, reload systemd,
then restart the timer after Xavier has advanced. Verify a fresh block on gad,
the matching CID and byte digest on both nodes, and a successful LAN peer
response in Xavier's web-server log. A peer 404 is an incomplete copy, not a
reason to fetch from Cloudflare on gad. Xavier remains the bootstrap source
until the full dated inventory is stored on both nodes.
Gad limits peer fetches to two simultaneous requests and retries transient
peer HTTP errors three times; a persistent refusal still stops the batch.
If gad reaches a CID before Xavier pins it, the peer-only unit waits for up
to 120 seconds for that block. It never falls back to the public source or
advances the page cursor during that wait. If the peer remains unavailable,
the batch exits with the peer error and the timer resumes later.

After measuring the dated lake at 88,410,406,176 bytes and checking 120 GB
Kubo limits plus physical free space on both nodes, the deployed user units
allow up to 100 pages / 2 GB of new bytes per invocation and a three-hour
start timeout. The script's default remains 20 pages / 512 MB for manual
bounded probes. A service that fails or crosses the disk reserve refuses;
the next invocation resumes at its persisted cursor. The two-minute restart
reduces idle time between successful batches; the 2 GB byte cap, Kubo storage
limit, and 50 GB physical disk reserve remain in force. These settings do not
prove full replication.

## Local read API from the dated lake snapshot

`deploy/serve_lake.py` serves the complete, dated inventory and locally pinned
or CID-verified raw blocks without fetching from Cloudflare. It refuses startup unless the
inventory's SHA-256 and row count match the declared snapshot. Its
`/api/v1/lake/blocks` response uses the same `blocks`, `cursor`, and
`truncated?` fields as the replication source, with a local integer cursor.
`/ipfs/{cid}` accepts only a CID in that inventory. It either validates the
raw store's CID, size, and digest or confirms a direct/recursive Kubo pin and
reads the block with `ipfs --offline`. Missing or oversized blocks return an
error. `/health` describes the
inventory only; it does not certify that all block bytes have been copied.

The first deployment uses the dated `inventory-20260926.jsonl` snapshot
(821,533 rows; SHA-256
`f616962875a0850efa824b53be45fc22edce39f4c280a32cb80b72a55b020188`)
and binds loopback only (`gad:18090`, `xavier:8090`; gad's 8090 and 8091
are occupied by local model servers). Install `serve_lake.py` as
`~/.local/bin/yataverse-lake-read` and the matching
`deploy/{gad,xavier}-lake-read.service` as
`~/.config/systemd/user/yataverse-lake-read.service`. Copy the verified
inventory to the path in the unit, then enable the service. This local route
can be tested with a direct node connection. Public ingress and a live
snapshot refresh still need separate qualification.

`deploy/audit_lake.py` compares the dated inventory against Kubo's durable
direct, recursive, and indirect pins. When `--raw-block-store` is supplied,
it checks the CID and bytes of each raw sidecar block. It refuses an absent or changed
inventory, a failed pin listing, corrupted sidecar bytes, and invalid or
duplicate rows, then reports pinned, raw, and missing coverage separately.
`--require-complete` exits 1 while any inventory CID is missing, 2 when the
audit cannot answer, and 0 only when every inventory CID has a durable pin
or verified raw copy. It does not reread every pinned block's content: the
copy receipt and Kubo readback in `replicate_lake.py` cover that
separate check. For a node:

```
python3 deploy/audit_lake.py \
  --inventory /path/to/inventory-20260926.jsonl \
  --sha256 f616962875a0850efa824b53be45fc22edce39f4c280a32cb80b72a55b020188 \
  --count 821533 --ipfs-bin /path/to/ipfs --require-complete
```

For a CAR-fed node, supply both import receipt directories and `--ipfs-path`.
Add one `--native-receipts` per state directory when the node also runs public-peer native mirrors.
This mode checks the current recursive roots, reads every receipted CAR or native root
offline to compare its exact inventory links, checks direct pins and verified
raw sidecars, and reports a lower bound on retained rows. It avoids Kubo's
costly complete indirect-pin listing. `--require-complete` still fails while
rows are missing. A complete receipt audit proves the linked roots and pin
state at audit time; a separate leaf readback or `ipfs pin verify` is required
to prove that every linked block remains readable.

```
python3 deploy/audit_lake.py \
  --inventory /path/to/inventory-20260926.jsonl \
  --sha256 f616962875a0850efa824b53be45fc22edce39f4c280a32cb80b72a55b020188 \
  --count 821533 --ipfs-bin /path/to/ipfs --ipfs-path /path/to/kubo-repo \
  --car-receipts /path/to/lake-state --large-receipts /path/to/recovery \
  --native-receipts /path/to/native-state \
  --raw-block-store /path/to/raw-blocks --require-complete
```

Jacob runs this receipt audit every six hours with
`deploy/jacob-lake-receipt-audit.plist`. Install the plist in
`~/Library/LaunchAgents/` after installing the matching script and checking
its paths, then load it with
`launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/cloud.kotoba.jacob.lake-receipt-audit.plist`.
Each run appends one JSON report to `receipt-audit.stdout.log`; failures go to
`receipt-audit.stderr.log` with a nonzero launchd exit code. Partial coverage
is expected during transfer and is reported as `status: partial` with exit 0.
The report remains a lower bound: it verifies current roots and receipts,
not every leaf's bytes. Read individual blocks back for content checks.

`deploy/verify_lake_leaves.py` closes that measurement gap incrementally. It
validates the dated inventory, Jacob's repository identity, and the loopback
RPC address before reading each block with `offline=true`. It checks each
block's size and sha2-256 CID digest, then atomically checkpoints the verified
prefix and a rolling scan hash. A missing block stops the default scan at that
row so the next run can continue after replication reaches it; corrupted bytes
refuse without advancing. `--scan-gaps` is for a diagnostic pass that counts
missing rows, and requires a fresh pass after transfer completes.

Jacob runs `deploy/jacob-lake-leaf-readback.plist` every five minutes against
the dedicated HDD repo. The first live canary read 203 consecutive rows and
49,902,505 bytes with zero missing rows. This is a point-in-time byte check,
not a pin or Filecoin custody claim. Full inventory verification requires the
readback cursor to reach all 821,533 rows with zero missing rows **and** a
separate current root and receipt audit.

`deploy/export_lake_car.cljk` writes one bounded range of the dated inventory
to CARv1 with `io-ipld-car`'s streaming writer. It checks the complete
inventory SHA-256 and row count, downloads original blocks through one node's
own HTTPS reader, checks size and CID, normalizes CIDv0 to equivalent CIDv1
dag-pb, and publishes an fsynced file without replacing an existing output.
A refusal removes the partial file. The receipt gives the row range, block
count, CAR byte count and SHA-256 for a later storage handoff.

```bash
kbb --backend sci --classpath "$(kbb -Spath)" deploy/export_lake_car.cljk \
  --inventory /path/to/inventory-20260926.jsonl \
  --inventory-sha256 f616962875a0850efa824b53be45fc22edce39f4c280a32cb80b72a55b020188 \
  --inventory-count 821533 --start-row 0 --max-blocks 3 --max-bytes 2000000 \
  --base-url https://yataverse-data.220-146-170-114.sslip.io/ipfs/ \
  --resolve yataverse-data.220-146-170-114.sslip.io:443:100.87.226.80 \
  --output /path/to/first-3.car
```

`--resolve` pins the HTTPS hostname to Xavier's private node address for this
transfer, bypassing public DNS and proxies while retaining TLS hostname checks.
Replace the address if Xavier's private endpoint changes.

On a destination node, copy the CAR and its export receipt, then import using
the receipt's exact SHA-256. `import_lake_car.py` checks the dated inventory,
CAR digest, repository identity and disk reserve. It imports blocks without
pinning the CAR header's normalized CIDv1 roots, then pins one DAG-CBOR root
whose links use the original inventory CIDs. It confirms all selected CIDs are
durably pinned before writing a receipt. The read API, audit and replicator
recognize Kubo's `indirect through <root>` pin type. Keep the CAR until the
import receipt and API readback are confirmed; a failed import may leave
unpinned blocks that Kubo can garbage collect.

```bash
python3 deploy/import_lake_car.py \
  --inventory /path/to/inventory-20260926.jsonl \
  --sha256 f616962875a0850efa824b53be45fc22edce39f4c280a32cb80b72a55b020188 \
  --count 821533 --start-row 0 --max-blocks 3 \
  --car /path/to/first-3.car --car-sha256 4e86a913a863b47f9067b52f9a3080d75345beb2e78d67fe07b19fd33e22c9ea \
  --ipfs-bin /path/to/ipfs --ipfs-path /path/to/dedicated/repo \
  --receipt /path/to/first-3-import.json
```

On Jacob's dedicated 18 TiB HDD repo on 2026-09-28, a 10-block CAR imported
without per-root pinning in 1.1 seconds. A single 10-link root pinned in 2.1
seconds; the original CIDv0 was reported by Kubo as indirectly pinned and the
local read API returned its 262,158 bytes. A 200-link root pinned in 3.4
seconds. Per-root CAR import for the same 200 blocks took 910 seconds, so the
single-root path is the measured basis for further bulk replication. This is
only the tested range, not a full-lake or Filecoin custody claim.

`deploy/ship_lake_car.py` transfers one bounded range per invocation. It
checks the inventory identity, reuses a digest-checked CAR after a failed
transfer, verifies Jacob's import receipt, and advances an atomic checkpoint
only after the remote pin succeeds. Run it periodically on a source node with
key-based SSH to Jacob. It records blocks above 8 MB in individual
`skipped-<row>.json` receipts and advances past them; those rows still need the
large-block recovery path before the dated inventory is complete. The regular
block replicator can run alongside it and recognizes the batch root's
indirect pins on its next invocation.

For a Kubo source on Xavier, `--source-rpc-url http://127.0.0.1:5001`
passes the source's loopback RPC endpoint to
`deploy/export_lake_car_from_kubo.py`. The exporter verifies that the RPC
daemon and the local CLI use the requested repository, requests each block
with `offline=true` over one connection, and still checks its inventory size
and CID digest before writing the CAR. The RPC URL must be loopback HTTP;
Kubo's admin RPC must never be published. A 30-block live comparison on Xavier
read the same bytes in 0.236 seconds over RPC versus 2.372 seconds through
individual CLI calls. This is a source-read microbenchmark, not end-to-end
CAR transfer throughput. A separate 200-block, 50,179,997-byte CAR from
rows 4200–4399 was byte-identical between the two paths; full source export
took 7.977 seconds over RPC versus 20.190 seconds through the CLI. Network
copy and Jacob import were not part of that comparison.

```bash
python3 deploy/ship_lake_car.py \
  --source /path/to/yataverse-distribution --kbb /path/to/kbb \
  --inventory /path/to/inventory-20260926.jsonl \
  --sha256 f616962875a0850efa824b53be45fc22edce39f4c280a32cb80b72a55b020188 \
  --count 821533 --start-row 4200 \
  --base-url https://yataverse-data.220-146-170-114.sslip.io/ipfs/ \
  --resolve yataverse-data.220-146-170-114.sslip.io:443:100.87.226.80 \
  --state-dir /path/to/ship-state --ssh-host junkawasaki@100.117.208.83 \
  --remote-dir /path/to/jacob/lake-state \
  --remote-importer /path/to/jacob/bin/import_lake_car.py \
  --remote-inventory /path/to/jacob/inventory-20260926.jsonl \
  --remote-ipfs-bin /path/to/jacob/ipfs \
  --remote-ipfs-path /path/to/jacob/dedicated/repo
```

`deploy/main2-lake-car-jacob.plist` records the installed main-2 LaunchAgent
for the dated inventory and Jacob's dedicated HDD repository. Its 10-second
interval replaces the earlier 30-second interval; launchd keeps one instance
of the same job active at a time, and `ship_lake_car.py` takes an exclusive
state lock. The checkpoint advances only after Jacob writes an import receipt.
The installed `--stop-row 300000` lets this lane continue from the early
inventory into rows 100000–299999 after the first 100000 rows are complete.
That middle range contains 200000 rows, 17541495955 bytes, and no blocks over
8 MB or raw sidecar exceptions in the declared inventory. The final batch is
shortened to end exactly at row 300000, and a checkpoint past the stop row
refuses instead of silently claiming completion. Keep the Jacob native middle
job disabled while the CAR lane owns that range; preserve its earlier receipts
for the coverage audit.
The LaunchAgent reads an installed copy of `ship_lake_car.py` and its dated
raw-CID manifest from `~/.local/share/yataverse-car-jacob/bin/`; install
those exact files before loading the plist. The state directory and existing
checkpoint stay in place across code updates, while the running job no longer
depends on a moving source checkout.
Before installing it on another Mac, adjust all absolute paths, the SSH hosts,
and the disk reserve settings to that machine. An interval change does not
establish full inventory custody; use `audit_lake.py` and leaf readback to
measure that separately.

`deploy/mirror_lake_bitswap.py` is a Jacob-local path for a disjoint dated
inventory range. It requires an explicit public IPv4 libp2p peer route to
Xavier, downloads bounded blocks through Jacob's Kubo daemon, checks every
block's inventory size and CID digest, and recursively pins one batch root
before checkpointing. Its native receipts are included in the receipt audit;
a later offline leaf readback remains necessary. The installed
`deploy/jacob-lake-native-bitswap.plist` targets rows 300000–821532, away from
the main-2 CAR shipper's current prefix. The state directory retains its
initial `native-300000-400000` name so existing receipts and checkpoint remain
valid. The public peer check establishes a
usable independent route; it does not prove which Bitswap peer supplied every
block, router failover, full inventory custody, or Filecoin storage.
`deploy/jacob-lake-native-prefix.plist` is the fallback Jacob-local job for
rows 100000–299999, using four workers and 100-block batches every two
minutes when enabled. The audit plist includes its existing receipt directory.
The main-2 CAR shipper covers rows before 300000, including oversized and raw
sidecar exceptions in the early range. Neither scheduled job is a full-lake
custody receipt.

On 2026-09-26 the first three actual rows exported from Xavier :8443 and gad
:8444 to byte-identical 786,733-byte CARs, SHA-256
`4e86a913a863b47f9067b52f9a3080d75345beb2e78d67fe07b19fd33e22c9ea`.
A fresh offline Kubo repo imported the CAR and read back the first original
CIDv0 block. A wrong inventory digest and a too-small output ceiling each
exited 2 without publishing an output file. This is a three-block recovery
canary, not a complete lake export or a Filecoin custody proof. The dated
inventory's first 200 rows also exported from Xavier to a 49,132,040-byte
CAR, SHA-256 `bc7aff2c03e7294a0f5bc141f23f5752b16f11a17eb2730e74a83bc2f075c482`.
The fresh offline Kubo repo imported it and read back both the first and last
CIDv0 blocks in that range. This larger canary is still a subset. The dated
inventory totals 88,410,406,176 block bytes; 446 blocks exceed the current
public reader's 8 MB per-block limit (largest 204,123,728 bytes). The updated
reader accepts an explicit 256 MB ceiling for the dated inventory and allows
only one request above 8 MB at a time, returning 503 to a second large read.
Both node units set `MemoryMax=2G`. The reader still buffers one block, so
live qualification must include a largest-class block from each node and
memory observation before treating the whole inventory as exportable. The
203,519,718-byte row 16866 was read through both public HTTPS routes with
SHA-256 `1ae98a9fe61f9d70ac7f94f1a2e4129c3ca858b68273f8a5ddcf079644f6b499`.
Gad's observed reader memory peak was 607,698,944 bytes under its 2 GiB cap.
Xavier also returned the complete block; its user service did not expose a
memory peak in this check.

For oversized source blocks, use `deploy/export_large_lake_block.py`. A CAR
frame containing the unmodified 203 MB raw block was written successfully,
but a fresh Kubo repo refused to import that single oversized section even
with `--allow-big-block`. The large-block exporter instead builds 256 KiB
UnixFS leaves, exports that DAG as a CAR, imports it into a fresh offline Kubo
repo, cats the root, checks the original byte count and sha2-256 CID digest,
and rehydrates the original CID with `ipfs block put --allow-big-block` before
publishing the CAR and JSON receipt. The recovery root is a new CID; the
receipt preserves its mapping to the original lake CID.
`export_lake_car.cljk` refuses these blocks by name and sends them to this
recovery path; it does not publish a CAR that has not passed the importer.

```bash
python3 deploy/export_large_lake_block.py \
  --inventory /path/to/inventory-20260926.jsonl \
  --sha256 f616962875a0850efa824b53be45fc22edce39f4c280a32cb80b72a55b020188 \
  --count 821533 --row 16866 \
  --base-url https://yataverse-data.220-146-170-114.sslip.io:8443/ipfs/ \
  --ipfs-bin /path/to/ipfs --output /path/to/row-16866-recovery.car
```

For row 16866, this produced a 203,589,477-byte CAR with SHA-256
`2feec457907ce841a90b49dc0aca4c5b3cced60109970fa7ed50299ad5d3f978`
and recovery root `bafybeic4uk6d2ose5wyadez6df4vtfnj7ypghxgrrwo5f2eddashia4gma`.
The automated fresh-repo import, offline cat, and original-CID rehydration
passed. This validates one large block; the remaining 445 large blocks and
the complete lake still need export, storage placement and retrieval checks.

`deploy/export_large_lake_batch.py` advances the dated oversized-block plan.
It verifies all 821,533 inventory rows before selecting the 446 blocks over
8 MB (13,454,005,391 original bytes), then handles one row at a time. Each
CAR is first checked by a fresh offline Kubo restore and original CID
rehydration, then copied to gad and xavier with digest checks. On restart it
rechecks both nodes' receipt and CAR digest, skips matching rows, and resumes
at the next missing row. An incomplete local pair refuses instead of being
counted as copied. An exclusive output-directory lock refuses a concurrent
batch; a stopped process releases it so the same command can resume. Keep at
least 50 GB free on the Mac and both node volumes.

```bash
python3 deploy/export_large_lake_batch.py \
  --inventory /path/to/inventory-20260926.jsonl \
  --sha256 f616962875a0850efa824b53be45fc22edce39f4c280a32cb80b72a55b020188 \
  --count 821533 --expected-large-count 446 \
  --output-dir /path/to/lake-large-car-export --ipfs-bin /path/to/ipfs --plan
# Remove --plan and set --max-new 10 for a bounded production batch.
```

The first live bounded passes skipped the already mirrored row 16866, then
exported and mirrored rows 16885 and 16994. Their CAR digests are checked on
both nodes after placement. This proves restart/skip and two new rows, not
all 446 rows. A later audit must reconcile the complete inventory with both
nodes and perform provider retrieval before calling this Filecoin custody.

For a third storage node, copy each recovery CAR and its `.json` receipt to
the node's own disk, then use `deploy/import_large_lake_recovery.py`. It checks
the dated inventory and source CAR, pins the UnixFS recovery root, restores
the original bytes offline, rederives the original raw CID in Kubo, and pins
that CID directly. The intermediate restored file is removed after validation;
the CAR and its source receipt remain for recovery. The output receipt is
written only after Kubo confirms the original CID and size. A rerun is safe.

```bash
python3 deploy/import_large_lake_recovery.py \
  --inventory /path/to/inventory-20260926.jsonl \
  --sha256 f616962875a0850efa824b53be45fc22edce39f4c280a32cb80b72a55b020188 \
  --count 821533 --row 16866 \
  --car /path/to/row-16866-recovery.car \
  --source-receipt /path/to/row-16866-recovery.car.json \
  --ipfs-bin /path/to/ipfs --ipfs-path /path/to/dedicated/repo \
  --output-receipt /path/to/row-16866-jacob-import.json
```

`deploy/import_large_lake_batch.py` advances the fixed set of 446 oversized
rows in inventory order. It waits for absent CAR/receipt pairs, verifies
previously receipted Kubo pins, imports a bounded number per invocation, and
advances its checkpoint after each confirmed row. It must use a complete,
digest-verified recovery archive; a partially transferred CAR is refused.
On Jacob's dedicated HDD, row 16866 restored and pinned its original
203,519,718 bytes; the local read API returned SHA-256
`1ae98a9fe61f9d70ac7f94f1a2e4129c3ca858b68273f8a5ddcf079644f6b499`.
That one row does not establish custody of the other 445 oversized rows.

```bash
python3 deploy/import_large_lake_batch.py \
  --inventory /path/to/inventory-20260926.jsonl \
  --sha256 f616962875a0850efa824b53be45fc22edce39f4c280a32cb80b72a55b020188 \
  --count 821533 --expected-large-count 446 \
  --expected-large-bytes 13454005391 \
  --archive /path/to/recovery-archive --receipt-dir /path/to/import-receipts \
  --state-dir /path/to/import-state \
  --importer /path/to/import_large_lake_recovery.py \
  --ipfs-bin /path/to/ipfs --ipfs-path /path/to/dedicated/repo \
  --max-new 5
```

For the first public read entry, `deploy/xavier-public-lake.conf` exposes this
same local service at `https://yataverse-data.220-146-170-114.sslip.io:8443/`
through Xavier's Nginx and the shared router mapping (public 8443 to Xavier
443). Issue a certificate for that exact name using the port-80 webroot
challenge before enabling the HTTPS server; retain the Isekai virtual host on
the same listener. The Nginx config limits each source address to 8 requests
per second and 8 simultaneous connections, permits GET and browser preflight
OPTIONS, and serves only
`/health`, `/api/v1/lake/blocks`, and `/ipfs/{cid}`. The response headers
`X-Yataverse-Inventory-Scope: dated-full` and
`X-Yataverse-Bytes-Scope: local-pinned-only` distinguish inventory coverage
from locally copied bytes. Public read responses have wildcard CORS so a
browser can fetch these separate HTTPS origins without Cloudflare; no
credentialed requests or writes are offered. A public `/health` response still
describes the inventory, not full block availability. This temporary hostname does not
replace `yataverse.com` and the shared router is a failure domain.

`deploy/gad-public-lake-{http,}.conf` provides a standby virtual host for
the same public hostname, proxying gad's loopback reader on port 18090. gad
obtains its own TLS certificate with the router's port 80 temporarily moved
to gad for HTTP-01, then the mapping returns to Xavier. When the public
8443 mapping is moved to gad, the same name can serve gad's locally pinned
subset. A long takeover must also move port 80 and enable gad's Certbot
renewal timer. Do not run both nodes' port mapping refreshers at once.

`deploy/build_independent_index.py` makes a new dated HTML document from the
exact 2026-09-26 apex snapshot. It refuses a changed source SHA-256, rewrites
all 50 HTTPS block links to this gateway's own `/ipfs/{cid}` route, and
replaces source claims about unavailable APIs and live generation. The output
is a separate CID, so the original CID retains its original bytes. The
2026-09-26 build was 17,826 bytes, SHA-256
`2fd28ac84111bd181090f4ea712f8988c80dbad9162edf52c108865a3b636e97`,
raw CID `bafkreibp2kfmqqirxumbbehu5jys7cmizag3vwiwf3pvfqiiqzndwy3os4`.
gad and Xavier both recursively pinned and read back those exact bytes. The
existing `yataverse-apex` IPNS key on both nodes now resolves to this CID,
and their Nginx root path proxies the local IPNS gateway. A public HTTPS GET
returned the new source hash and all 50 relative block links returned 200,
totaling 12,845,901 bytes from the own-node path. With Xavier's Nginx
stopped and the router's external 8443 mapping moved to gad, the same public
hostname returned the new HTML hash and a linked block; Xavier and its
mapping were then restored and rechecked. This is a manual gateway drill,
not automatic failover or proof that every lake block is replicated.
The document is a dated, read-only snapshot; changing the canonical
`yataverse.com` origin and updating snapshots remain separate work.

## Honest state (what this repo does NOT do yet)

- The murakumo overlay adapter (QUIC delivery of gossip forwards and
  bitswap blocks) is unlanded — ADR-2607023100 remains `proposed`.
- Availability proofs (`kotobase.peer.availability`) are not wired into
  the plan — replication today is declared, not continuously audited.
- Zone coordinates for geo ranking come from the caller; no zone→latlng
  registry is authoritative yet.
