#!/usr/bin/env python3
"""Publish only new lake CIDs after two complete cursor walks agree.

The result is a compact inventory suitable for a separate CAR lane. The two
walks are a stability check, not an atomic snapshot or writer cutover.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import uuid

from replicate_lake import valid_cid


class DeltaError(Exception):
    pass


def sha_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def completed_candidate(path, state_path, old_sha):
    state = json.loads(state_path.read_text())
    if (state.get("schema") != 1 or state.get("status") != "complete" or
            state.get("output") != str(path.resolve()) or
            state.get("old_sha256") != old_sha or
            state.get("size") != path.stat().st_size or
            state.get("sha256") != sha_file(path) or
            type(state.get("rows")) is not int or state["rows"] < 1 or
            type(state.get("bytes")) is not int or state["bytes"] < 1):
        raise DeltaError("candidate or completion receipt differs")
    return state


def inventory_rows(file):
    previous = ""
    for raw in file:
        try:
            row = json.loads(raw)
            cid, size = row["cid"], row["bytes"]
        except (ValueError, KeyError, TypeError) as exc:
            raise DeltaError("inventory row is invalid") from exc
        if not valid_cid(cid) or cid <= previous or type(size) is not int or size < 1:
            raise DeltaError("inventory CID order or size is invalid")
        previous = cid
        yield cid, size


def delta(args):
    old, first, second = (path.resolve() for path in
                          (args.old_inventory, args.candidate_a, args.candidate_b))
    output = args.output.resolve()
    receipt = args.receipt.resolve()
    if len({old, first, second, output, receipt}) != 5:
        raise DeltaError("input and output paths overlap")
    if output.exists() or receipt.exists():
        raise DeltaError("delta output or receipt already exists")
    old_sha = sha_file(old)
    if old_sha != args.old_sha256:
        raise DeltaError("old inventory digest differs")
    state_a = completed_candidate(first, args.candidate_a_state, old_sha)
    state_b = completed_candidate(second, args.candidate_b_state, old_sha)
    for key in ("sha256", "rows", "bytes", "size"):
        if state_a[key] != state_b[key]:
            raise DeltaError("two complete walks differ: " + key)

    output.parent.mkdir(parents=True, exist_ok=True)
    receipt.parent.mkdir(parents=True, exist_ok=True)
    tmp = output.with_name(output.name + "." + uuid.uuid4().hex + ".partial")
    old_count = new_count = old_bytes = new_bytes = delta_count = delta_bytes = 0
    maximum = oversized = 0
    digest = hashlib.sha256()
    try:
        with old.open("rb") as old_file, first.open("rb") as new_file, tmp.open("xb") as target:
            older = inventory_rows(old_file)
            previous = next(older, None)
            for cid, size in inventory_rows(new_file):
                new_count += 1
                new_bytes += size
                if previous is not None and previous[0] < cid:
                    raise DeltaError("new inventory omits old CID: " + previous[0])
                if previous is not None and previous[0] == cid:
                    if previous[1] != size:
                        raise DeltaError("new inventory changes old CID size: " + cid)
                    old_count += 1
                    old_bytes += size
                    previous = next(older, None)
                    continue
                line = json.dumps({"cid": cid, "bytes": size}, separators=(",", ":")).encode() + b"\n"
                target.write(line)
                digest.update(line)
                delta_count += 1
                delta_bytes += size
                maximum = max(maximum, size)
                oversized += size > 8_000_000
            if previous is not None:
                raise DeltaError("new inventory omits old CID: " + previous[0])
            target.flush()
            os.fsync(target.fileno())
        if (new_count != state_a["rows"] or new_bytes != state_a["bytes"] or
                new_count != old_count + delta_count or new_bytes != old_bytes + delta_bytes):
            raise DeltaError("candidate counts differ from completion receipt")
        record = {"schema": 1, "status": "complete", "old_sha256": old_sha,
                  "candidate_sha256": state_a["sha256"], "candidate_rows": new_count,
                  "candidate_bytes": new_bytes, "old_rows": old_count,
                  "old_bytes": old_bytes, "delta_rows": delta_count,
                  "delta_bytes": delta_bytes, "delta_sha256": digest.hexdigest(),
                  "delta_max_block_bytes": maximum, "delta_oversized_rows": oversized,
                  "scope": "two matching non-atomic public listing walks"}
        tmp.replace(output)
        directory = os.open(output.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        staged = receipt.with_name(receipt.name + "." + uuid.uuid4().hex + ".partial")
        with staged.open("x") as file:
            file.write(json.dumps(record, sort_keys=True) + "\n")
            file.flush()
            os.fsync(file.fileno())
        staged.replace(receipt)
        return record
    finally:
        tmp.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--old-inventory", required=True, type=Path)
    parser.add_argument("--old-sha256", required=True)
    parser.add_argument("--candidate-a", required=True, type=Path)
    parser.add_argument("--candidate-a-state", required=True, type=Path)
    parser.add_argument("--candidate-b", required=True, type=Path)
    parser.add_argument("--candidate-b-state", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--receipt", required=True, type=Path)
    args = parser.parse_args()
    try:
        print(json.dumps(delta(args), sort_keys=True))
    except (DeltaError, OSError, ValueError, TypeError, KeyError) as exc:
        print("REFUSED: " + str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
