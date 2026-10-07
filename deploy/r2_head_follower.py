#!/usr/bin/env python3
"""Keep R2's yataverse graph heads a projection of inga (ADR-2610062000 D5, P5 step 2).

After the P5 cutover, a graph head is decided by inga (through yataverse-writer),
and R2's heads/yataverse/ipns/<graph>.json is a copy a follower writes. This is
that follower.

For each graph it reads the committed head from the writer (GET /v1/graph-head,
whose document carries the namespace-signed record) and the R2 object, and
classifies the pair:

  agree        same sequence and value
  r2-ahead     R2 is newer. Normal BEFORE the cutover, while R2 is still the
               authority and the mirror follows it. After the cutover it means
               something wrote R2 around the writer, and it is reported.
  inga-ahead   inga is newer. Normal AFTER the cutover. With --project, the
               follower writes the committed signed record to R2.
  diverged     the same sequence names different values. Never projected over;
               reported for a human.
  absent       one side has no head.

--project writes only `inga-ahead` pairs, and only the exact record inga holds,
whose signature the writer already verified. Writing R2 is an owner decision:
before the cutover, run without --project.

Exit codes: 0 every graph agrees or was projected; 1 a graph is r2-ahead after
--cutover, or diverged; 3 a graph could not be measured.
"""

import argparse
import json
import shlex
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request


def classify(inga_head, r2_head):
    """Pure. Each argument is a head record {sequence, value, ...} or None."""
    if inga_head is None or r2_head is None:
        return "absent"
    a, b = inga_head["sequence"], r2_head["sequence"]
    if a == b:
        return "agree" if inga_head["value"] == r2_head["value"] else "diverged"
    return "inga-ahead" if a > b else "r2-ahead"


def inga_head(writer_url, graph):
    try:
        with urllib.request.urlopen(f"{writer_url.rstrip('/')}/v1/graph-head?graph={graph}", timeout=300) as r:
            return json.load(r)["document"]["head"]
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise RuntimeError(f"writer {e.code}")


def r2_head(get_cmd, graph):
    r = subprocess.run(shlex.split(get_cmd.format(graph=graph)), capture_output=True, timeout=120)
    if r.returncode != 0:
        if b"does not exist" in r.stderr + r.stdout:
            return None
        raise RuntimeError(f"R2 read failed: {r.stderr.decode(errors='replace')[-160:]}")
    return json.loads(r.stdout)


def project(put_cmd, graph, record):
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(record, f, separators=(",", ":"))
        path = f.name
    r = subprocess.run(shlex.split(put_cmd.format(graph=graph, file=path)), capture_output=True, timeout=120)
    if r.returncode != 0:
        raise RuntimeError(f"R2 write failed: {r.stderr.decode(errors='replace')[-160:]}")


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--graph", action="append", required=True)
    p.add_argument("--writer-url", required=True)
    p.add_argument("--r2-get-cmd", required=True, help="prints the R2 head JSON; {graph}")
    p.add_argument("--r2-put-cmd", help="writes {file} to the R2 head key; {graph} {file}")
    p.add_argument("--project", action="store_true", help="write inga-ahead heads to R2 (after the cutover)")
    p.add_argument("--cutover", action="store_true", help="inga is the authority: r2-ahead is an error")
    a = p.parse_args(argv)
    if a.project and not (a.r2_put_cmd and a.cutover):
        p.error("--project needs --r2-put-cmd and --cutover")
    worst = 0
    for g in a.graph:
        try:
            i, r = inga_head(a.writer_url, g), r2_head(a.r2_get_cmd, g)
        except Exception as e:
            print(f"UNMEASURED {g}: {e}", flush=True)
            worst = 3
            continue
        c = classify(i, r)
        detail = f"inga {i and i['sequence']} r2 {r and r['sequence']}"
        if c == "inga-ahead" and a.project:
            # R2 has no conditional put from here, so narrow the race instead:
            # the Worker may copy a NEWER head between our read and our write,
            # and an unconditional write would roll R2 back. Re-read R2 just
            # before writing, and after writing re-check and repair once more.
            # inga stays the authority; a rollback that slips through is
            # repaired on the next run.
            try:
                done = False
                for _ in range(3):
                    i, r = inga_head(a.writer_url, g), r2_head(a.r2_get_cmd, g)
                    if classify(i, r) != "inga-ahead":
                        done = True
                        break
                    project(a.r2_put_cmd, g, i)
                print(f"PROJECTED {g} {detail}", flush=True)
            except Exception as e:
                print(f"UNMEASURED {g}: {e}", flush=True)
                worst = 3
            continue
        print(f"{c.upper()} {g} {detail}", flush=True)
        if c == "diverged" or (c == "r2-ahead" and a.cutover):
            worst = max(worst, 1) if worst != 3 else 3
    return worst


if __name__ == "__main__":
    sys.exit(main())
