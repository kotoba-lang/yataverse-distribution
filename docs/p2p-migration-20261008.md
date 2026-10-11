# Yataverse P2P migration — 2026-10-08

Yataverse is the public entrance; Kotobase remains the storage technology
name. Do not redeploy the retired `net-kotobase-*`, `kotobase-*`,
`kotodama-yoro`, `network-awai-apex`, `adserver`, `animeka-viewer`, or
`sekaiju-rooms` Workers to recover their former routes. The cleanup approved
29 exact Worker names; this retirement statement describes those existing
deployments, not authorization to delete future similarly named services.

## Activated and measured

PR #93 (`3cc51c1` main integration) adds the dedicated user service
`yataverse-reader-p2p-mount` on Xavier. It mounts the existing loopback reader
at port 8090 as `/x/yataverse/lake-reader/1`. The 30-second mount keeper
restores this protocol after Kubo restarts and leaves other protocols alone.
It exposes the reader's existing inventory and local-custody block API,
not the Kubo administration API.

At `2026-10-08T04:49:10.823Z`, an empty client Kubo connected to public
IPv4 `220.146.170.114:13515`, peer
`12D3KooWH5CfezpDdMtYttLLMeib526f7f8QKCC6jCgiHDme4XSV`.
Over a direct libp2p stream, it fetched 262158 bytes for
`QmNLgJDagPKXUg4C1vKtvETQhTxEDScNYwEhYBke5uBan8` and verified SHA-256
against the CID. The client used no bootstrap, routing, delegated routers,
DNS, Cloudflare or Tailscale. This proves that block and that reader path;
it does not establish full inventory custody or independent operators.

Reproduce using the deployed Kotoba SCI release:

```sh
node <release>/engine/cli.js --config <empty-config.edn> --classpath deploy \
  deploy/p2p_reader_drill.cljk --ipfs-bin=<ipfs> \
  --peer=/ip4/220.146.170.114/tcp/13515/p2p/12D3KooWH5CfezpDdMtYttLLMeib526f7f8QKCC6jCgiHDme4XSV \
  --cid=QmNLgJDagPKXUg4C1vKtvETQhTxEDScNYwEhYBke5uBan8
```

The IP/port is a dated observation, not service discovery. Use the current
public address announced by the pinned peer if the UPnP mapping changes.
The config file is `{}`; explicitly selecting it avoids inheriting the
workspace dependency resolver for these standalone scripts.

## HTTPS/DNS entrance — prepared, not activated

Authoritative DNS still uses Cloudflare. At the time of the probe, both
`yataverse.com` and `ipfs.yataverse.com` had no A response from 1.1.1.1.
Connecting the apex to Xavier by `curl --resolve` failed hostname validation:
the current certificate does not cover `yataverse.com`. DNS alone cannot
finish the entrance migration. Xavier's `sudo -n` requires a password;
no system Nginx or certificate configuration was modified.

Prepared files: `deploy/yataverse-entry-http.conf` (HTTP-01 setup) and
`deploy/yataverse-entry.conf` (HTTPS). An administrator must first install
the HTTP virtual host, validate/reload Nginx, and verify WAN port 80 reaches
Xavier. Then add DNS-only A records for `@` and `ipfs` to the verified WAN
address, issue a certificate for both names with the existing webroot
`/var/www/letsencrypt`, install the HTTPS virtual host, and validate/reload
Nginx. Keep certificate renewal scheduled. Confirm WAN port 443 reaches
this Nginx; use SNI to preserve the other existing sites.

Before completion, check authoritative DNS and a public resolver, certificate
hostname validation, apex HTML from the pinned IPNS name, inventory response,
and a CID-verified `/ipfs/<cid>` read through both names. The intended path
gateway is `https://ipfs.yataverse.com/ipfs/<cid>`; the old wildcard subdomain
gateway is not supplied by these configs. Do not route to a missing Worker.

## Remaining migration gates

- Writer-to-Witness client transport is now libp2p (PRs #95 and #96, main
  `799f004`). Xavier's active writer reads a derived ledger pointing to
  loopback forwards 13001–13007, each pinned to its Witness peer ID. The
  original chain, all seven public keys and writer policies are unchanged.
  The previous ledger is retained as `score-witness-v5-tailnet-before-20261008.edn`.
  A newly signed record for the dedicated acceptance ref
  `yataverse/graph/bafkreieezmuowwyshvjwdhme5ip5lwqrsz4t5utc7bgd4r6h3jyqd75gbi`
  committed at sequence 0 in 33,756 ms, verified by w1/w2/w4/w6/w7. A subsequent
  bundle fetch returned five proofs; local offline `verify-bundle` validated
  all five against the canonical network-isekai pinned ledger. An unsigned
  sequence-1 probe received the existing mempool acknowledgement (HTTP 200),
  but the subsequent five-Witness verified projection remained at signed
  sequence 0. Ingress acceptance is not writer admission or a committed head;
  authorization is enforced by the writer-policy projection. The
  production lake and graph heads were not changed. This proves new signed
  consensus over the P2P client path, not fixture-byte custody: a separate
  Kubo readback of the fixture timed out. w3/w5 remain unreachable; do not
  restart any of the five healthy Witnesses. Witness listener addresses and
  recovery of those two nodes still require work before full tailnet removal.
- Add a separately reachable reader/custodian and verify fallback under a
  controlled failure only after confirming fresh quorum and custody.
- Recover consumers of deleted authn/IPFS bindings: `itonami-app-auth`,
  `local-murakumo`, `kotoba-cloud-control-plane`, `app-aozora-auth`,
  `nexus-x402`, `kotoba-cloud-database`, `etzhayyim-did-web`, `app-hyakka`.
  Authentication must keep signature/ownership checks and reject unauthenticated
  writes; Worker deletion is not authentication migration.
- Reconcile these consumers' deployment configuration and release automation
  so a subsequent release cannot silently recreate retired Workers.
- R2 bootstrap/follower paths and Cloudflare DNS authority remain dependencies.
  P2P data retrieval passing does not prove their complete removal.

## Consumer and authentication work prepared this session

Hyakka draft PR `network-awai/app-hyakka#1195` removes its deleted
`net-kotobase-ipfs` binding and uses the independent HTTPS Reader
`https://yataverse-data.220-146-170-114.sslip.io:8443/ipfs/<cid>` until the
canonical Yataverse DNS/TLS entry is qualified. Consumers validate raw/DAG-CBOR
CIDv1 SHA-256 before decoding, with size/time bounds and no redirects.
The actual fetch code retrieved and verified the game root (79,429 bytes)
and ledger root (5,250 bytes); corrupted bytes were refused.
This is not deployed. The existing `amu compile ... worker` build accepts no
bare build name. A follow-up using the actual entry and explicit module paths
also stops at unresolved `cljs.reader`; neither probe emitted a Worker bundle.
Keep the PR draft until the build and production read surface pass.

Authentication draft PR `cloud-kotoba/kotobase-control-plane#781` lets viewer,
inference and operator Biscuit verifiers use a pinned public root without an
issuer seed. Public discovery metadata also uses that pinned root without a
seed. The 15-test/88-assertion smoke suite passed, including explicit
wrong-key refusal, expiry, model/output limits and operator revocation.
Minting still requires issuer custody. The old Worker and AuthnStore remain
retired; public-key verification does not restore sessions, account records,
or a token issuer. No deployed auth consumer was switched by this source change.

Current source configurations still name the deleted `kotobase-authn` in
`local-murakumo`, `app-aozora-engine/auth`, `nexus-x402`, `app-kotoba-cloud`
and the API gateway. The gateway also still names the deleted staging graph
Worker. Those paths need local capability verification and the corresponding
P2P data contract before bindings can be removed safely. Passkey/session
lookup and minting must not be treated as public-key verification.
