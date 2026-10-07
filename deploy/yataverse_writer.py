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
"""

import argparse
import json
import os
import shlex
import sys
import threading
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
            cur = self.current(graph)
            action, step = decide(graph, expected, head, cur[2] if cur else None)
            if action == "commit":
                return {"seq": cur[0], "cid": cur[1], "sequence": head["sequence"], "value": head["value"]}
            seq, prev = step[0], (cur[1] if cur else None)
            data = gm.mirror_doc(graph, seq, prev, head)
            cid = gm.raw_cid(data)
            with open(os.path.join(self.a.out_dir, cid + ".json"), "wb") as f:
                f.write(data)
            for pin in self.a.pin_cmd:
                code, out, err = gm.run(shlex.split(pin), data=data)
                if code != 0 or out.decode().strip() != cid:
                    raise gm.Unmeasured(f"pin via {pin.split()[0]} did not return {cid}")
            ref = "yataverse/graph/" + graph
            code, out, _ = gm.run(shlex.split(self.a.verifier) + ["submit", self.a.ledger, str(seq), cid, "--ref=" + ref],
                                  timeout=600)
            last = (out.decode(errors="replace").strip().splitlines() or [""])[-1]
            if code == 1:
                # Another writer took the sequence. Report what is there now.
                now = self.current(graph)
                raise Conflict({"sequence": now[2]["head"]["sequence"], "value": now[2]["head"]["value"]} if now else None)
            if code != 0:
                raise gm.Unmeasured(last[:200])
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
                return self.reply(409, {"conflict": c.current})
            except gm.Refused as e:
                return self.reply(403, {"refused": str(e)[:200]})
            except Exception as e:
                return self.reply(503, {"unmeasured": str(e)[:200]})
    return H


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=18130)
    p.add_argument("--signer", action="append", required=True, help="pinned namespace head-signer did:key")
    p.add_argument("--verifier", required=True, help="command prefix running lake_head.cljk (with --writer-seed)")
    p.add_argument("--ledger", required=True)
    p.add_argument("--pin-cmd", action="append", default=[])
    p.add_argument("--cat-cmd", required=True)
    p.add_argument("--out-dir", required=True)
    a = p.parse_args(argv)
    if len(a.pin_cmd) < 2:
        p.error("pin each document on at least two custodians (--pin-cmd twice)")
    srv = ThreadingHTTPServer((a.host, a.port), handler(Writer(a)))
    print(f"yataverse-writer on {a.host}:{a.port}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
