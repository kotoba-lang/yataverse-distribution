#!/usr/bin/env python3
"""Project the inga lake log into the `lake-log/` layout the Worker's opt-in
log listing reads (cloud-kotoba/kotobase-ipfs#71, ADR-2610062000 P3).

    lake-log/head.json
      {"seq", "manifest", "bundle", "rows",
       "epochs": [{"epoch", "inventory", "sha256", "rows", "page_rows", "prefix"}]}
    lake-log/pages/<inventory sha256>/<n>.json    a JSON array of {cid, bytes}

Input is the per-epoch inventories in log order, each checked against its
sha256 and row count, plus the verified head. The projection is a copy:
pages are content-named by the inventory's sha256, so re-running writes the
same objects. `head.json` is written LAST, so a reader never sees a head that
names pages which are not there yet.

`--out DIR` writes the layout locally. `--put-cmd` is a template run per
object with {key} and {file}, for example
`wrangler r2 object put kotobase-graph-database-production/{key} --file {file} --remote`.
Writing to a production bucket is an owner decision; this tool does not
default to it.

Exit codes: 0 projected; 1 an input refused its check; 3 a write failed.
"""

import argparse
import hashlib
import json
import os
import shlex
import subprocess
import sys

PAGE_ROWS = 200


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def parse_epoch(spec):
    """EPOCH:INVENTORY:SHA256:ROWS:INVENTORY_CID"""
    epoch, path, sha, rows, cid = spec.split(":", 4)
    return {"epoch": int(epoch), "path": path, "sha256": sha, "rows": int(rows), "inventory": cid}


def build(epochs, head, page_rows=PAGE_ROWS):
    """Pure over files: -> list of (key, bytes), head.json last."""
    objects, listed, total = [], [], 0
    for e in epochs:
        if sha256_file(e["path"]) != e["sha256"]:
            raise ValueError(f"epoch {e['epoch']}: inventory sha256 differs from the manifest")
        with open(e["path"]) as f:
            rows = [json.loads(l) for l in f if l.strip()]
        if len(rows) != e["rows"]:
            raise ValueError(f"epoch {e['epoch']}: {len(rows)} rows, manifest says {e['rows']}")
        prefix = f"lake-log/pages/{e['sha256']}/"
        for n in range(0, len(rows), page_rows):
            page = [{"cid": r["cid"], "bytes": r["bytes"]} for r in rows[n:n + page_rows]]
            objects.append((f"{prefix}{n // page_rows}.json",
                            json.dumps(page, separators=(",", ":")).encode()))
        listed.append({"epoch": e["epoch"], "inventory": e["inventory"], "sha256": e["sha256"],
                       "rows": e["rows"], "page_rows": page_rows, "prefix": prefix})
        total += e["rows"]
    if [e["epoch"] for e in listed] != list(range(len(listed))):
        raise ValueError("epochs must be given in log order from 0 without gaps")
    if head["seq"] != len(listed) - 1:
        raise ValueError(f"head seq {head['seq']} does not match {len(listed)} epochs")
    head_doc = dict(head, rows=total, epochs=listed)
    objects.append(("lake-log/head.json", json.dumps(head_doc, sort_keys=True, separators=(",", ":")).encode()))
    return objects


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--epoch", action="append", required=True, type=parse_epoch,
                   help="EPOCH:INVENTORY:SHA256:ROWS:INVENTORY_CID, once per epoch in log order")
    p.add_argument("--seq", type=int, required=True)
    p.add_argument("--manifest", required=True)
    p.add_argument("--bundle", required=True)
    where = p.add_mutually_exclusive_group(required=True)
    where.add_argument("--out", help="write the layout under this directory")
    where.add_argument("--put-cmd", help="template run per object with {key} and {file}")
    p.add_argument("--staging", default="/tmp/lake-log-projection", help="where --put-cmd files are staged")
    a = p.parse_args(argv)
    try:
        objects = build(a.epoch, {"seq": a.seq, "manifest": a.manifest, "bundle": a.bundle})
    except (ValueError, OSError) as e:
        print("REFUSE", e, flush=True)
        return 1
    root = a.out or a.staging
    for key, data in objects:
        path = os.path.join(root, key)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)
        if a.put_cmd:
            r = subprocess.run(shlex.split(a.put_cmd.format(key=key, file=path)), capture_output=True)
            if r.returncode != 0:
                print("UNMEASURED put", key, r.stderr.decode(errors="replace")[-200:], flush=True)
                return 3
    print(f"PROJECTED seq {a.seq} objects {len(objects)} rows {sum(e['rows'] for e in a.epoch)} "
          f"{'to ' + a.out if a.out else 'via put-cmd'}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
