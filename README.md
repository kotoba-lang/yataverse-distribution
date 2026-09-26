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
blocks after checking their size. The source listing and bytes endpoint
still depend on Cloudflare; this is a physical copy path, not an independent
read API or a public gateway.

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
direct/recursive pins and, when `--raw-block-store` is supplied, checks the
CID and bytes of each raw sidecar block. It refuses an absent or changed
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

For the first public read entry, `deploy/xavier-public-lake.conf` exposes this
same local service at `https://yataverse-data.220-146-170-114.sslip.io:8443/`
through Xavier's Nginx and the shared router mapping (public 8443 to Xavier
443). Issue a certificate for that exact name using the port-80 webroot
challenge before enabling the HTTPS server; retain the Isekai virtual host on
the same listener. The Nginx config limits each source address to 8 requests
per second and 8 simultaneous connections, permits GET only, and serves only
`/health`, `/api/v1/lake/blocks`, and `/ipfs/{cid}`. The response headers
`X-Yataverse-Inventory-Scope: dated-full` and
`X-Yataverse-Bytes-Scope: local-pinned-only` distinguish inventory coverage
from locally copied bytes. A public `/health` response still describes the
inventory, not full block availability. This temporary hostname does not
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
