#!/usr/bin/env python3
"""Build the next yataverse lake epoch from a verified capture.

Two pure steps of the epoch pipeline (README "Lake log on inga"):

  delta     rows of a capture diff that the log does not already hold. The
            diff (diff_lake_inventory.py) is relative to the epoch-0 base, so it
            also contains every later epoch's rows; subtracting the
            log-resolved membership (lake_head.cljk resolve) leaves only what
            is new. Refuses a row whose CID the log holds with another size.

  manifest  the canonical manifest JSON for epoch N: sorted keys, no
            whitespace, so the same inputs give the same bytes and the same
            CID on every custodian. Counts, bytes and sha256 are computed from
            the inventory file, not taken on trust, and --custody must name
            at least two nodes on at least two WANs (ADR-2610062000 D2: a
            manifest whose blocks only one network holds is not committed).

Neither step talks to the network. Replicating the delta, auditing custody,
pinning and `lake_head.cljk submit` stay separate, observable steps.
"""
import argparse
import hashlib
import json
import sys

SCHEMA = "yataverse-lake-manifest/v1"
FORMAT = "jsonl {cid,bytes} one row per lake block"


def read_rows(path):
    with open(path, "rb") as f:
        for n, line in enumerate(f, 1):
            if line.strip():
                row = json.loads(line)
                if not (isinstance(row.get("cid"), str) and type(row.get("bytes")) is int and row["bytes"] > 0):
                    raise ValueError(f"{path}:{n}: not a {{cid,bytes}} row")
                yield row


def delta(log_path, diff_path, out_path):
    held = {}
    for row in read_rows(log_path):
        held[row["cid"]] = row["bytes"]
    new, seen = [], set()
    for row in read_rows(diff_path):
        size = held.get(row["cid"])
        if size is not None:
            if size != row["bytes"]:
                raise ValueError(f"{row['cid']}: log holds {size} bytes, capture says {row['bytes']}")
            continue
        if row["cid"] in seen:
            raise ValueError(f"{row['cid']}: duplicated in the capture diff")
        seen.add(row["cid"])
        new.append(row)
    payload = b"".join((json.dumps({"cid": r["cid"], "bytes": r["bytes"]}, separators=(",", ":")) + "\n").encode()
                       for r in new)
    with open(out_path, "wb") as f:
        f.write(payload)
    return {"rows": len(new), "bytes": sum(r["bytes"] for r in new),
            "sha256": hashlib.sha256(payload).hexdigest()}


def manifest(epoch, prev, inventory_cid, inventory_path, captured, relation, custody, authority):
    if epoch < 1 or not prev:
        raise ValueError("a new epoch needs epoch >= 1 and the previous manifest CID")
    wans = {c.get("wan") for c in custody}
    if len(custody) < 2 or len(wans) < 2 or None in wans:
        raise ValueError("custody must name at least two nodes on at least two WANs")
    data = open(inventory_path, "rb").read()
    rows = list(read_rows(inventory_path))
    if not rows:
        raise ValueError("an empty epoch is not committed")
    m = {"schema": SCHEMA, "epoch": epoch, "prev": prev,
         "inventory": {"cid": inventory_cid, "format": FORMAT, "rows": len(rows),
                       "bytes": sum(r["bytes"] for r in rows),
                       "sha256": hashlib.sha256(data).hexdigest(),
                       "captured": captured, "relation": relation},
         "custody": custody, "authority": authority}
    return json.dumps(m, sort_keys=True, separators=(",", ":"))


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("delta")
    d.add_argument("--log-resolved", required=True)
    d.add_argument("--diff", required=True)
    d.add_argument("--out", required=True)
    m = sub.add_parser("manifest")
    m.add_argument("--epoch", type=int, required=True)
    m.add_argument("--prev", required=True)
    m.add_argument("--inventory-cid", required=True)
    m.add_argument("--inventory", required=True)
    m.add_argument("--captured", required=True)
    m.add_argument("--relation", required=True)
    m.add_argument("--custody", action="append", required=True, help='JSON {"node","wan","evidence"}')
    m.add_argument("--authority", required=True)
    m.add_argument("--out", required=True)
    a = p.parse_args(argv)
    try:
        if a.cmd == "delta":
            r = delta(a.log_resolved, a.diff, a.out)
            print(f"DELTA rows={r['rows']} bytes={r['bytes']} sha256={r['sha256']} out={a.out}")
        else:
            text = manifest(a.epoch, a.prev, a.inventory_cid, a.inventory, a.captured, a.relation,
                            [json.loads(c) for c in a.custody], a.authority)
            with open(a.out, "w") as f:
                f.write(text)
            print(f"MANIFEST epoch={a.epoch} bytes={len(text)} sha256={hashlib.sha256(text.encode()).hexdigest()} out={a.out}")
        return 0
    except ValueError as e:
        print(f"REFUSE {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
