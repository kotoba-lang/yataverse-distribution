#!/usr/bin/env python3
"""yataverse-writer: the fleet proposer for graph heads (ADR-2610062000 D3, P5).

The Worker stops being the arbiter of a graph head. A write goes to this
process first. It checks the head is signed by the pinned namespace signer and
that it advances exactly the head inga holds now (a compare-and-set, the same
rule `write-head!` applied on R2). Then it submits the next mirror document to
the inga ref yataverse/graph/<graph>, signed with this writer's key, and
answers only once 5 of 7 witnesses prove it committed. R2 becomes a copy that
a follower writes afterwards.

    POST /v1/graph-head  {"graph": cid, "expected": {"sequence": n, "value": cid} | null,
                          "head": <signed head record>}
      200 {"committed": {"seq", "cid", "sequence", "value"}}   committed, or already the head
      409 {"conflict": {"sequence", "value"} | null}           expected is not the current head
      400 / 403 {"refused": reason}                            malformed, or not the signer
      503 {"unmeasured": reason}                               witnesses or custodians unreachable
    GET  /v1/graph-head?graph=cid
      200 {"seq", "cid", "document"} | 404 when inga holds no record for the graph

The document format and every check come from graph_head_mirror.py, so the
writer and the P4 mirror produce identical chains: seq N names
{schema, graph, seq, prev, head} and `prev` is the seq N-1 document's CID.

Writes to one graph are serialised in this process. Across processes, or a
second writer, inga's first-wins sequence is the arbiter: a submit that loses
its sequence is reported as a conflict, never as a success.

Writers are relays (2026-10-07). The authority is the namespace signature
inside the document; a writer's own key only admits it to the witnesses'
policy. Two writers relaying the same head build byte-identical documents,
so either may answer. A writer need not reach every custodian: it holds the
document on its own Kubo (--pin-cmd), and custodians that pull committed
documents themselves (custody_puller.py) are named with --pulled-by.
"""

import argparse
import json
import os
import shlex
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import graph_head_mirror as gm  # noqa: E402

MAX_BODY = 16 * 1024


class Conflict(Exception):
    def __init__(self, current):
        super().__init__("expected head is not the current head")
        self.current = current


def decide(graph, expected, head, current_doc):
    """Pure. current_doc is the parsed document inga holds for the graph, or
    None when it holds none. -> ("commit", None) when `head` is already the
    current head, ("submit", (seq, prev_cid_or_None)) for the next document,
    or raises Conflict / gm.Refused."""
    cur = current_doc["head"] if current_doc else None
    cur_key = {"sequence": cur["sequence"], "value": cur["value"]} if cur else None
    if cur and (cur["sequence"], cur["value"]) == (head["sequence"], head["value"]):
        return "commit", None
    # `expected` may name only the sequence: the Worker knows the sequence it
    # read, not always the value. One sequence names one committed head, so
    # the sequence alone is a sufficient compare-and-set; a value, when given,
    # must match too.
    if not (expected is None and cur is None) and not (
            expected is not None and cur is not None
            and expected["sequence"] == cur["sequence"]
            and expected.get("value", cur["value"]) == cur["value"]):
        raise Conflict(cur_key)
    # The compare-and-set is `expected`; it is what prevents a lost update.
    # The sequence only has to move forward: the P4 mirror, which now writes
    # through here, can see several R2 writes collapse into one step.
    floor = 0 if cur is None else cur["sequence"] + 1
    if head["sequence"] < floor:
        raise gm.Refused(f"head sequence {head['sequence']} does not advance past {floor - 1}")
    if current_doc is None:
        return "submit", (0, None)
    return "submit", (current_doc["seq"] + 1, None)


class Writer:
    def __init__(self, args):
        self.a = args
        self.locks = {}
        self.guard = threading.Lock()
        # The head this writer last committed or read, per graph. A write
        # starts from it instead of asking the witnesses again (a cold node
        # start plus a quorum check). A stale entry is safe: inga refuses a
        # submit whose sequence is already held, and that refusal re-reads.
        self.cache = {}
        os.makedirs(args.out_dir, exist_ok=True)

    def lock(self, graph):
        with self.guard:
            return self.locks.setdefault(graph, threading.Lock())

    def current(self, graph):
        """(seq, cid, document) inga holds, or None."""
        ref = "yataverse/graph/" + graph
        cur = gm.witness_head(self.a.verifier, self.a.ledger, ref)
        if cur is None:
            return None
        doc = gm.read_doc(self.a, cur[1])
        if doc is None:
            raise gm.Unmeasured(f"document {cur[1]} for seq {cur[0]} is not readable")
        if doc.get("schema") != gm.SCHEMA or doc.get("graph") != graph or doc.get("seq") != cur[0]:
            raise gm.Refused(f"document {cur[1]} is not the seq {cur[0]} mirror of {graph}")
        return cur[0], cur[1], doc

    def write(self, graph, expected, head):
        gm.verify_head(head, graph, set(self.a.signer))
        with self.lock(graph):
            cur = self.cache.get(graph) or self.current(graph)
            try:
                return self._write(graph, expected, head, cur)
            except Conflict:
                # The cache may have been the stale party. Decide again on a
                # fresh read before answering 409.
                fresh = self.current(graph)
                if fresh != cur:
                    self.cache.pop(graph, None)
                    return self._write(graph, expected, head, fresh)
                raise

    def _late_pin(self, pin, data, cid):
        for attempt in range(6):
            code, out, _ = gm.run(shlex.split(pin), data=data, timeout=600)
            if code == 0 and out.decode().strip() == cid:
                sys.stderr.write(f"LATE-PIN {cid} via {pin.split()[0]} ok (attempt {attempt + 1})\n")
                return
            time.sleep(30 * (attempt + 1))
        sys.stderr.write(f"LATE-PIN {cid} via {pin.split()[0]} FAILED\n")

    def _write(self, graph, expected, head, cur):
        t0 = time.time()
        action, step = decide(graph, expected, head, cur[2] if cur else None)
        if action == "commit":
            self.cache[graph] = cur
            return {"seq": cur[0], "cid": cur[1], "sequence": head["sequence"], "value": head["value"]}
        seq, prev = step[0], (cur[1] if cur else None)
        data = gm.mirror_doc(graph, seq, prev, head)
        cid = gm.raw_cid(data)
        with open(os.path.join(self.a.out_dir, cid + ".json"), "wb") as f:
            f.write(data)
        # Both custodians at once: the pins are independent, and they were
        # two sequential ssh round trips on every write.
        #
        # The first --sync-pins custodians must hold the document before it is
        # submitted. The rest are pinned after the commit, in the background,
        # with retries: jacob's Kubo sits on a busy HDD and took 16.8 s for a
        # 500-byte add (2026-10-07), which was most of a write's latency. The
        # document also stays in --out-dir here, so it is never held by one
        # node alone while the background pin catches up.
        sync, rest = self.a.pin_cmd[:self.a.sync_pins], self.a.pin_cmd[self.a.sync_pins:]
        with ThreadPoolExecutor(len(sync)) as pool:
            results = list(pool.map(lambda pin: gm.run(shlex.split(pin), data=data), sync))
        t_pin = time.time()
        for pin, (code, out, _err) in zip(sync, results):
            if code != 0 or out.decode().strip() != cid:
                raise gm.Unmeasured(f"pin via {pin.split()[0]} did not return {cid}")
        ref = "yataverse/graph/" + graph
        # --landed=1: answer once one witness of this fleet reports the commit
        # (see lake_head landed!); readers verify the 5-of-7 proofs themselves.
        code, out, err = gm.run(shlex.split(self.a.verifier) + ["submit", self.a.ledger, str(seq), cid,
                                                                "--ref=" + ref, "--landed=%d" % self.a.landed],
                                timeout=600)
        sys.stderr.write(err.decode(errors="replace"))
        last = (out.decode(errors="replace").strip().splitlines() or [""])[-1]
        if code == 1:
            # Another writer took the sequence. If it relayed this same head,
            # the head is committed: answer as committed, not as a conflict.
            now = self.current(graph)
            if now and (now[2]["head"]["sequence"], now[2]["head"]["value"]) == (head["sequence"], head["value"]):
                self.cache[graph] = now
                return {"seq": now[0], "cid": now[1], "sequence": head["sequence"], "value": head["value"]}
            raise Conflict({"sequence": now[2]["head"]["sequence"], "value": now[2]["head"]["value"]} if now else None)
        if code != 0:
            raise gm.Unmeasured(last[:200])
        self.cache[graph] = (seq, cid, json.loads(data))
        for pin in rest:
            threading.Thread(target=self._late_pin, args=(pin, data, cid), daemon=True).start()
        # Where a write's time goes, for the P5 latency budget.
        sys.stderr.write(f"TIMING {graph[:14]} seq {seq} pins {t_pin - t0:.1f}s "
                         f"submit {time.time() - t_pin:.1f}s total {time.time() - t0:.1f}s\n")
        return {"seq": seq, "cid": cid, "sequence": head["sequence"], "value": head["value"]}


def handler(writer):
    class H(BaseHTTPRequestHandler):
        def reply(self, status, body):
            data = json.dumps(body, sort_keys=True).encode()
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, format, *args):
            sys.stderr.write("%s %s\n" % (self.address_string(), format % args))

        def do_GET(self):
            u = urlparse(self.path)
            if u.path == "/health":
                return self.reply(200, {"ok": True, "service": "yataverse-writer"})
            if u.path != "/v1/graph-head":
                return self.reply(404, {"refused": "not found"})
            graph = (parse_qs(u.query).get("graph") or [""])[0]
            if not gm.GRAPH_RE.fullmatch(graph):
                return self.reply(400, {"refused": "graph must be a CID"})
            try:
                cur = writer.current(graph)
                # The follower reads every graph every ten minutes, so this
                # keeps the write path's starting point warm.
                if cur is not None:
                    writer.cache[graph] = cur
            except gm.Refused as e:
                return self.reply(409, {"refused": str(e)})
            except (gm.Unmeasured, Exception) as e:
                return self.reply(503, {"unmeasured": str(e)[:200]})
            if cur is None:
                return self.reply(404, {"absent": graph})
            return self.reply(200, {"seq": cur[0], "cid": cur[1], "document": cur[2]})

        def do_POST(self):
            if urlparse(self.path).path != "/v1/graph-head":
                return self.reply(404, {"refused": "not found"})
            n = int(self.headers.get("content-length") or 0)
            if n <= 0 or n > MAX_BODY:
                return self.reply(400, {"refused": "body must be 1..%d bytes" % MAX_BODY})
            try:
                req = json.loads(self.rfile.read(n))
                graph, expected, head = req["graph"], req.get("expected"), req["head"]
                if not (isinstance(graph, str) and gm.GRAPH_RE.fullmatch(graph)):
                    raise ValueError("graph must be a CID")
                if expected is not None and not (isinstance(expected, dict) and "sequence" in expected
                                                 and set(expected) <= {"sequence", "value"}
                                                 and isinstance(expected["sequence"], int)):
                    raise ValueError("expected must be null or {sequence[, value]}")
            except (ValueError, KeyError, TypeError) as e:
                return self.reply(400, {"refused": str(e)[:200]})
            try:
                return self.reply(200, {"committed": writer.write(graph, expected, head)})
            except Conflict as c:
                sys.stderr.write(f"CONFLICT {graph[:14]} expected {expected} head {head.get('sequence')} current {c.current}\n")
                return self.reply(409, {"conflict": c.current})
            except gm.Refused as e:
                sys.stderr.write(f"REFUSED {graph[:14]} expected {expected} head {head.get('sequence') if isinstance(head, dict) else '?'}: {e}\n")
                return self.reply(403, {"refused": str(e)[:200]})
            except Exception as e:
                return self.reply(503, {"unmeasured": str(e)[:200]})
    return H


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--host", action="append",
                   help="address to listen on; repeat for several (default 127.0.0.1). The libp2p mount "
                        "(p2p_mount.py) targets loopback, so that path never depends on the tailnet")
    p.add_argument("--port", type=int, default=18130)
    p.add_argument("--signer", action="append", required=True, help="pinned namespace head-signer did:key")
    p.add_argument("--verifier", required=True, help="command prefix running lake_head.cljk (with --writer-seed)")
    p.add_argument("--ledger", required=True)
    p.add_argument("--pin-cmd", action="append", default=[])
    p.add_argument("--cat-cmd", required=True)
    p.add_argument("--landed", type=int, default=1,
                   help="witnesses that must report the commit before the writer answers")
    p.add_argument("--sync-pins", type=int, default=2,
                   help="how many --pin-cmd custodians must hold a document before it is submitted")
    p.add_argument("--pulled-by", action="append", default=[],
                   help="a custodian that pulls committed documents itself (custody_puller.py); "
                        "counts toward the two custodians without this writer reaching it")
    p.add_argument("--out-dir", required=True)
    a = p.parse_args(argv)
    if len(a.pin_cmd) < 1 or len(a.pin_cmd) + len(a.pulled_by) < 2:
        p.error("each document needs two custodians: --pin-cmd twice, or --pin-cmd and --pulled-by")
    if not 1 <= a.sync_pins <= len(a.pin_cmd):
        p.error("--sync-pins must be between 1 and the number of --pin-cmd")
    hosts = a.host or ["127.0.0.1"]
    w = Writer(a)
    servers = [ThreadingHTTPServer((h, a.port), handler(w)) for h in hosts]
    for srv in servers[1:]:
        threading.Thread(target=srv.serve_forever, daemon=True).start()
    print(f"yataverse-writer on {', '.join(f'{h}:{a.port}' for h in hosts)}", flush=True)
    servers[0].serve_forever()


if __name__ == "__main__":
    main()
