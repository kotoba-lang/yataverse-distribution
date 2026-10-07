#!/usr/bin/env python3
"""ADR-2610062000 P6: one authenticated write, committed and read back,
with Cloudflare never contacted, proven by observation.

The write is a lake drill epoch. Its single block is a signed agent action,
shaped after Holochain's source chain: the lake operator (one agent) appends
an action that names its author, its sequence on the author's chain, the
previous action, and the entry it carries. The action is self-validating:
anyone holding the block can check the signature against the author's
did:key without asking anyone. The witnesses play Holochain's validation
authorities: they apply the pinned writer policy (the DNA's integrity rules)
and their quorum certificate is the set of validation receipts.

Steps, each refusing to continue unless its check passes:

  head      the verified log head, which must be seq N-1
  action    the signed drill action, built and self-verified here
  custody   the action block on xavier and jacob (local Kubo adds), equal
            CIDs, offline readback on both
  cids      the one-row inventory, equal CIDs on both custodians
  manifest  epoch N's manifest, labelled as the P6 drill, pinned on both
  submit    lake_head submit, signed with the operator seed, 5 of 7
  bundle    the proof bundle (the validation receipts), pinned on both
  resolve   the log now resolves to the previous rows plus the action
  reader    jacob's onion reader gains the epoch
  tier1     over Tor only: the onion reader's /health equals the log, and
            the action block it serves verifies (digest and signature)
  tier2     over libp2p only: an ephemeral Kubo with no bootstrap and no DNS,
            peers by raw IP, verifies the bundle offline and fetches the
            action block by bitswap
  observe   every outbound connection and name lookup of the run, checked
            against Cloudflare's published ranges: zero is the pass

Observation. Every child Python process records socket.connect and
getaddrinfo through an audit hook (net_audit/sitecustomize.py), every node
process through net.Socket#connect and dns.lookup (net_audit_hook.cjs);
both are complete for their process, not sampled. Processes that are neither
(ssh, kubo, curl, and the tor daemon the onion reads use) are sampled
with lsof every half second over the drill's process tree for the whole run. Root packet capture is not available on main-2; the receipt says so.

Exit codes: 0 the drill passed; 1 a check refused; 3 a step could not run.
"""

import argparse
import base64
import datetime
import hashlib
import ipaddress
import json
import os
import re
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from lake_epoch_cycle import Cycle, Refused, Unmeasured, last_json, rows_of, sh, ssh, sha256_file  # noqa: E402

STEPS = ["head", "action", "custody", "cids", "manifest", "submit", "bundle",
         "resolve", "reader", "tier1", "tier2", "observe"]
ACTION_SCHEMA = "yataverse-agent-action/v1"
ACTION_DOMAIN = b"yataverse/agent-action/v1\n"
B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


# ── the agent action ─────────────────────────────────────────────────────────
def b58(b):
    n, out = int.from_bytes(b, "big"), ""
    while n:
        n, r = divmod(n, 58)
        out = B58[r] + out
    return "1" * (len(b) - len(b.lstrip(b"\0"))) + out


def unb58(s):
    n = 0
    for c in s:
        n = n * 58 + B58.index(c)
    full = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return b"\0" * (len(s) - len(s.lstrip("1"))) + full


def did_of_pub(pub):
    return "did:key:z" + b58(b"\xed\x01" + pub)


def pub_of_did(did):
    raw = unb58(did[len("did:key:z"):])
    if raw[:2] != b"\xed\x01" or len(raw) != 34:
        raise ValueError("not an Ed25519 did:key")
    return raw[2:]


def canonical(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def signing_bytes(action):
    """Everything but the signature, under a domain tag."""
    return ACTION_DOMAIN + canonical({k: v for k, v in action.items() if k != "signature"})


def sign_action(action, seed_hex):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    sk = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(seed_hex))
    did = did_of_pub(sk.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw))
    if did != action["author"]:
        raise Refused(f"the seed is {did}, not the action's author {action['author']}")
    sig = sk.sign(signing_bytes(action))
    return dict(action, signature="z" + b58(sig))


def verify_action(block):
    """-> the action, or raise. Needs nothing but the block itself."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    action = json.loads(block)
    if canonical(action) != block:
        raise Refused("action block is not canonical JSON")
    if action.get("schema") != ACTION_SCHEMA:
        raise Refused(f"schema {action.get('schema')}")
    sig = unb58(action["signature"][1:])
    Ed25519PublicKey.from_public_bytes(pub_of_did(action["author"])).verify(sig, signing_bytes(action))
    return action


def raw_cid_digest(cid):
    raw = base64.b32decode(cid[1:].upper() + "=" * (-len(cid[1:]) % 8))
    if raw[:4] != b"\x01\x55\x12\x20":
        raise Refused(f"{cid} is not a CIDv1 raw sha2-256")
    return raw[4:36]


# ── observation ──────────────────────────────────────────────────────────────
class Netwatch:
    """lsof every half second over the drill's own process tree, plus the tor
    daemon its onion reads go through, for processes the audit hooks cannot
    reach. Other processes of this user (browsers, editors, agents) are not
    part of the drill and are not sampled. Records each distinct
    (command, remote)."""

    def __init__(self, out, extra_cmds=("tor",)):
        self.out, self.seen, self.stop_ev, self.samples = out, set(), threading.Event(), 0
        self.extra_cmds = extra_cmds

    def pids(self):
        table = [l.split(None, 2) for l in subprocess.run(["ps", "-axo", "pid=,ppid=,comm="], capture_output=True,
                                                            text=True).stdout.splitlines() if l.strip()]
        children, keep = {}, {os.getpid()}
        for pid, ppid, comm in table:
            children.setdefault(int(ppid), []).append(int(pid))
            if os.path.basename(comm) in self.extra_cmds:
                keep.add(int(pid))
        todo = [os.getpid()]
        while todo:
            for c in children.get(todo.pop(), []):
                if c not in keep:
                    keep.add(c)
                    todo.append(c)
        return keep

    def run(self):
        while not self.stop_ev.is_set():
            pids = ",".join(str(p) for p in sorted(self.pids()))
            r = subprocess.run(["lsof", "-nP", "-i", "-a", "-p", pids], capture_output=True, text=True, timeout=30)
            self.samples += 1
            for line in r.stdout.splitlines()[1:]:
                parts = line.split()
                name = next((p for p in parts if "->" in p), None)
                if not name:
                    continue
                remote = name.split("->", 1)[1]
                key = (parts[0], remote)
                if key not in self.seen:
                    self.seen.add(key)
                    with open(self.out, "a") as f:
                        f.write(json.dumps({"t": int(time.time() * 1000), "ev": "lsof", "cmd": parts[0],
                                            "pid": int(parts[1]), "remote": remote}) + "\n")
            self.stop_ev.wait(0.5)

    def start(self):
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_ev.set()
        self.thread.join(40)


def split_hostport(remote):
    if remote.startswith("["):
        host, _, port = remote[1:].partition("]:")
        return host, port
    host, _, port = remote.rpartition(":")
    return host, port


def classify(host, cf_nets):
    try:
        ip = ipaddress.ip_address(host.split("%")[0])
    except ValueError:
        return "name"
    if any(ip in n for n in cf_nets):
        return "cloudflare"
    if ip.is_loopback:
        return "loopback"
    if ip.version == 4 and ip in ipaddress.ip_network("100.64.0.0/10"):
        return "tailnet"
    if ip.version == 6 and ip in ipaddress.ip_network("fd7a:115c:a1e0::/48"):
        return "tailnet"
    if ip.is_private or ip.is_link_local:
        return "private"
    return "public"


def summarize(log_path, cf_nets):
    by_class, names, cf, public = {}, set(), [], set()
    for line in open(log_path):
        e = json.loads(line)
        if e["ev"] == "dns":
            if e["host"] not in ("localhost", "127.0.0.1", "::1"):
                names.add(e["host"])
            continue
        if e["ev"] == "exec" or e.get("path"):
            continue
        host = e.get("host") if e["ev"] == "connect" else split_hostport(e["remote"])[0]
        port = e.get("port") if e["ev"] == "connect" else split_hostport(e["remote"])[1]
        c = "loopback" if host == "localhost" else classify(str(host), cf_nets)
        by_class[c] = by_class.get(c, 0) + 1
        who = e.get("lang") or e.get("cmd")
        if c == "cloudflare":
            cf.append({"who": who, "host": host, "port": port})
        elif c in ("public", "name"):
            public.add(f"{who} {host}:{port}")
    return {"connections_by_class": by_class, "cloudflare": cf, "name_lookups": sorted(names),
            "public_remotes": sorted(public)}


# ── the drill ────────────────────────────────────────────────────────────────
class Drill(Cycle):
    def step_action(self):
        e = self.s["epoch"]
        if "action_block" not in self.s:
            action = {"schema": ACTION_SCHEMA, "author": self.c["operator"], "chain": "yataverse/lake",
                      "action_seq": e, "prev_action": self.s["prev_cid"],
                      "timestamp": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                      "entry_type": "p6-drill",
                      "entry": {"adr": "ADR-2610062000", "phase": "P6",
                                "claim": "this action was signed, committed by the witness quorum and read back "
                                         "over Tor and libp2p with Cloudflare never contacted"}}
            seed = open(self.c["seed"]).read().strip()
            block = canonical(sign_action(action, seed))
            open(self.p("action.json"), "wb").write(block)
            self.s["action_block"] = True
        block = open(self.p("action.json"), "rb").read()
        action = verify_action(block)
        self.s["action"] = {"sha256": hashlib.sha256(block).hexdigest(), "bytes": len(block),
                            "author": action["author"], "action_seq": action["action_seq"]}
        self.say(f"action seq {action['action_seq']} by {action['author'][:24]}… verifies, {len(block)} bytes")

    def step_custody(self):
        cid = self.add_both(self.p("action.json"), f"p6-drill-action-epoch{self.s['epoch']}.json")
        if raw_cid_digest(cid).hex() != self.s["action"]["sha256"]:
            raise Refused(f"{cid} does not address the action")
        x, j = self.c["xavier"], self.c["jacob"]
        for host, env in ((x["host"], f"IPFS_PATH={x['ipfs_path']} {x['ipfs_bin']}"),
                          (j["host"], f"IPFS_PATH={j['ipfs_path']} {j['ipfs_bin']}")):
            got = ssh(host, f"{env} --offline block get {cid} | shasum -a 256 | cut -c1-64").strip()
            if got != self.s["action"]["sha256"]:
                raise Refused(f"{host} reads back {got} for {cid}")
        self.s["action"]["cid"] = cid
        with open(self.p("inventory.jsonl"), "w") as f:
            f.write(json.dumps({"cid": cid, "bytes": self.s["action"]["bytes"]}, separators=(",", ":")) + "\n")
        self.s["inv"] = {"rows": 1, "bytes": self.s["action"]["bytes"], "large_rows": [],
                         "sha256": sha256_file(self.p("inventory.jsonl"))}
        self.say(f"action {cid} added (and so pinned) and read back offline on xavier and jacob")

    def step_manifest(self):
        e, inv, w = self.s["epoch"], self.s["inv"], self.c["wan"]
        today = datetime.date.today().isoformat()
        sh([sys.executable, os.path.join(HERE, "lake_epoch.py"), "manifest", "--epoch", str(e),
            "--prev", self.s["prev_cid"], "--inventory-cid", inv["cid"], "--inventory", self.p("inventory.jsonl"),
            "--captured", today,
            "--relation", "P6 drill (ADR-2610062000): one signed agent action, committed and read back "
                          "with Cloudflare not contacted; not a capture of the lake listing",
            "--custody", json.dumps({"node": "jacob", "wan": w["jacob"], "evidence": f"offline readback 1/1, pinned, {today}"}),
            "--custody", json.dumps({"node": "xavier", "wan": w["xavier"], "evidence": f"offline readback 1/1, pinned, {today}"}),
            "--authority", self.c["authority"], "--out", self.p(f"epoch-{e}.json")])
        self.s["manifest_cid"] = self.add_both(self.p(f"epoch-{e}.json"), f"epoch-{e}-manifest.json")
        self.say(f"manifest {self.s['manifest_cid']}")

    def step_resolve(self, epochs=None):
        if "rows_before" not in self.s:
            # jacob's reader still serves the log before this epoch (it gains
            # the epoch in the next step), so its row count is the "before".
            j = self.c["jacob"]
            h = last_json(ssh(j["host"], f"curl -s -m 30 http://127.0.0.1:{j['reader_port']}/health", check=False))
            if not (h and h.get("ok")):
                raise Unmeasured(f"reader health: {h}")
            self.s["rows_before"] = h["rows"]
            self.save()
        Cycle.step_resolve(self, epochs=self.s["epoch"] + 1)
        rows = rows_of(self.p("log-resolved.jsonl"))
        cids = {r["cid"] for r in rows}
        if len(rows) != self.s["rows_before"] + 1 or self.s["action"]["cid"] not in cids:
            raise Refused(f"log has {len(rows)} rows, expected {self.s['rows_before']} + the action")
        self.s["log"] = {"rows": len(rows), "sha256": sha256_file(self.p("log-resolved.jsonl"))}
        self.say(f"log resolves to {len(rows)} rows, the action included")

    def step_reader(self):
        j, e = self.c["jacob"], self.s["epoch"]
        sh(["scp", "-q", self.p("inventory.jsonl"), f"{j['host']}:{j['bulk']}/inventory/inventory-epoch{e}.jsonl"])
        ssh(j["host"], f"mkdir -p {j['bulk']}/lake-state/epoch{e}")
        Cycle.step_reader(self)

    def step_tier1(self):
        onion, port = self.c["onion"], self.c["jacob"]["onion_port"]
        socks = ["curl", "-s", "-m", "180", "--socks5-hostname", self.c["tor_socks"]]
        health = last_json(sh(socks + [f"http://{onion}:{port}/health"], check=False))
        if not (health and health.get("inventory-sha256") == self.s["log"]["sha256"]
                and health.get("rows") == self.s["log"]["rows"]):
            raise Refused(f"onion reader does not serve the log: {health}")
        r = subprocess.run(socks + [f"http://{onion}:{port}/ipfs/{self.s['action']['cid']}"], capture_output=True, timeout=240)
        block = r.stdout
        if hashlib.sha256(block).hexdigest() != self.s["action"]["sha256"]:
            raise Refused("the onion reader's action block does not match its CID")
        verify_action(block)
        self.s["tier1"] = {"onion": onion, "port": port, "rows": health["rows"],
                           "sha256": health["inventory-sha256"], "action": "digest and signature verify"}
        self.say(f"tier1: onion reader serves {health['rows']} rows; the action verifies over Tor")

    def step_tier2(self):
        out = sh([sys.executable, os.path.join(HERE, "independent_read_drill.py"),
                  "--ipfs-bin", self.c["ipfs_bin"], "--ledger-cid", self.c["ledger_cid"],
                  "--bundle", self.s["bundle_cid"], "--require-block", self.s["action"]["cid"],
                  "--verifier", self.c["lake_head"]["runner"] + " " + os.path.join(HERE, "lake_head.cljk"),
                  "--blocks", "3"] + [x for p in self.c["peers"] for x in ("--peer", p)],
                 timeout=3600, check=False)
        open(self.p("tier2.log"), "w").write(out)
        r = last_json(out)
        if not (r and r.get("passed") and r["head"]["seq"] == self.s["epoch"]):
            raise Refused("tier2: " + out.strip()[-300:])
        self.s["tier2"] = {"peers": r["peers"], "rows": r["rows"], "head": r["head"],
                           "steps": [s["step"] for s in r["steps"]]}
        self.say(f"tier2: libp2p read verifies seq {r['head']['seq']}, {r['rows']} rows, the action by bitswap")

    def step_observe(self):
        cf = [ipaddress.ip_network(l.strip()) for f in ("cf_ips_v4", "cf_ips_v6")
              for l in open(self.c[f]) if l.strip()]
        s = summarize(self.p("net-audit.jsonl"), cf)
        s["lsof_samples"] = self.s.get("lsof_samples", 0)
        self.s["observe"] = s
        if s["cloudflare"]:
            raise Refused(f"Cloudflare contacted: {s['cloudflare'][:5]}")
        if s["name_lookups"]:
            raise Refused(f"name lookups during the drill: {s['name_lookups'][:10]}")
        self.say(f"observe: 0 Cloudflare connections, 0 name lookups; {s['connections_by_class']}")

    def run(self, until=None):
        audit = self.p("net-audit.jsonl")
        os.environ["NET_AUDIT_LOG"] = audit
        os.environ["PYTHONPATH"] = os.path.join(HERE, "net_audit") + os.pathsep + os.environ.get("PYTHONPATH", "")
        os.environ["NODE_OPTIONS"] = "--require " + os.path.join(HERE, "net_audit", "net_audit_hook.cjs")
        watch = Netwatch(audit)
        watch.start()
        try:
            for name in STEPS:
                if name in self.s["done"]:
                    continue
                if name == "observe":
                    watch.stop()
                    self.s["lsof_samples"] = self.s.get("lsof_samples", 0) + watch.samples
                self.say(f"── {name}")
                getattr(self, "step_" + name)()
                self.s["done"].append(name)
                self.save()
                if name == until:
                    return 0
        finally:
            if not watch.stop_ev.is_set():
                watch.stop()
                self.s["lsof_samples"] = self.s.get("lsof_samples", 0) + watch.samples
                self.save()
        receipt = {"drill": "yataverse-p6", "passed": True, "epoch": self.s["epoch"],
                   "action": self.s["action"], "manifest": self.s["manifest_cid"], "bundle": self.s["bundle_cid"],
                   "log": self.s["log"], "tier1": self.s["tier1"], "tier2": self.s["tier2"],
                   "observe": self.s["observe"],
                   "observation_scope": "every python and node process by audit hook (complete); ssh, tor, kubo, "
                                        "curl and the tor daemon by lsof every 0.5 s over the drill process tree (sampled); no root packet capture on main-2",
                   "checked": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        open(self.p("receipt.json"), "w").write(json.dumps(receipt, indent=1, sort_keys=True))
        self.say(json.dumps(receipt, sort_keys=True))
        return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--config", required=True)
    ap.add_argument("--work", required=True)
    ap.add_argument("--until", choices=STEPS)
    a = ap.parse_args(argv)
    cfg = json.load(open(a.config))
    d = Drill(cfg, a.work)
    print("work", a.work, flush=True)
    try:
        return d.run(a.until)
    except Refused as e:
        d.save()
        print("REFUSED", e, flush=True)
        return 1
    except (Unmeasured, subprocess.TimeoutExpired, OSError) as e:
        d.save()
        print("UNMEASURED", e, flush=True)
        return 3


if __name__ == "__main__":
    sys.exit(main())
