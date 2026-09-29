#!/usr/bin/env python3
"""Advance the dated oversized-block recovery plan on one owned node."""

import argparse
import fcntl
import json
import os
import re
import subprocess
import sys
import uuid
from pathlib import Path

from serve_lake import Inventory, InventoryError


CID = re.compile(r"b[a-z2-7]{20,200}\Z")


class BatchError(Exception):
    pass


def atomic_json(path, value):
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".partial")
    with temp.open("x") as output:
        json.dump(value, output, sort_keys=True)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    temp.replace(path)
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def plan(inventory, expected_count, expected_bytes):
    rows = []
    with inventory.path.open("rb") as source:
        for index, line in enumerate(source):
            item = json.loads(line)
            if item["bytes"] > 8_000_000:
                if (not CID.fullmatch(item["cid"]) or
                        not 8_000_000 < item["bytes"] <= 256_000_000):
                    raise BatchError("oversized row has unsupported CID or size")
                rows.append((index, item["cid"], item["bytes"]))
    if len(rows) != expected_count or sum(row[2] for row in rows) != expected_bytes:
        raise BatchError("oversized plan differs from dated inventory")
    return rows


def pinned(ipfs, repo, cid):
    env = dict(os.environ, IPFS_PATH=str(repo.resolve()))
    try:
        # The recovery importer pins the original raw CID directly. Looking up
        # indirect pins traverses Jacob's growing CAR graph and can time out.
        result = subprocess.run([str(ipfs), "pin", "ls", "--type=direct", cid],
                                env=env, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BatchError("Kubo pin check failed") from exc
    return result.returncode == 0 and result.stdout.strip() == cid + " direct"


def advance(args):
    if (not 1 <= args.max_new <= 20 or args.expected_large_count < 1 or
            args.expected_large_bytes < 1 or args.min_free_bytes < 0):
        raise BatchError("invalid batch bounds")
    inventory = Inventory(args.inventory, args.sha256, args.count)
    rows = plan(inventory, args.expected_large_count, args.expected_large_bytes)
    args.state_dir.mkdir(parents=True, exist_ok=True)
    args.receipt_dir.mkdir(parents=True, exist_ok=True)
    with (args.state_dir / "import.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise BatchError("large recovery batch already running") from exc
        checkpoint = args.state_dir / "checkpoint.json"
        state = json.loads(checkpoint.read_text()) if checkpoint.exists() else {
            "cursor": 0, "inventory_sha256": args.sha256}
        cursor = state.get("cursor")
        if state.get("inventory_sha256") != args.sha256 or type(cursor) is not int or not 0 <= cursor <= len(rows):
            raise BatchError("checkpoint identity differs")
        imported = 0
        skipped = 0
        while cursor < len(rows) and imported < args.max_new:
            row, cid, size = rows[cursor]
            name = "row-{}-recovery.car".format(row)
            car = args.archive / name
            source_receipt = args.archive / (name + ".json")
            destination_receipt = args.receipt_dir / ("row-{}-jacob-import.json".format(row))
            if destination_receipt.exists():
                saved = json.loads(destination_receipt.read_text())
                if (saved.get("status") != "complete" or saved.get("row") != row or
                        saved.get("inventory_sha256") != args.sha256 or
                        saved.get("original_cid") != cid or
                        saved.get("original_bytes") != size or
                        not pinned(args.ipfs_bin, args.ipfs_path, cid)):
                    raise BatchError("saved import receipt or Kubo pin differs")
                skipped += 1
            else:
                if not car.is_file() or not source_receipt.is_file():
                    return {"status": "waiting-source", "row": row,
                            "cursor": cursor, "imported": imported, "skipped": skipped}
                command = [sys.executable, str(args.importer),
                           "--inventory", str(args.inventory), "--sha256", args.sha256,
                           "--count", str(args.count), "--row", str(row),
                           "--car", str(car), "--source-receipt", str(source_receipt),
                           "--ipfs-bin", str(args.ipfs_bin), "--ipfs-path", str(args.ipfs_path),
                           "--min-free-bytes", str(args.min_free_bytes),
                           "--output-receipt", str(destination_receipt)]
                try:
                    result = subprocess.run(command, capture_output=True, text=True,
                                            timeout=1800, check=False)
                except (OSError, subprocess.TimeoutExpired) as exc:
                    raise BatchError("large recovery importer failed to run") from exc
                if result.returncode:
                    raise BatchError("large recovery importer refused row " + str(row) +
                                     ": " + result.stderr[-400:].strip())
                completed = json.loads(result.stdout.splitlines()[-1])
                if (completed.get("status") != "complete" or completed.get("row") != row or
                        completed.get("original_cid") != cid or not destination_receipt.is_file()):
                    raise BatchError("large recovery import receipt differs")
                imported += 1
            cursor += 1
            atomic_json(checkpoint, {"cursor": cursor,
                                     "inventory_sha256": args.sha256,
                                     "last_row": row})
        return {"status": "complete" if cursor == len(rows) else "partial",
                "cursor": cursor, "total": len(rows),
                "imported": imported, "skipped": skipped}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--inventory", required=True, type=Path)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--count", required=True, type=int)
    parser.add_argument("--expected-large-count", required=True, type=int)
    parser.add_argument("--expected-large-bytes", required=True, type=int)
    parser.add_argument("--archive", required=True, type=Path)
    parser.add_argument("--receipt-dir", required=True, type=Path)
    parser.add_argument("--state-dir", required=True, type=Path)
    parser.add_argument("--importer", required=True, type=Path)
    parser.add_argument("--ipfs-bin", required=True, type=Path)
    parser.add_argument("--ipfs-path", required=True, type=Path)
    parser.add_argument("--min-free-bytes", type=int, default=1_000_000_000_000)
    parser.add_argument("--max-new", type=int, default=5)
    args = parser.parse_args()
    try:
        print(json.dumps(advance(args), sort_keys=True))
        return 0
    except (BatchError, InventoryError, OSError, ValueError, KeyError, IndexError) as exc:
        print("REFUSED: " + str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
