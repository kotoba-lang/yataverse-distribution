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
    test/yataverse/distribution/test_runner.cljk
```

Explicit-namespace runner; exits non-zero on any failure. Current: 14
tests / 51 assertions, 0 failures.

## Honest state (what this repo does NOT do yet)

- The murakumo overlay adapter (QUIC delivery of gossip forwards and
  bitswap blocks) is unlanded — ADR-2607023100 remains `proposed`.
- Availability proofs (`kotobase.peer.availability`) are not wired into
  the plan — replication today is declared, not continuously audited.
- Zone coordinates for geo ranking come from the caller; no zone→latlng
  registry is authoritative yet.
