#!/usr/bin/env python3
"""Count durable local lake pins against an immutable inventory snapshot.

This is a coverage audit, not a content readback. Individual bytes are
checked and receipted by replicate_lake.py when they are copied.
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path


CID = re.compile(r"^(?:Qm[1-9A-HJ-NP-Za-km-z]{44}|b[a-z2-7]{20,200})$")


class AuditError(Exception):
    pass


def durable_pins(ipfs_bin):
    if not Path(ipfs_bin).is_file():
        raise AuditError("Kubo binary does not exist")
    pins = set()
    for pin_type in ("direct", "recursive"):
        try:
            result = subprocess.run(
                [ipfs_bin, "pin", "ls", "--type=" + pin_type],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=600, check=False, env=os.environ.copy(),
            )
        except subprocess.TimeoutExpired as exc:
            raise AuditError("Kubo pin listing timed out: " + pin_type) from exc
        except OSError as exc:
            raise AuditError("Kubo pin listing could not run: " + str(exc)) from exc
        if result.returncode:
            raise AuditError("Kubo pin listing failed: " + pin_type + " (" +
                             result.stderr.decode("utf-8", "replace")[:160].strip() + ")")
        for line in result.stdout.decode("utf-8", "replace").splitlines():
            parts = line.split()
            if len(parts) != 2 or not CID.fullmatch(parts[0]) or parts[1] != pin_type:
                raise AuditError("Kubo pin listing has invalid line")
            pins.add(parts[0])
    return pins


def audit_inventory(path, expected_sha256, expected_count, pins):
    if expected_count < 1 or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise AuditError("declared snapshot identity is invalid")
    digest = hashlib.sha256()
    seen = set()
    count = total_bytes = pinned_count = pinned_bytes = 0
    missing = []
    try:
        with Path(path).open("rb") as source:
            for line in source:
                digest.update(line)
                try:
                    row = json.loads(line)
                    cid, size = row["cid"], row["bytes"]
                except (ValueError, UnicodeDecodeError, KeyError, TypeError) as exc:
                    raise AuditError("invalid inventory row at " + str(count + 1)) from exc
                if not isinstance(cid, str) or not CID.fullmatch(cid) or type(size) is not int or size < 1:
                    raise AuditError("invalid CID or size at " + str(count + 1))
                if cid in seen:
                    raise AuditError("duplicate inventory CID at " + str(count + 1))
                seen.add(cid)
                count += 1
                total_bytes += size
                if cid in pins:
                    pinned_count += 1
                    pinned_bytes += size
                elif len(missing) < 3:
                    missing.append(cid)
    except OSError as exc:
        raise AuditError("inventory unreadable: " + str(exc)) from exc
    if count != expected_count or digest.hexdigest() != expected_sha256:
        raise AuditError("inventory count or SHA-256 differs from declared snapshot")
    missing_count = count - pinned_count
    return {"status": "complete" if missing_count == 0 else "partial",
            "inventory_rows": count, "inventory_bytes": total_bytes,
            "pinned_rows": pinned_count, "pinned_bytes": pinned_bytes,
            "missing_rows": missing_count, "missing_bytes": total_bytes - pinned_bytes,
            "missing_sample": missing, "inventory_sha256": digest.hexdigest(),
            "scope": "durable-pin-coverage-not-content-readback"}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--inventory", required=True, type=Path)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--count", required=True, type=int)
    parser.add_argument("--ipfs-bin", required=True)
    parser.add_argument("--require-complete", action="store_true")
    args = parser.parse_args()
    try:
        report = audit_inventory(args.inventory, args.sha256, args.count,
                                 durable_pins(args.ipfs_bin))
    except AuditError as exc:
        print("REFUSED: " + str(exc), file=sys.stderr)
        return 2
    print(json.dumps(report, sort_keys=True))
    if args.require_complete and report["status"] != "complete":
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
