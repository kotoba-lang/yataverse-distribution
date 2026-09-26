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

## Honest state (what this repo does NOT do yet)

- The murakumo overlay adapter (QUIC delivery of gossip forwards and
  bitswap blocks) is unlanded — ADR-2607023100 remains `proposed`.
- Availability proofs (`kotobase.peer.availability`) are not wired into
  the plan — replication today is declared, not continuously audited.
- Zone coordinates for geo ranking come from the caller; no zone→latlng
  registry is authoritative yet.
