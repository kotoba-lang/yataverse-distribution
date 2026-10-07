#!/usr/bin/env python3
"""Hold what inga committed: a custodian pulls graph-head documents itself.

A writer puts each document on its own Kubo before it submits. Every other
custodian then fetches it, the way Holochain's validation authorities hold
the entries of their neighbourhood: nobody pushes to them. This custodian
reads each graph's committed head from the witnesses (5 of 7), walks the
document chain back through `prev` until it reaches a document it already
holds, and for each one:

  fetch     ipfs pin add (bitswap from whichever peer has it)
  validate  the document's CID, schema, graph and seq, and the namespace
            signature on the head it carries (graph_head_mirror.verify_head)

A document that fails validation is unpinned and reported as a WARRANT line
(the evidence: ref, seq, cid, reason). It is still the committed record;
the warrant is what a reader needs to refuse it.

With this, a writer needs no access to any other custodian: two writers on
two hosts each hold their own documents, and the custodians converge.

    custody_puller.py --graph G [--graph G2 ...] --signer did:key:... \\
        --verifier "<node ... lake_head.cljk>" --ledger LEDGER \\
        --ipfs "<ipfs command prefix>" --state STATE.json [--loop SECONDS]

Exit codes: 0 every head held and valid; 1 a warrant was raised;
3 a head or document could not be read.
"""

import argparse
import json
import os
import shlex
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import graph_head_mirror as gm  # noqa: E402


def check_doc(graph, seq, cid, data, signers):
    """Pure. -> the parsed document, or raise gm.Refused."""
    if gm.raw_cid(data) != cid:
        raise gm.Refused(f"bytes do not hash to {cid}")
    doc = json.loads(data)
    if doc.get("schema") != gm.SCHEMA or doc.get("graph") != graph or doc.get("seq") != seq:
        raise gm.Refused(f"not the seq {seq} document of {graph}")
    if (seq == 0) != (doc.get("prev") is None):
        raise gm.Refused(f"seq {seq} with prev {doc.get('prev')}")
    gm.verify_head(doc["head"], graph, signers)
    return doc


class Puller:
    def __init__(self, a):
        self.a = a
        self.ipfs = shlex.split(a.ipfs)
        self.held = set(json.load(open(a.state))) if os.path.exists(a.state) else set()

    def save(self):
        tmp = self.a.state + ".tmp"
        with open(tmp, "w") as f:
            json.dump(sorted(self.held), f)
        os.replace(tmp, self.a.state)

    def fetch(self, cid):
        code, _, err = gm.run(self.ipfs + ["pin", "add", "--progress=false", cid], timeout=self.a.fetch_timeout)
        if code != 0:
            raise gm.Unmeasured(f"pin add {cid}: {err.decode(errors='replace').strip()[-160:]}")
        code, out, err = gm.run(self.ipfs + ["cat", "--offline", cid], timeout=120)
        if code != 0:
            raise gm.Unmeasured(f"cat {cid}: {err.decode(errors='replace').strip()[-160:]}")
        return out

    def pull(self, graph):
        """-> (head seq, documents newly held). Raises on a warrant."""
        head = gm.witness_head(self.a.verifier, self.a.ledger, "yataverse/graph/" + graph)
        if head is None:
            return None, 0
        seq, cid = head
        new = 0
        while cid is not None and cid not in self.held:
            data = self.fetch(cid)
            try:
                doc = check_doc(graph, seq, cid, data, set(self.a.signer))
            except (gm.Refused, ValueError, KeyError) as e:
                gm.run(self.ipfs + ["pin", "rm", cid], timeout=120)
                print(json.dumps({"warrant": {"ref": "yataverse/graph/" + graph, "seq": seq, "cid": cid,
                                              "reason": str(e)[:200]}}), flush=True)
                raise
            self.held.add(cid)
            self.save()
            new += 1
            cid, seq = doc["prev"], seq - 1
        return head[0], new

    def once(self):
        worst = 0
        for graph in self.a.graph:
            try:
                seq, new = self.pull(graph)
                print(f"HELD {graph[:14]} head seq {seq} (+{new})", flush=True)
            except (gm.Refused, ValueError, KeyError):
                worst = max(worst, 1)
            except (gm.Unmeasured, OSError) as e:
                print(f"UNMEASURED {graph[:14]} {e}", flush=True)
                worst = max(worst, 3) if worst != 1 else 1
        return worst


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--graph", action="append", required=True)
    p.add_argument("--signer", action="append", required=True, help="pinned namespace head-signer did:key")
    p.add_argument("--verifier", required=True, help="command prefix running lake_head.cljk")
    p.add_argument("--ledger", required=True)
    p.add_argument("--ipfs", required=True, help="ipfs command prefix for this custodian's Kubo")
    p.add_argument("--state", required=True, help="JSON list of document CIDs already held and validated")
    p.add_argument("--fetch-timeout", type=int, default=300)
    p.add_argument("--loop", type=int, help="repeat every N seconds instead of exiting")
    a = p.parse_args(argv)
    puller = Puller(a)
    if not a.loop:
        return puller.once()
    while True:
        puller.once()
        time.sleep(a.loop)


if __name__ == "__main__":
    sys.exit(main())
