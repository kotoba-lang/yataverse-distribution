#!/usr/bin/env python3
"""Read yataverse with no Cloudflare and no DNS, starting from one IPNS name.

A client that knows only the public directory's IPNS name and holds nothing
it was not handed out of band (the witness key ledger is fetched by CID, but
its CID must match the one pinned in the repository) should be able to:

  1. resolve the directory over the DHT,
  2. read the yataverse lake_log entry from it,
  3. fetch the proof bundle and verify it offline against the witness keys,
  4. walk the manifests from the head back to epoch 0, checking each
     inventory against its manifest's sha256 and row count,
  5. fetch sample lake blocks by CID over bitswap and check their digests.

The client is an ephemeral Kubo with no bootstrap list, AutoConf off (it fetches
config over HTTPS by DNS name), DHT-only routing (no delegated HTTP routers),
and exactly the peers given by raw IP multiaddr. Every step prints a line; the
last line is a JSON receipt. Exit 0 only when every step passed; 1 when a step
was refused; 3 when the drill could not run.
"""
import argparse
import hashlib
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time


class Drill:
    def __init__(self, ipfs_bin, peers):
        self.ipfs_bin = ipfs_bin
        self.peers = peers
        self.repo = tempfile.mkdtemp(prefix="yataverse-drill-")
        self.daemon = None
        self.steps = []

    def ipfs(self, *args, data=None, timeout=300):
        return subprocess.run([self.ipfs_bin, *args], input=data, capture_output=True,
                              timeout=timeout, env={"IPFS_PATH": self.repo, "PATH": "/usr/bin:/bin"})

    def step(self, name, ok, detail):
        self.steps.append({"step": name, "ok": bool(ok), "detail": detail})
        print(("PASS " if ok else "FAIL ") + name + ": " + detail, flush=True)
        return ok

    def start(self):
        self.ipfs("init", "--profile=test")
        for k, v in [("Bootstrap", "[]"), ("Routing.Type", '"dht"'), ("AutoConf.Enabled", "false"),
                     ("Addresses.API", '"/ip4/127.0.0.1/tcp/0"'), ("Addresses.Gateway", '"/ip4/127.0.0.1/tcp/0"'),
                     ("Addresses.Swarm", '["/ip4/0.0.0.0/tcp/0","/ip4/0.0.0.0/udp/0/quic-v1"]'),
                     ("Swarm.RelayClient.Enabled", "true"), ("Swarm.Transports.Network.Relay", "true")]:
            self.ipfs("config", "--json", k, v)
        self.daemon = subprocess.Popen([self.ipfs_bin, "daemon", "--enable-gc=false"],
                                       env={"IPFS_PATH": self.repo, "PATH": "/usr/bin:/bin"},
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        # `ipfs id` answers without a daemon; `swarm peers` needs the online one.
        for _ in range(90):
            if self.ipfs("swarm", "peers").returncode == 0:
                break
            time.sleep(1)
        connected = [p for p in self.peers if self.ipfs("swarm", "connect", p, timeout=60).returncode == 0]
        return self.step("peer", connected, f"connected {len(connected)}/{len(self.peers)} peers by raw IP multiaddr")

    def cat(self, cid):
        r = self.ipfs("cat", cid, timeout=600)
        return r.stdout if r.returncode == 0 else None

    def stop(self):
        if self.daemon:
            self.daemon.terminate()
            try:
                self.daemon.wait(20)
            except subprocess.TimeoutExpired:
                self.daemon.kill()
        shutil.rmtree(self.repo, ignore_errors=True)


def digest_of(cid_bytes_text):
    # Lake rows are CIDv0 (Qm...) or CIDv1 base32 with sha2-256; the digest is
    # recomputed from the CID text here rather than trusted from Kubo.
    import base64
    cid = cid_bytes_text
    if cid.startswith("Qm"):
        alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
        n = 0
        for c in cid:
            n = n * 58 + alphabet.index(c)
        return n.to_bytes(34, "big")[2:]
    raw = base64.b32decode(cid[1:].upper() + "=" * (-len(cid[1:]) % 8))
    i = 1
    while raw[i] & 0x80:
        i += 1
    i += 1  # codec varint (single byte for raw/dag-pb/dag-cbor)
    return raw[i + 2:i + 34]


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--ipfs-bin", default="ipfs")
    p.add_argument("--directory-ipns", required=True)
    p.add_argument("--peer", action="append", required=True, help="raw IP multiaddr; no /dns*")
    p.add_argument("--ledger-cid", required=True, help="the witness ledger CID pinned in the repository")
    p.add_argument("--verifier", required=True, help="command prefix that runs lake_head.cljk")
    p.add_argument("--blocks", type=int, default=6)
    a = p.parse_args(argv)
    if any("/dns" in peer for peer in a.peer):
        print("REFUSE a /dns multiaddr would make this drill depend on DNS")
        return 1
    d = Drill(a.ipfs_bin, a.peer)
    work = tempfile.mkdtemp(prefix="yataverse-drill-files-")
    try:
        if not d.start():
            return 3
        r = d.ipfs("name", "resolve", "--nocache", "/ipns/" + a.directory_ipns, timeout=300)
        html_path = r.stdout.decode().strip() if r.returncode == 0 else ""
        if not d.step("resolve-directory", html_path.startswith("/ipfs/"), html_path or r.stderr.decode()[-200:]):
            return 1
        html = d.cat(html_path[len("/ipfs/"):]) or b""
        m = re.search(rb"<code>(bafkrei[a-z2-7]+)</code>", html)
        json_cid = m.group(1).decode() if m else None
        directory = json.loads(d.cat(json_cid) or b"{}") if json_cid else {}
        yv = next((s for s in directory.get("services", []) if s.get("domain") == "yataverse.com"), {})
        lake = yv.get("lake_log") or {}
        if not d.step("directory-lake-log", lake.get("proof_bundle"), f"directory {json_cid} names bundle {lake.get('proof_bundle')}"):
            return 1
        if not d.step("ledger-pinned", lake.get("witness_ledger") == a.ledger_cid,
                      f"directory ledger {lake.get('witness_ledger')} vs repository {a.ledger_cid}"):
            return 1
        ledger, bundle = d.cat(a.ledger_cid), d.cat(lake["proof_bundle"])
        lp, bp = os.path.join(work, "ledger.edn"), os.path.join(work, "bundle.json")
        open(lp, "wb").write(ledger or b"")
        open(bp, "wb").write(bundle or b"")
        v = subprocess.run(a.verifier.split() + ["verify-bundle", lp, bp], capture_output=True, text=True, timeout=300)
        if not d.step("verify-bundle", v.returncode == 0 and "VERIFIED-BUNDLE" in v.stdout,
                      (v.stdout.strip().splitlines() or [v.stderr.strip()[-200:]])[-1][:200]):
            return 1
        head = json.loads(bundle)["head"]
        cid, seq, rows_total, inventories = head["cid"], head["seq"], 0, []
        while True:
            m_raw = d.cat(cid)
            man = json.loads(m_raw or b"{}")
            inv = man.get("inventory", {})
            data = d.cat(inv.get("cid", "")) or b""
            n = len([l for l in data.split(b"\n") if l])
            good = man.get("epoch") == seq and hashlib.sha256(data).hexdigest() == inv.get("sha256") and n == inv.get("rows")
            if not d.step(f"manifest-epoch-{seq}", good, f"{cid} inventory {inv.get('cid')} rows {n}"):
                return 1
            rows_total += n
            inventories.append(data)
            if seq == 0:
                break
            cid, seq = man["prev"], seq - 1
        rows = [json.loads(l) for data in inventories for l in data.split(b"\n") if l]
        sample = random.Random(len(rows)).sample([r for r in rows if r["bytes"] <= 2 * 1024 * 1024], a.blocks)
        ok_blocks = 0
        for row in sample:
            r = d.ipfs("block", "get", row["cid"], timeout=300)
            if r.returncode == 0 and len(r.stdout) == row["bytes"] and hashlib.sha256(r.stdout).digest() == digest_of(row["cid"]):
                ok_blocks += 1
        d.step("blocks", ok_blocks == len(sample), f"{ok_blocks}/{len(sample)} lake blocks over bitswap match their CID digests")
        passed = all(s["ok"] for s in d.steps)
        print(json.dumps({"drill": "yataverse-independent-read", "passed": passed, "rows": rows_total,
                          "head": head, "peers": a.peer, "dns": "none", "cloudflare": "none",
                          "checked": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "steps": d.steps}))
        return 0 if passed else 1
    finally:
        d.stop()
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
