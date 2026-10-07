#!/usr/bin/env python3
"""Mirror yataverse graph heads into inga refs (ADR-2610062000 P4, shadow).

Each yataverse graph head lives in R2 at heads/yataverse/ipns/<graph>.json as
a record signed by the namespace head signer:
{name, value, sequence, valid_until, public_key_multibase, signature_multibase}.
The signature covers the canonical dag-cbor of every field except the two
signature fields (kotobase-client kotobase.ipns/sign-head).

The head record has no `prev`, so inga cannot be backfilled from seq 0 with
the R2 sequence numbers (ADR-2608048000). The mirror keeps its own sequence
instead. Inga ref yataverse/graph/<graph> seq N names the CID of a mirror
document:

    {"schema": "yataverse-graph-head-mirror/v1", "graph": ..., "seq": N,
     "prev": <CID of the seq N-1 document or null>, "head": <signed record>}

The document carries the signed head, so anyone can check offline that the
head signer really produced that (value, sequence). `prev` chains the
documents, so the mirror's history needs nothing from R2.

For each graph, `mirror`:
1. Reads the head from --source-cmd and from --check-cmd. Both must return the
   identical record, e.g. the B2 copy and a read-only R2 get.
2. Verifies the signature, and refuses a signer that is not pinned with --signer.
3. Asks the witnesses for the ref's current head through --verifier
   (lake_head.cljk verify --ref=...).
4. Leaves the ref alone when the head is unchanged. Refuses a lower R2 sequence.
5. Otherwise writes the next document and pins it with every --pin-cmd; the
   CIDs must all agree. Then it submits seq N through the verifier, which
   resubmits until 5 of 7 witnesses prove it.

Exit codes: 0 every graph is mirrored or unchanged; 1 a graph was refused;
3 a graph could not be measured.
"""

import argparse
import base64
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

SCHEMA = "yataverse-graph-head-mirror/v1"
GRAPH_RE = re.compile(r"baf[a-z2-7]{50,}")
B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


class Refused(Exception):
    pass


class Unmeasured(Exception):
    pass


def b58decode(text):
    n = 0
    for ch in text:
        n = n * 58 + B58.index(ch)
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return b"\0" * (len(text) - len(text.lstrip("1"))) + raw


def _cbor_head(major, n):
    if n < 24:
        return bytes([major << 5 | n])
    for info, width in ((24, 1), (25, 2), (26, 4), (27, 8)):
        if n < 1 << (8 * width):
            return bytes([major << 5 | info]) + n.to_bytes(width, "big")
    raise ValueError("integer too large")


def dag_cbor(value):
    """Canonical dag-cbor for the JSON values a head record holds."""
    if value is None:
        return b"\xf6"
    if isinstance(value, bool):
        return b"\xf5" if value else b"\xf4"
    if isinstance(value, int):
        return _cbor_head(0, value) if value >= 0 else _cbor_head(1, -1 - value)
    if isinstance(value, str):
        raw = value.encode()
        return _cbor_head(3, len(raw)) + raw
    if isinstance(value, dict):
        keys = sorted(value, key=lambda k: (len(k.encode()), k.encode()))
        return _cbor_head(5, len(keys)) + b"".join(dag_cbor(k) + dag_cbor(value[k]) for k in keys)
    if isinstance(value, list):
        return _cbor_head(4, len(value)) + b"".join(dag_cbor(v) for v in value)
    raise Refused(f"head record holds an unsupported value: {type(value).__name__}")


def ed25519_from_did(did):
    if not (isinstance(did, str) and did.startswith("did:key:z")):
        raise Refused("public_key_multibase is not a did:key")
    raw = b58decode(did[len("did:key:z"):])
    if raw[:2] != b"\xed\x01" or len(raw) != 34:
        raise Refused("did:key is not Ed25519")
    return Ed25519PublicKey.from_public_bytes(raw[2:])


def verify_head(record, graph, signers):
    """Return the record when it is a well-formed head for `graph`, signed by a pinned key."""
    if not isinstance(record, dict):
        raise Refused("head is not a JSON object")
    if record.get("name") != graph:
        raise Refused(f"head names {record.get('name')!r}, not {graph}")
    if not (isinstance(record.get("value"), str) and GRAPH_RE.fullmatch(record["value"])):
        raise Refused("head value is not a CID")
    if not (isinstance(record.get("sequence"), int) and record["sequence"] >= 0):
        raise Refused("head sequence is not a natural number")
    signer = record.get("public_key_multibase")
    if signer not in signers:
        raise Refused(f"head signer {signer} is not pinned")
    sig = record.get("signature_multibase")
    if not (isinstance(sig, str) and sig.startswith("z")):
        raise Refused("signature_multibase is not base58btc")
    payload = {k: v for k, v in record.items() if k not in ("public_key_multibase", "signature_multibase")}
    try:
        ed25519_from_did(signer).verify(b58decode(sig[1:]), dag_cbor(payload))
    except (InvalidSignature, ValueError):
        raise Refused("head signature does not verify")
    return record


def canonical(doc):
    return json.dumps(doc, sort_keys=True, separators=(",", ":")).encode()


def raw_cid(data):
    """CIDv1, raw codec, sha2-256, base32: what `ipfs add --cid-version=1 --raw-leaves` gives a small file."""
    digest = bytes([0x01, 0x55, 0x12, 0x20]) + hashlib.sha256(data).digest()
    return "b" + base64.b32encode(digest).decode().lower().rstrip("=")


def mirror_doc(graph, seq, prev, head):
    return canonical({"schema": SCHEMA, "graph": graph, "seq": seq, "prev": prev, "head": head})


def next_step(graph, head, current, prev_doc):
    """Pure. current is None (ref absent) or (seq, cid). prev_doc is the parsed
    document at `current`. -> ("unchanged", None) or ("submit", (seq, prev))."""
    if current is None:
        return "submit", (0, None)
    seq, cid = current
    if prev_doc is None:
        raise Unmeasured(f"document {cid} for seq {seq} is not readable")
    if prev_doc.get("schema") != SCHEMA or prev_doc.get("graph") != graph or prev_doc.get("seq") != seq:
        raise Refused(f"document {cid} is not the seq {seq} mirror of {graph}")
    old = prev_doc["head"]
    if (old["sequence"], old["value"]) == (head["sequence"], head["value"]):
        return "unchanged", None
    if head["sequence"] <= old["sequence"]:
        raise Refused(f"R2 sequence {head['sequence']} does not advance past mirrored {old['sequence']}")
    return "submit", (seq + 1, cid)


def run(cmd, data=None, timeout=300):
    r = subprocess.run(cmd, input=data, capture_output=True, timeout=timeout)
    return r.returncode, r.stdout, r.stderr


def fetch(template, graph):
    code, out, err = run(shlex.split(template.format(graph=graph)))
    if code != 0:
        raise Unmeasured(f"{template.split()[0]} failed: {err.decode(errors='replace').strip()[-200:]}")
    try:
        return json.loads(out)
    except ValueError:
        raise Unmeasured(f"{template.split()[0]} did not return JSON")


def witness_head(verifier, ledger, ref):
    code, out, _ = run(shlex.split(verifier) + ["verify", ledger, "--ref=" + ref], timeout=180)
    line = out.decode(errors="replace").strip().splitlines()[-1:] or [""]
    m = re.match(r"VERIFIED \S+ seq (\d+) cid (\S+)", line[0])
    if code == 0 and m:
        return int(m.group(1)), m.group(2)
    if code == 4:
        return None
    if code == 1:
        raise Refused("witnesses disagree: " + line[0][:200])
    raise Unmeasured("witnesses did not prove a head: " + line[0][:200])


def read_doc(args, cid):
    path = os.path.join(args.out_dir, cid + ".json")
    if os.path.exists(path):
        data = open(path, "rb").read()
    elif args.cat_cmd:
        code, data, _ = run(shlex.split(args.cat_cmd.format(cid=cid)))
        if code != 0:
            return None
    else:
        return None
    if raw_cid(data) != cid:
        raise Refused(f"document bytes do not hash to {cid}")
    return json.loads(data)


def mirror_one(args, graph):
    ref = "yataverse/graph/" + graph
    head = verify_head(fetch(args.source_cmd, graph), graph, set(args.signer))
    checked = fetch(args.check_cmd, graph)
    if checked != head:
        raise Refused(f"source and check disagree (sequence {head['sequence']} vs {checked.get('sequence')})")
    current = witness_head(args.verifier, args.ledger, ref)
    prev_doc = read_doc(args, current[1]) if current else None
    action, step = next_step(graph, head, current, prev_doc)
    if action == "unchanged":
        print(f"UNCHANGED {ref} seq {current[0]} r2-sequence {head['sequence']}", flush=True)
        return
    if args.writer_url:
        return via_writer(args, ref, graph, head, prev_doc)
    seq, prev = step
    data = mirror_doc(graph, seq, prev, head)
    cid = raw_cid(data)
    with open(os.path.join(args.out_dir, cid + ".json"), "wb") as f:
        f.write(data)
    for pin in args.pin_cmd:
        code, out, err = run(shlex.split(pin), data=data)
        got = out.decode().strip()
        if code != 0 or got != cid:
            raise Unmeasured(f"pin via {pin.split()[0]} returned {got or err.decode(errors='replace')[-120:]!r}, want {cid}")
    if args.dry_run:
        print(f"PLAN {ref} seq {seq} cid {cid} r2-sequence {head['sequence']}", flush=True)
        return
    code, out, _ = run(shlex.split(args.verifier) + ["submit", args.ledger, str(seq), cid, "--ref=" + ref], timeout=600)
    last = (out.decode(errors="replace").strip().splitlines() or [""])[-1]
    if code == 0 and args.writer_check:
        # The same (seq, cid) can be committed by another submitter: the
        # document is deterministic, so an unsigned run would produce it too.
        # Verified 2026-10-07: an unsigned scheduled run won seq 2 while a
        # signed manual run reported MIRRORED for it. Re-verify under a
        # policy naming this mirror's own key.
        wcode, wout, _ = run(shlex.split(args.verifier) + ["verify", args.ledger, "--ref=" + ref,
                                                          "--writer-policy=" + args.writer_check], timeout=180)
        if wcode != 0:
            wlast = (wout.decode(errors="replace").strip().splitlines() or [""])[-1]
            raise Refused(f"seq {seq} cid {cid} is committed, but not as this mirror's signed record: {wlast[:160]}")
        print(f"MIRRORED-SIGNED {ref} seq {seq} cid {cid} r2-sequence {head['sequence']}", flush=True)
    elif code == 0:
        print(f"MIRRORED {ref} seq {seq} cid {cid} r2-sequence {head['sequence']}", flush=True)
    elif code == 1:
        raise Refused(last[:200])
    else:
        raise Unmeasured(last[:200])


def via_writer(args, ref, graph, head, prev_doc):
    """Send the R2 head to yataverse-writer (ADR-2610062000 P5) instead of
    submitting here, so every inga graph write goes through one proposer.
    `expected` is the head inga holds now, which makes it a compare-and-set."""
    import urllib.error
    import urllib.request
    old = prev_doc["head"] if prev_doc else None
    body = json.dumps({"graph": graph, "head": head,
                       "expected": {"sequence": old["sequence"], "value": old["value"]} if old else None}).encode()
    req = urllib.request.Request(args.writer_url.rstrip("/") + "/v1/graph-head", data=body,
                                 headers={"content-type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=900) as r:
            c = json.load(r)["committed"]
        print(f"MIRRORED-VIA-WRITER {ref} seq {c['seq']} cid {c['cid']} r2-sequence {head['sequence']}", flush=True)
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:200]
        if e.code in (400, 403, 409):
            raise Refused(f"writer {e.code}: {detail}")
        raise Unmeasured(f"writer {e.code}: {detail}")
    except (urllib.error.URLError, OSError) as e:
        raise Unmeasured(f"writer unreachable: {e}")


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--graph", action="append", required=True)
    p.add_argument("--source-cmd", required=True, help="command template printing the head JSON; {graph}")
    p.add_argument("--check-cmd", required=True, help="independent read of the same head; {graph}")
    p.add_argument("--signer", action="append", required=True, help="pinned head-signer did:key")
    p.add_argument("--verifier", required=True, help="command prefix running lake_head.cljk")
    p.add_argument("--ledger", required=True)
    p.add_argument("--pin-cmd", action="append", default=[], help="reads the document on stdin, prints its CID")
    p.add_argument("--cat-cmd", help="prints a mirror document by CID; {cid}")
    p.add_argument("--out-dir", required=True)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--writer-url", help="send heads to yataverse-writer instead of submitting here (P5)")
    p.add_argument("--writer-check", help="after a submit, require the head to pass this writer policy (EDN)")
    args = p.parse_args(argv)
    if len(args.pin_cmd) < 2 and not args.dry_run and not args.writer_url:
        p.error("pin each document on at least two custodians (--pin-cmd twice)")
    for g in args.graph:
        if not GRAPH_RE.fullmatch(g):
            p.error(f"not a graph CID: {g}")
    os.makedirs(args.out_dir, exist_ok=True)
    worst = 0
    for g in args.graph:
        try:
            mirror_one(args, g)
        except Refused as e:
            print(f"REFUSE yataverse/graph/{g}: {e}", flush=True)
            worst = max(worst, 1) if worst != 3 else 3
        except (Unmeasured, subprocess.TimeoutExpired) as e:
            print(f"UNMEASURED yataverse/graph/{g}: {e}", flush=True)
            worst = 3
    return worst


if __name__ == "__main__":
    sys.exit(main())
