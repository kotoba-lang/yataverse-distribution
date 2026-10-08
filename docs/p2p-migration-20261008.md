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
not the Kubo administration API. The existing writer service was unchanged.

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

- Witness consensus still uses tailnet endpoints. Move client and witness
  transports to libp2p without changing pinned witness keys, quorum or
  signature verification; measure fresh signed commit and readback.
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
