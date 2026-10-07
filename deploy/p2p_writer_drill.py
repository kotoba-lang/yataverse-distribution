#!/usr/bin/env python3
"""Reach each yataverse-writer over libp2p only (ADR-2610062000, after Holochain).

The client is an ephemeral Kubo with no bootstrap list, AutoConf off and no
DNS: it knows only raw-IP multiaddrs, given on the command line. For each
writer peer ID it opens the protocol /x/yataverse/graph-head/1 with
`ipfs p2p forward`, then over that stream:

  read    GET /v1/graph-head for a graph: the committed head and document
  replay  POST that committed head back with its own sequence as expected.
          A writer answers 200 with the same document without submitting
          anything, so the drill proves the write path end to end while
          changing nothing.

A writer behind NAT is reached through a circuit-relay address
(/ip4/<relay>/.../p2p/<relay>/p2p-circuit/p2p/<writer>) or directly after
hole punching; the receipt records which. The last line is a JSON receipt.
Exit 0 when every writer answered both; 1 when one refused; 3 when the
drill could not run.
"""

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

PROTOCOL = "/x/yataverse/graph-head/1"


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class Client:
    def __init__(self, ipfs_bin):
        self.ipfs_bin = ipfs_bin
        self.repo = tempfile.mkdtemp(prefix="yataverse-p2p-writer-")
        self.daemon = None

    def ipfs(self, *args, timeout=120):
        return subprocess.run([self.ipfs_bin, *args], capture_output=True, text=True, timeout=timeout,
                              env={"IPFS_PATH": self.repo, "PATH": "/usr/bin:/bin"})

    def start(self):
        self.ipfs("init", "--profile=test")
        for k, v in [("Bootstrap", "[]"), ("AutoConf.Enabled", "false"), ("Routing.Type", '"none"'),
                     ("Addresses.API", '"/ip4/127.0.0.1/tcp/0"'), ("Addresses.Gateway", '"/ip4/127.0.0.1/tcp/0"'),
                     ("Addresses.Swarm", '["/ip4/0.0.0.0/tcp/0","/ip4/0.0.0.0/udp/0/quic-v1"]'),
                     ("Experimental.Libp2pStreamMounting", "true"),
                     ("Swarm.RelayClient.Enabled", "true"), ("Swarm.EnableHolePunching", "true")]:
            self.ipfs("config", "--json", k, v)
        self.daemon = subprocess.Popen([self.ipfs_bin, "daemon", "--enable-gc=false"],
                                       env={"IPFS_PATH": self.repo, "PATH": "/usr/bin:/bin"},
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        api = os.path.join(self.repo, "api")
        for _ in range(90):
            if self.daemon.poll() is not None:
                raise RuntimeError(f"daemon exited {self.daemon.returncode}")
            if os.path.exists(api) and self.ipfs("swarm", "peers").returncode == 0:
                return
            time.sleep(1)
        raise RuntimeError("daemon did not come online")

    def stop(self):
        if self.daemon:
            self.daemon.terminate()
            try:
                self.daemon.wait(20)
            except subprocess.TimeoutExpired:
                self.daemon.kill()
        shutil.rmtree(self.repo, ignore_errors=True)


def http(port, method, path, body=None, timeout=300):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def path_of(c, peer):
    conns = [l for l in c.ipfs("swarm", "peers").stdout.splitlines() if l.endswith(peer)]
    return ["relay" if "/p2p-circuit" in l else "direct" for l in conns]


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--ipfs-bin", default="ipfs")
    p.add_argument("--writer", action="append", required=True,
                   help="NAME=raw-IP multiaddr[,multiaddr...] ending in /p2p/<writer peer id>; for a writer "
                        "behind NAT give its /p2p-circuit/ addresses on several relays")
    p.add_argument("--graph", required=True)
    p.add_argument("--read-tries", type=int, default=5)
    p.add_argument("--upgrade-wait", type=int, default=45, help="seconds to wait for a relayed connection to go direct")
    a = p.parse_args(argv)
    writers = [w.split("=", 1) for w in a.writer]
    if any("/dns" in addr for _, addr in writers):
        print("REFUSE a /dns multiaddr would make this drill depend on DNS")
        return 1
    c = Client(a.ipfs_bin)
    results, ok = [], True
    try:
        c.start()
        for name, addr in writers:
            addrs = addr.split(",")
            peer = addrs[0].rsplit("/p2p/", 1)[1]
            if any(x.rsplit("/p2p/", 1)[1] != peer for x in addrs):
                print(f"REFUSE {name}: addresses name different peers")
                return 1
            r = {"writer": name, "peer": peer, "addrs": addrs}
            results.append(r)
            cons = [c.ipfs("swarm", "connect", x, timeout=90) for x in addrs]
            con = next((x for x in cons if x.returncode == 0), cons[-1])
            r["connect"] = [x.returncode == 0 for x in cons]
            r["connect"], r["connected_via"] = any(r["connect"]), r["connect"]
            if r["connect"] and all("/p2p-circuit" in x for x in addrs):
                # A relayed connection is limited: Kubo opens no application
                # stream over it. DCUtR upgrades it to a direct connection
                # (a reversal when one side is public, else a hole punch).
                t0 = time.time()
                while time.time() - t0 < a.upgrade_wait:
                    lines = [l for l in c.ipfs("swarm", "peers").stdout.splitlines() if l.endswith(peer)]
                    if any("/p2p-circuit" not in l for l in lines):
                        break
                    time.sleep(1)
                r["upgrade_s"] = round(time.time() - t0, 1)
            port = free_port()
            fwd = c.ipfs("p2p", "forward", PROTOCOL, f"/ip4/127.0.0.1/tcp/{port}", "/p2p/" + peer)
            if not (r["connect"] and fwd.returncode == 0):
                r["error"] = (con.stderr + fwd.stderr).strip()[-200:]
                ok = False
                print(f"FAIL {name}: {r['error']}", flush=True)
                continue
            t0 = time.time()
            # A stream opened while the connection is still relayed (limited)
            # is reset; retry while DCUtR finishes the direct connection.
            for attempt in range(a.read_tries):
                try:
                    status, cur = http(port, "GET", f"/v1/graph-head?graph={a.graph}")
                    break
                except OSError as e:
                    status, cur = 0, {"error": str(e)}
                    time.sleep(3)
            r["read_attempts"] = attempt + 1
            r["read"] = {"status": status, "seq": cur.get("seq"), "cid": cur.get("cid"), "s": round(time.time() - t0, 1)}
            if status != 200:
                ok = False
                print(f"FAIL {name} read {status} {cur.get('error', '')}", flush=True)
                c.ipfs("p2p", "close", "--all")
                continue
            h = cur["document"]["head"]
            t0 = time.time()
            try:
                status, out = http(port, "POST", "/v1/graph-head",
                                   {"graph": a.graph, "expected": {"sequence": h["sequence"]}, "head": h})
            except OSError as e:
                status, out = 0, {"error": str(e)}
            committed = out.get("committed") or {}
            r["replay"] = {"status": status, "cid": committed.get("cid"), "s": round(time.time() - t0, 1)}
            same = status == 200 and committed.get("cid") == cur["cid"]
            ok = ok and same
            r["path"] = path_of(c, peer)
            print(("PASS " if same else "FAIL ") + f"{name}: read seq {r['read']['seq']} in {r['read']['s']}s, "
                  f"replay {status} in {r['replay']['s']}s, path {r['path']}", flush=True)
            c.ipfs("p2p", "close", "--all")
        print(json.dumps({"drill": "yataverse-p2p-writer", "passed": ok, "protocol": PROTOCOL, "graph": a.graph,
                          "dns": "none", "tailscale": "none", "cloudflare": "none", "writers": results,
                          "checked": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}))
        return 0 if ok else 1
    except (RuntimeError, subprocess.TimeoutExpired, OSError) as e:
        print("UNMEASURED", e)
        return 3
    finally:
        c.stop()


if __name__ == "__main__":
    sys.exit(main())
