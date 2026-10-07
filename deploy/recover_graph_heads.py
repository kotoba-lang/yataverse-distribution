#!/usr/bin/env python3
"""Rebuild yataverse's R2 graph heads from inga alone (ADR-2610062000 D5, P5 drill).

After the P5 cutover, R2's heads/yataverse/ipns/<graph>.json is a copy. The
copy is only a projection if it can be thrown away and rebuilt without R2,
without Cloudflare, and without the host that runs yataverse-writer. This
does that from any fleet node:

  1. Ask every witness for the committed head of yataverse/graph/<graph> and
     require --min of them to agree on (seq, cid).
  2. Read the mirror document by CID from this node's own Kubo, OFFLINE, and
     check its bytes hash to that CID.
  3. Check that the document is the seq-th mirror of the graph, and that the
     head inside it is signed by the pinned namespace signer.
  4. Write the head to --out-dir/heads/yataverse/ipns/<graph>.json, which is
     the shape and key R2 uses.

Exit codes: 0 every graph rebuilt; 1 a graph refused (disagreement, a bad
document or signature); 3 a graph could not be measured.
"""

import argparse
import collections
import json
import os
import subprocess
import sys
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import graph_head_mirror as gm  # noqa: E402


def committed(witness, ref):
    url = f"{witness.rstrip('/')}/committed?ref={urllib.parse.quote(ref, safe='')}"
    try:
        with urllib.request.urlopen(url, timeout=8) as r:
            h = json.load(r).get("head")
        return (h["seq"], h["cid"]) if h else None
    except Exception:
        return None


def agreed(answers, minimum):
    """Pure. The (seq, cid) at least `minimum` answers name, or None."""
    counts = collections.Counter(a for a in answers if a)
    if not counts:
        return None
    top, n = counts.most_common(1)[0]
    return top if n >= minimum else None


def rebuild(args, graph):
    ref = "yataverse/graph/" + graph
    answers = [committed(w, ref) for w in args.witness]
    got = agreed(answers, args.min)
    if got is None:
        raise gm.Unmeasured(f"fewer than {args.min} witnesses agree: {answers}")
    seq, cid = got
    env = dict(os.environ, IPFS_PATH=args.ipfs_path)
    r = subprocess.run([args.ipfs_bin, "--offline", "cat", cid], env=env, capture_output=True, timeout=60)
    if r.returncode != 0:
        raise gm.Unmeasured(f"document {cid} is not held by this node")
    data = r.stdout
    if gm.raw_cid(data) != cid:
        raise gm.Refused(f"bytes do not hash to {cid}")
    doc = json.loads(data)
    if doc.get("schema") != gm.SCHEMA or doc.get("graph") != graph or doc.get("seq") != seq:
        raise gm.Refused(f"document {cid} is not the seq {seq} mirror of {graph}")
    head = gm.verify_head(doc["head"], graph, set(args.signer))
    path = os.path.join(args.out_dir, "heads", "yataverse", "ipns", graph + ".json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(head, f, separators=(",", ":"))
    return seq, head["sequence"], head["value"]


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--graph", action="append", required=True)
    p.add_argument("--witness", action="append", required=True, help="http://ip:port, one per witness")
    p.add_argument("--min", type=int, default=5)
    p.add_argument("--ipfs-bin", required=True)
    p.add_argument("--ipfs-path", required=True)
    p.add_argument("--signer", action="append", required=True)
    p.add_argument("--out-dir", required=True)
    a = p.parse_args(argv)
    worst = 0
    for g in a.graph:
        try:
            seq, sequence, value = rebuild(a, g)
            print(f"REBUILT {g} inga-seq {seq} r2-sequence {sequence} value {value}", flush=True)
        except gm.Refused as e:
            print(f"REFUSE {g}: {e}", flush=True)
            worst = 1 if worst != 3 else 3
        except Exception as e:
            print(f"UNMEASURED {g}: {e}", flush=True)
            worst = 3
    return worst


if __name__ == "__main__":
    sys.exit(main())
