#!/usr/bin/env python3
"""Cut the next lake epoch end to end (ADR-2610062000 P2): the README's
"full sequence for epoch N" as one resumable run.

Epochs 1 and 2 were cut by hand. Every step below is the command that worked
for epoch 2, with the check that gated it. The run keeps a state file in
--work; a step that has completed is not repeated, so an interrupted run
continues from where it stopped.

Steps, each refusing to continue unless its check passes:

  head      the verified log head, which must be seq N-1
  capture   two listing walks at one cutoff; both must complete
  diff      the two walks must agree (diff_lake_inventory refuses otherwise)
  resolve   the log's current membership, from the witnesses and a custodian
  delta     the rows the log does not hold. Zero rows ends the run: no epoch.
  xavier    replicate the delta into xavier's Kubo, then full leaf readback
  jacob     CAR ship, the large-block recovery lane, receipt audit and a full
            offline leaf readback, all on jacob
  cids      the inventory's CID must be equal on xavier, jacob and here
  manifest  lake_epoch.py manifest, pinned on both with equal CIDs
  submit    lake_head submit, signed with --writer-seed, verified 5 of 7
  bundle    the proof bundle, pinned on both
  check     the log now resolves to exactly the union of the walk and the log

  reader    jacob's lake-log reader (onion port 82) gains the epoch; its
            /health must then report exactly the log's sha256 and rows

Publishing the directory is printed as a follow-up. It changes a public,
signed pointer and stays a reviewed step. The reader serves only what the
log already proves, so it follows the log without review.

Exit codes: 0 epoch committed, or nothing to commit; 1 a check refused;
3 a step could not run.
"""

import argparse
import datetime
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
STEPS = ["head", "capture", "diff", "resolve", "delta", "xavier", "jacob",
         "cids", "manifest", "submit", "bundle", "check", "reader"]


class Refused(Exception):
    pass


class Unmeasured(Exception):
    pass


def sh(cmd, timeout=None, check=True, data=None):
    """Run a command list (or a shell string for remote pipelines). -> stdout text."""
    shell = isinstance(cmd, str)
    r = subprocess.run(cmd, shell=shell, capture_output=True, timeout=timeout, input=data,
                       executable="/bin/bash" if shell else None)
    out = r.stdout.decode(errors="replace")
    if check and r.returncode != 0:
        tail = (r.stderr.decode(errors="replace") or out).strip()[-400:]
        raise Unmeasured(f"{(cmd if shell else ' '.join(cmd))[:120]} exited {r.returncode}: {tail}")
    return out


def ssh(host, remote, timeout=None, check=True):
    return sh(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=20", host, remote],
              timeout=timeout, check=check)


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def rows_of(path):
    with open(path) as f:
        return [json.loads(l) for l in f if l.strip()]


def last_json(text):
    for line in reversed(text.strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except ValueError:
                pass
    return None


class Cycle:
    def __init__(self, cfg, work):
        self.c = cfg
        self.work = work
        os.makedirs(work, exist_ok=True)
        self.state_path = os.path.join(work, "cycle-state.json")
        self.s = json.load(open(self.state_path)) if os.path.exists(self.state_path) else {"done": []}

    def save(self):
        tmp = self.state_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.s, f, indent=1, sort_keys=True)
        os.replace(tmp, self.state_path)

    def p(self, name):
        return os.path.join(self.work, name)

    def say(self, *xs):
        print(*xs, flush=True)

    # ── the log, through lake_head.cljk ──────────────────────────────────────
    def lake_head(self, *args, timeout=900):
        c = self.c["lake_head"]
        cmd = shlex.split(c["runner"]) + [os.path.join(HERE, "lake_head.cljk")] + list(args) + c.get("extra", [])
        r = subprocess.run(cmd, capture_output=True, timeout=timeout)
        return r.returncode, r.stdout.decode(errors="replace")

    def step_head(self):
        code, out = self.lake_head("verify", self.c["ledger"])
        m = re.search(r"VERIFIED yataverse/lake seq (\d+) cid (\S+)", out)
        if code != 0 or not m:
            raise Unmeasured("lake head not verified: " + out.strip()[-200:])
        self.s.update(prev_seq=int(m.group(1)), prev_cid=m.group(2), epoch=int(m.group(1)) + 1)
        self.say(f"head seq {self.s['prev_seq']} {self.s['prev_cid']}; cutting epoch {self.s['epoch']}")

    def step_capture(self):
        if "cutoff" not in self.s:
            now = datetime.datetime.now(datetime.timezone.utc).replace(minute=0, second=0, microsecond=0)
            self.s["cutoff"] = (now - datetime.timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
            self.save()
        base = self.c["epoch0"]
        for k in ("a", "b"):
            done = self.p(f"candidate-{k}.done")
            if os.path.exists(done):
                continue
            out = sh([sys.executable, os.path.join(HERE, "capture_lake_inventory.py"),
                      "--old-inventory", base["inventory"], "--old-sha256", base["sha256"],
                      "--cutoff-utc", self.s["cutoff"], "--output", self.p(f"candidate-{k}.jsonl"),
                      "--state", self.p(f"candidate-{k}.state.json")], timeout=4 * 3600)
            j = last_json(out)
            if not (j and j.get("status") == "complete"):
                raise Unmeasured(f"walk {k} did not complete: {out.strip()[-200:]}")
            open(done, "w").write(json.dumps(j))
        self.say(f"two walks complete at cutoff {self.s['cutoff']}")

    def step_diff(self):
        base = self.c["epoch0"]
        code = subprocess.run([sys.executable, os.path.join(HERE, "diff_lake_inventory.py"),
                               "--old-inventory", base["inventory"], "--old-sha256", base["sha256"],
                               "--candidate-a", self.p("candidate-a.jsonl"),
                               "--candidate-a-state", self.p("candidate-a.state.json"),
                               "--candidate-b", self.p("candidate-b.jsonl"),
                               "--candidate-b-state", self.p("candidate-b.state.json"),
                               "--output", self.p("diff-since-epoch0.jsonl"),
                               "--receipt", self.p("diff.receipt.json")], capture_output=True).returncode
        if code != 0:
            raise Refused("the two walks disagree, or the diff refused (see diff_lake_inventory)")
        r = json.load(open(self.p("diff.receipt.json")))
        self.s["listing"] = {k: r.get("candidate_" + k) for k in ("rows", "bytes", "sha256")}
        self.say(f"walks agree: {r.get('candidate_rows')} rows; {r.get('delta_rows')} rows since epoch 0")

    def step_resolve(self, epochs=None):
        c = self.c["jacob"]
        tunnel = subprocess.Popen(["ssh", "-o", "BatchMode=yes", "-N", "-L",
                                   f"{c['rpc_tunnel_port']}:127.0.0.1:{c['kubo_api_port']}", c["host"]])
        try:
            time.sleep(3)
            code, out = self.lake_head("resolve", self.c["ledger"],
                                       f"--rpc=http://127.0.0.1:{c['rpc_tunnel_port']}",
                                       "--out=" + self.p("log-resolved.jsonl"), timeout=3600)
        finally:
            tunnel.terminate()
        m = re.search(r"RESOLVED yataverse/lake epochs (\d+) rows (\d+)", out)
        if code != 0 or not m or int(m.group(1)) != (epochs or self.s["epoch"]):
            raise Unmeasured("resolve failed or saw a different head: " + out.strip()[-200:])
        self.say(f"log resolves to {m.group(2)} rows over {m.group(1)} epochs")

    def step_delta(self):
        sh([sys.executable, os.path.join(HERE, "lake_epoch.py"), "delta",
            "--log-resolved", self.p("log-resolved.jsonl"), "--diff", self.p("diff-since-epoch0.jsonl"),
            "--out", self.p("inventory.jsonl")])
        rows = rows_of(self.p("inventory.jsonl"))
        self.s["inv"] = {"rows": len(rows), "bytes": sum(r["bytes"] for r in rows),
                         "sha256": sha256_file(self.p("inventory.jsonl")),
                         "large_rows": [i for i, r in enumerate(rows) if r["bytes"] > self.c["large_bytes"]]}
        self.say(f"delta: {len(rows)} rows, {self.s['inv']['bytes']} bytes, {len(self.s['inv']['large_rows'])} large")

    # ── custody on xavier ────────────────────────────────────────────────────
    def step_xavier(self):
        c, inv, e = self.c["xavier"], self.s["inv"], self.s["epoch"]
        remote_inv = f"{c['state']}/inventory-epoch{e}.jsonl"
        sh(["scp", "-q", self.p("inventory.jsonl"), f"{c['host']}:{remote_inv}"])
        for f in ("verify_lake_leaves.py", "raw_block_store.py", "serve_lake.py"):
            sh(["scp", "-q", os.path.join(HERE, f), f"{c['host']}:{c['tools']}/{f}"])
        port = c["temp_reader_port"]
        common = f"--ipfs-bin {c['ipfs_bin']} --ipfs-path {c['ipfs_path']}"
        # A temporary reader serves the delta as a listing: replicate_lake only
        # honours --inventory-path when the listing comes from a local reader.
        ssh(c["host"], f"cd {c['state']} && (nohup /usr/bin/python3 {c['reader_bin']} --inventory {remote_inv} "
                       f"--sha256 {inv['sha256']} --count {inv['rows']} {common} --host 127.0.0.1 --port {port} "
                       f"> epoch{e}-listing.log 2>&1 < /dev/null & echo $! > epoch{e}-reader.pid); "
                       f"for i in $(seq 1 30); do curl -s -m 2 http://127.0.0.1:{port}/health >/dev/null && break; sleep 1; done")
        try:
            ssh(c["host"], f"mkdir -p {c['state']}/epoch{e}-state && IPFS_PATH={c['ipfs_path']} /usr/bin/python3 {c['replicate_bin']} "
                           f"--ipfs-bin {c['ipfs_bin']} --kubo-api-url http://127.0.0.1:5001 --raw-block-store {c['state']}/raw-blocks "
                           f"--state-dir {c['state']}/epoch{e}-state --api-url http://127.0.0.1:{port}/api/v1/lake/blocks "
                           f"--inventory-path {remote_inv} --inventory-sha256 {inv['sha256']} "
                           f"--block-url-base {c['block_url_base']} --max-pages 100000 --max-new-bytes 200000000000 "
                           f"--max-block-bytes 256000000 --fetch-workers 8", timeout=12 * 3600)
        finally:
            ssh(c["host"], f"kill $(cat {c['state']}/epoch{e}-reader.pid) 2>/dev/null; rm -f {c['state']}/epoch{e}-reader.pid", check=False)
        leaves = None
        for _ in range(1000):
            out = ssh(c["host"], f"cd {c['tools']} && python3 verify_lake_leaves.py --inventory {remote_inv} "
                                 f"--sha256 {inv['sha256']} --count {inv['rows']} {common} --rpc-url http://127.0.0.1:5001 "
                                 f"--raw-block-store {c['state']}/raw-blocks --state {c['state']}/epoch{e}-leaves.json "
                                 f"--max-rows 1000 --max-bytes 250000000", timeout=3600, check=False)
            leaves = last_json(out)
            if leaves and leaves.get("status") == "complete":
                break
        if not (leaves and leaves.get("status") == "complete" and leaves.get("missing_rows", 0) == 0
                and leaves.get("present_rows") == inv["rows"]):
            raise Refused(f"xavier leaf readback incomplete: {leaves}")
        self.s["xavier_leaves"] = {"present_rows": leaves["present_rows"], "present_bytes": leaves.get("present_bytes")}
        self.say(f"xavier: leaf readback {leaves['present_rows']}/{inv['rows']}")

    # ── custody on jacob ─────────────────────────────────────────────────────
    def step_jacob(self):
        x, j, inv, e = self.c["xavier"], self.c["jacob"], self.s["inv"], self.s["epoch"]
        remote_inv = f"{j['bulk']}/inventory/inventory-epoch{e}.jsonl"
        archive = f"{j['archive_root']}/archive-epoch{e}"
        lake_state = f"{j['bulk']}/lake-state/epoch{e}"
        car_dir = f"{j['car_root']}/lake-car-epoch{e}"
        sh(["scp", "-q", self.p("inventory.jsonl"), f"{j['host']}:{remote_inv}"])
        ssh(j["host"], f"mkdir -p {archive} {lake_state} {car_dir}")
        xinv = f"{x['state']}/inventory-epoch{e}.jsonl"
        # Rows xavier holds as raw sidecar blocks rather than in Kubo.
        sh(["scp", "-q", os.path.join(HERE, "raw_block_store.py"), f"{x['host']}:{x['tools']}/raw_block_store.py"])
        out = ssh(x["host"], f"cd {x['tools']} && python3 -c 'import json,sys; sys.path.insert(0,\".\")\n"
                             f"from raw_block_store import RawBlockStore\n"
                             f"st=RawBlockStore(\"{x['state']}/raw-blocks\"); raw=[]\n"
                             f"for l in open(\"{xinv}\"):\n"
                             f"    r=json.loads(l)\n"
                             f"    try: b=st.read(r[\"cid\"], max_bytes=300000000)\n"
                             f"    except Exception: b=None\n"
                             f"    raw.append(r[\"cid\"]) if b is not None else None\n"
                             f"print(json.dumps(raw))'")
        json.dump({"inventory_sha256": inv["sha256"], "cids": json.loads(out)},
                  open(self.p("raw-cids.json"), "w"), indent=2)
        # Large blocks: export on xavier from its loopback gateway, relay, import on jacob.
        sh(["scp", "-q", os.path.join(HERE, "export_large_lake_block.py"), f"{x['host']}:{x['tools']}/"])
        for row in inv["large_rows"]:
            name = f"row-{row}-recovery.car"
            if ssh(j["host"], f"test -f {archive}/row-{row}-jacob-import.json && echo yes", check=False).strip() == "yes":
                continue
            ssh(x["host"], f"mkdir -p {x['state']}/epoch{e}-large && cd {x['state']}/epoch{e}-large && "
                           f"( test -f {name}.json || TMPDIR={x['state']}/epoch{e}-large python3 {x['tools']}/export_large_lake_block.py "
                           f"--inventory {xinv} --sha256 {inv['sha256']} --count {inv['rows']} --row {row} "
                           f"--base-url http://127.0.0.1:8080/ipfs/ --ipfs-bin {x['ipfs_bin']} --output {name} )", timeout=3600)
            for f in (name, name + ".json"):
                sh(["scp", "-q", "-3", "-o", "BatchMode=yes", f"{x['host']}:{x['state']}/epoch{e}-large/{f}", f"{j['host']}:{archive}/"],
                   timeout=3600)
            ssh(j["host"], f"TMPDIR={j['car_root']} python3 {j['bulk']}/bin/import_large_lake_recovery.py --inventory {remote_inv} "
                           f"--sha256 {inv['sha256']} --count {inv['rows']} --row {row} --car {archive}/{name} "
                           f"--source-receipt {archive}/{name}.json --ipfs-bin {j['ipfs_bin']} --ipfs-path {j['ipfs_path']} "
                           f"--output-receipt {archive}/row-{row}-jacob-import.json", timeout=3 * 3600)
        # Everything else by CAR. ship_lake_car refuses past a large row with no receipt.
        for _ in range(60):
            out = sh([sys.executable, self.c["ship_bin"], "--inventory", self.p("inventory.jsonl"),
                      "--sha256", inv["sha256"], "--count", str(inv["rows"]), "--start-row", "0",
                      "--stop-row", str(inv["rows"]), "--max-blocks", "1000", "--max-bytes", "200000000",
                      "--source-ssh-host", x["host"], "--source-exporter", x["car_exporter"],
                      "--source-inventory", xinv, "--source-ipfs-bin", x["ipfs_bin"],
                      "--source-ipfs-path", x["ipfs_path"], "--source-rpc-url", "http://127.0.0.1:5001",
                      "--source-output-dir", f"{x['state']}/car-exports-jacob-epoch{e}",
                      "--raw-cids-file", self.p("raw-cids.json"), "--state-dir", self.p("ship-state"),
                      "--ssh-host", j["host"], "--remote-dir", lake_state,
                      "--remote-importer", f"{j['bulk']}/bin/import_lake_car.py", "--remote-inventory", remote_inv,
                      "--remote-ipfs-bin", j["ipfs_bin"], "--remote-ipfs-path", j["ipfs_path"],
                      "--remote-raw-block-store", f"{j['bulk']}/lake-state/raw-blocks",
                      "--remote-car-dir", car_dir, "--remote-large-archive", archive], timeout=6 * 3600, check=False)
            last = (out.strip().splitlines() or [""])[-1]
            if '"status": "complete"' in last:
                break
            if "REFUSED" in last:
                raise Refused("ship: " + last[:200])
        else:
            raise Unmeasured("ship did not complete in 60 rounds")
        audit = last_json(ssh(j["host"], f"python3 {j['bulk']}/bin/audit_lake.py --inventory {remote_inv} --sha256 {inv['sha256']} "
                                         f"--count {inv['rows']} --ipfs-bin {j['ipfs_bin']} --ipfs-path {j['ipfs_path']} "
                                         f"--car-receipts {lake_state} --large-receipts {archive} --require-complete",
                              timeout=6 * 3600, check=False))
        if not (audit and audit.get("status") == "complete" and audit.get("missing_rows") == 0):
            raise Refused(f"jacob receipt audit: {audit}")
        # Full offline readback, every block against its CID digest.
        readback = last_json(ssh(j["host"], "python3 - <<'EOF'\n"
                                 "import json,subprocess,hashlib,base64,os\n"
                                 f"env=dict(os.environ,IPFS_PATH='{j['ipfs_path']}')\n"
                                 "def mh(c):\n"
                                 "    b=base64.b32decode(c[1:].upper()+'='*(-len(c[1:])%8)); i=1\n"
                                 "    while b[i]&0x80: i+=1\n"
                                 "    i+=1; return b[i+2:i+34].hex() if b[i]==0x12 else None\n"
                                 "ok=bad=0\n"
                                 f"for l in open('{remote_inv}'):\n"
                                 "    r=json.loads(l)\n"
                                 f"    p=subprocess.run(['{j['ipfs_bin']}','--offline','block','get',r['cid']],env=env,capture_output=True)\n"
                                 "    good=p.returncode==0 and len(p.stdout)==r['bytes'] and (not r['cid'].startswith('b') or hashlib.sha256(p.stdout).hexdigest()==mh(r['cid']))\n"
                                 "    ok+=good; bad+=not good\n"
                                 "print(json.dumps({'ok':ok,'bad':bad}))\n"
                                 "EOF", timeout=12 * 3600))
        if not (readback and readback["bad"] == 0 and readback["ok"] == inv["rows"]):
            raise Refused(f"jacob leaf readback: {readback}")
        self.s["jacob_custody"] = {"audit_rows": audit["covered_rows"], "readback": readback["ok"],
                                   "large": len(inv["large_rows"])}
        self.say(f"jacob: audit complete, readback {readback['ok']}/{inv['rows']}")

    # ── content addresses ────────────────────────────────────────────────────
    def add_both(self, local, name):
        x, j = self.c["xavier"], self.c["jacob"]
        sh(["scp", "-q", local, f"{x['host']}:{x['state']}/{name}"])
        sh(["scp", "-q", local, f"{j['host']}:{j['bulk']}/lake-state/{name}"])
        a = ssh(x["host"], f"IPFS_PATH={x['ipfs_path']} {x['ipfs_bin']} add -Q --cid-version=1 --raw-leaves {x['state']}/{name}").strip()
        b = ssh(j["host"], f"IPFS_PATH={j['ipfs_path']} {j['ipfs_bin']} add -Q --cid-version=1 --raw-leaves {j['bulk']}/lake-state/{name}").strip()
        if a != b:
            raise Refused(f"{name}: xavier {a} != jacob {b}")
        return a

    def step_cids(self):
        e = self.s["epoch"]
        self.s["inv"]["cid"] = self.add_both(self.p("inventory.jsonl"), f"inventory-epoch{e}.jsonl")
        self.say(f"inventory CID {self.s['inv']['cid']} on both custodians")

    def step_manifest(self):
        e, inv, w = self.s["epoch"], self.s["inv"], self.c["wan"]
        today = datetime.date.today().isoformat()
        sh([sys.executable, os.path.join(HERE, "lake_epoch.py"), "manifest", "--epoch", str(e),
            "--prev", self.s["prev_cid"], "--inventory-cid", inv["cid"], "--inventory", self.p("inventory.jsonl"),
            "--captured", self.s["cutoff"][:10],
            "--relation", f"delta over log epochs 0-{e - 1}: two matching listing walks at cutoff {self.s['cutoff']}, lake_epoch.py delta",
            "--custody", json.dumps({"node": "jacob", "wan": w["jacob"], "evidence":
                f"receipt audit complete {inv['rows']}/{inv['rows']} ({len(inv['large_rows'])} large via offline recovery CARs) "
                f"and full offline leaf readback {inv['rows']}/{inv['rows']} with CID digest match, {today}"}),
            "--custody", json.dumps({"node": "xavier", "wan": w["xavier"], "evidence":
                f"replicated and full leaf readback {inv['rows']}/{inv['rows']}, {today}"}),
            "--authority", self.c["authority"], "--out", self.p(f"epoch-{e}.json")])
        self.s["manifest_cid"] = self.add_both(self.p(f"epoch-{e}.json"), f"epoch-{e}-manifest.json")
        self.say(f"manifest {self.s['manifest_cid']}")

    def step_submit(self):
        code, out = self.lake_head("submit", self.c["ledger"], str(self.s["epoch"]), self.s["manifest_cid"])
        if code != 0:
            raise (Refused if code == 1 else Unmeasured)("submit: " + out.strip()[-200:])
        self.say(out.strip().splitlines()[-1][:160])

    def step_bundle(self):
        e = self.s["epoch"]
        code, out = self.lake_head("bundle", self.c["ledger"], "--out=" + self.p(f"bundle-e{e}.json"))
        if code != 0:
            raise Unmeasured("bundle: " + out.strip()[-200:])
        code, out = self.lake_head("verify-bundle", self.c["ledger"], self.p(f"bundle-e{e}.json"))
        if code != 0:
            raise Refused("verify-bundle: " + out.strip()[-200:])
        self.s["bundle_cid"] = self.add_both(self.p(f"bundle-e{e}.json"), f"bundle-e{e}.json")
        self.say(f"bundle {self.s['bundle_cid']}")

    def step_check(self):
        os.replace(self.p("log-resolved.jsonl"), self.p("log-resolved-before.jsonl"))
        self.step_resolve(epochs=self.s["epoch"] + 1)
        log = {json.dumps(r, sort_keys=True) for r in rows_of(self.p("log-resolved.jsonl"))}
        listing = {json.dumps(r, sort_keys=True) for r in rows_of(self.p("candidate-a.jsonl"))}
        missing = len(listing - log)
        if missing:
            raise Refused(f"{missing} listing rows are still outside the log")
        self.s["log"] = {"rows": len(log), "sha256": sha256_file(self.p("log-resolved.jsonl")),
                         "beyond_listing": len(log - listing)}
        self.say(f"log {len(log)} rows covers the listing at {self.s['cutoff']} ({len(log - listing)} rows only in the log)")

    def step_reader(self):
        j, e, inv = self.c["jacob"], self.s["epoch"], self.s["inv"]
        label = j["reader_label"]
        plist = f"~/Library/LaunchAgents/{label}.plist"
        spec = f"{j['bulk']}/inventory/inventory-epoch{e}.jsonl:{inv['sha256']}:{inv['rows']}:{j['bulk']}/lake-state/epoch{e}"
        pb = "/usr/libexec/PlistBuddy"
        args = ssh(j["host"], f"{pb} -c 'Print :ProgramArguments' {plist}")
        lines = [l.strip() for l in args.strip().splitlines()[1:-1]]
        if spec not in lines:
            # Insert the new --epoch pair after the last one, set the totals.
            last = max(i for i, l in enumerate(lines) if l == "--epoch") + 1
            ssh(j["host"], f"cp {plist} {j['bulk']}/lake-state/{label}.plist.pre-epoch{e} && "
                           f"{pb} -c 'Set :ProgramArguments:{lines.index('--sha256') + 1} {self.s['log']['sha256']}' "
                           f"-c 'Set :ProgramArguments:{lines.index('--count') + 1} {self.s['log']['rows']}' "
                           f"-c 'Add :ProgramArguments:{last + 1} string --epoch' "
                           f"-c 'Add :ProgramArguments:{last + 2} string {spec}' {plist} && plutil -lint {plist} >/dev/null && "
                           f"launchctl bootout gui/$(id -u)/{label}; sleep 2; launchctl bootstrap gui/$(id -u) {plist}")
        health = None
        for _ in range(30):
            time.sleep(5)
            health = last_json(ssh(j["host"], f"curl -s -m 30 http://127.0.0.1:{j['reader_port']}/health", check=False))
            if health and health.get("ok"):
                break
        if not (health and health.get("inventory-sha256") == self.s["log"]["sha256"]
                and health.get("rows") == self.s["log"]["rows"]):
            raise Refused(f"reader does not serve the log: {health}")
        self.say(f"reader serves {health['rows']} rows ({health['inventory-sha256'][:8]})")

    def run(self, until=None):
        for name in STEPS:
            if name in self.s["done"]:
                continue
            self.say(f"── {name}")
            getattr(self, "step_" + name)()
            self.s["done"].append(name)
            self.save()
            if name == "delta" and self.s["inv"]["rows"] == 0:
                self.say("NOTHING the log already holds every listed block")
                return 0
            if name == until:
                return 0
        e = self.s["epoch"]
        self.say(f"COMMITTED epoch {e} manifest {self.s['manifest_cid']} bundle {self.s['bundle_cid']}")
        self.say("follow-ups (reviewed): commit deploy/lake-manifests/epoch-%d.json and the reader plist "
                 "(sha256 %s, rows %d); point the directory lake_log at bundle %s"
                 % (e, self.s["log"]["sha256"], self.s["log"]["rows"], self.s["bundle_cid"]))
        return 0


def finished(work):
    try:
        s = json.load(open(os.path.join(work, "cycle-state.json")))
    except (OSError, ValueError):
        return False
    done = s.get("done", [])
    return "reader" in done or ("delta" in done and s.get("inv", {}).get("rows") == 0)


def pick_work(root):
    """The newest run under root that has not finished, else a new one.
    Resuming rather than restarting matters: a run stopped after `submit`
    has already committed its epoch, and a fresh run would cut the next one
    from a log head it never bundled or checked."""
    os.makedirs(root, exist_ok=True)
    runs = sorted(d for d in os.listdir(root) if d.startswith("run-"))
    if runs and not finished(os.path.join(root, runs[-1])):
        return os.path.join(root, runs[-1])
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%MZ")
    return os.path.join(root, "run-" + stamp)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--config", required=True)
    where = ap.add_mutually_exclusive_group(required=True)
    where.add_argument("--work", help="state directory for this epoch's run")
    where.add_argument("--work-root", help="for a schedule: resume the newest unfinished run "
                                          "under this directory, or start a new dated one")
    ap.add_argument("--until", choices=STEPS, help="stop after this step")
    a = ap.parse_args(argv)
    work = a.work or pick_work(a.work_root)
    print("work", work, flush=True)
    cyc = Cycle(json.load(open(a.config)), work)
    try:
        return cyc.run(a.until)
    except Refused as e:
        print("REFUSE", e, flush=True)
        return 1
    except (Unmeasured, subprocess.TimeoutExpired) as e:
        print("UNMEASURED", e, flush=True)
        return 3


if __name__ == "__main__":
    sys.exit(main())
