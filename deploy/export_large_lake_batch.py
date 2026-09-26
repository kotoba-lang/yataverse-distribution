#!/usr/bin/env python3
"""Resume oversized lake CAR export and place each verified CAR on both nodes."""

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from export_large_lake_block import (
    ExportError, MIN_LARGE_BLOCK, digest_file, export,
)
from raw_block_store import _sha256_digest
from serve_lake import Inventory, InventoryError


NODES = {
    "gad": ("gad", "/home/gad/.local/state/yataverse-lake/car-exports", "gad"),
    "xavier": ("root@xavier", "/mnt/nvme/ipfs-xavier/lake-state/car-exports", "xavier"),
}
SOURCE = {
    "gad": "https://yataverse-data.220-146-170-114.sslip.io:8444/ipfs/",
    "xavier": "https://yataverse-data.220-146-170-114.sslip.io:8443/ipfs/",
}
MIN_FREE = 50_000_000_000


def command(argv, timeout=300):
    try:
        done = subprocess.run(argv, capture_output=True, check=False, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ExportError("batch command could not run: " + argv[0]) from exc
    if done.returncode:
        raise ExportError("batch command refused: " + argv[0] + " " +
                          done.stderr.decode("utf8", "replace")[:180].strip())
    return done.stdout.decode("utf8", "replace")


def validate_record(record, row, cid, size, inventory_sha):
    if (record.get("schema") != 1 or record.get("row") != row or
            record.get("inventory_sha256") != inventory_sha or
            record.get("original_cid") != cid or
            record.get("original_bytes") != size or
            record.get("original_sha256") != _sha256_digest(cid).hex() or
            record.get("validation") !=
            "fresh-offline-kubo-import-cat-and-original-cid-rehydration" or
            type(record.get("car_bytes")) is not int or
            not 0 < record["car_bytes"] <= 536_870_912 or
            not isinstance(record.get("car_sha256"), str) or
            not re.fullmatch(r"[0-9a-f]{64}", record["car_sha256"]) or
            not isinstance(record.get("recovery_root"), str) or
            not re.fullmatch(r"b[a-z2-7]{20,200}", record["recovery_root"])):
        raise ExportError("recovery receipt differs from inventory row")


def local_status(car, row, cid, size, inventory_sha):
    receipt = Path(str(car) + ".json")
    if not car.exists() and not receipt.exists():
        return None
    if not car.is_file() or not receipt.is_file():
        raise ExportError("local CAR and receipt are incomplete")
    record = json.loads(receipt.read_text())
    validate_record(record, row, cid, size, inventory_sha)
    actual_size, digest = digest_file(car, 536_870_912)
    if actual_size != record["car_bytes"] or digest.hex() != record["car_sha256"]:
        raise ExportError("local CAR differs from its receipt")
    return record


def remote_status(node, row, cid, size, inventory_sha):
    host, directory, _owner = NODES[node]
    car = directory + "/row-" + str(row) + "-recovery.car"
    receipt = car + ".json"
    probe = command(["ssh", "-o", "BatchMode=yes", host,
                     "if test -f " + car + " && test -f " + receipt +
                     "; then cat " + receipt + "; else printf MISSING; fi"], timeout=15)
    if probe == "MISSING":
        return None
    record = json.loads(probe)
    validate_record(record, row, cid, size, inventory_sha)
    report = command(["ssh", "-o", "BatchMode=yes", host,
                      "sha256sum " + car + " && stat -c %s " + car], timeout=180)
    lines = report.splitlines()
    if (len(lines) != 2 or lines[0].split()[0] != record["car_sha256"] or
            int(lines[1]) != record["car_bytes"]):
        raise ExportError(node + " CAR differs from its receipt")
    return record


def put_remote(node, car, record):
    host, directory, owner = NODES[node]
    target = directory + "/" + car.name
    receipt = str(car) + ".json"
    free = int(command(["ssh", "-o", "BatchMode=yes", host,
                        "df -B1 --output=avail " + directory + " | tail -1"],
                       timeout=15).strip())
    if free - record["car_bytes"] < MIN_FREE:
        raise ExportError(node + " disk reserve would be crossed")
    command(["scp", "-q", str(car), host + ":" + target + ".partial"], timeout=600)
    command(["scp", "-q", receipt, host + ":" + target + ".json.partial"], timeout=30)
    command(["ssh", "-o", "BatchMode=yes", host,
             "printf '%s  %s\\n' " + record["car_sha256"] + " " + target +
             ".partial | sha256sum -c - && mv " + target + ".partial " + target +
             " && mv " + target + ".json.partial " + target + ".json" +
             " && chown " + owner + ":" + owner + " " + target + " " + target + ".json"],
            timeout=180)


def large_rows(inventory):
    with inventory.path.open("rb") as source:
        for row, line in enumerate(source):
            data = json.loads(line)
            if data["bytes"] > MIN_LARGE_BLOCK:
                yield row, data["cid"], data["bytes"]


def run_batch(args):
    inventory = Inventory(args.inventory, args.sha256, args.count)
    if not Path(args.ipfs_bin).is_file():
        raise ExportError("Kubo binary does not exist")
    rows = list(large_rows(inventory))
    if len(rows) != args.expected_large_count:
        raise ExportError("large-block plan count differs")
    if args.plan:
        print(json.dumps({"status": "plan", "rows": len(rows),
                          "bytes": sum(size for _row, _cid, size in rows),
                          "first": rows[0][0], "last": rows[-1][0]}))
        return
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    processed = skipped = 0
    for row, cid, size in rows:
        if row < args.start_row:
            continue
        car = output_dir / ("row-" + str(row) + "-recovery.car")
        remote = {node: remote_status(node, row, cid, size, args.sha256)
                  for node in NODES}
        if all(remote.values()):
            if remote["gad"] != remote["xavier"]:
                raise ExportError("node receipts differ for row " + str(row))
            local = local_status(car, row, cid, size, args.sha256)
            if local is not None and local != remote["gad"]:
                raise ExportError("local receipt differs from nodes for row " + str(row))
            skipped += 1
            continue
        record = local_status(car, row, cid, size, args.sha256)
        if record is None:
            if shutil.disk_usage(output_dir).free - 4 * size < MIN_FREE:
                raise ExportError("local disk reserve would be crossed")
            source_node = "xavier" if row % 2 == 0 else "gad"
            params = SimpleNamespace(inventory=args.inventory, sha256=args.sha256,
                                     count=args.count, row=row,
                                     base_url=SOURCE[source_node], ipfs_bin=args.ipfs_bin,
                                     output=str(car))
            record = export(params, inventory=inventory, emit=False)
        for node in NODES:
            if remote[node] is None:
                put_remote(node, car, record)
            check = remote_status(node, row, cid, size, args.sha256)
            if check != record:
                raise ExportError(node + " post-copy receipt differs for row " + str(row))
        processed += 1
        print(json.dumps({"status": "mirrored", "row": row,
                          "car_bytes": record["car_bytes"],
                          "car_sha256": record["car_sha256"]}), flush=True)
        if processed >= args.max_new:
            break
    print(json.dumps({"status": "batch-complete", "new": processed,
                      "already-mirrored": skipped, "planned": len(rows)}))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--inventory", required=True)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--count", required=True, type=int)
    parser.add_argument("--expected-large-count", required=True, type=int)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--ipfs-bin", required=True)
    parser.add_argument("--start-row", type=int, default=0)
    parser.add_argument("--max-new", type=int, default=1)
    parser.add_argument("--plan", action="store_true")
    args = parser.parse_args()
    if args.max_new < 1 or args.start_row < 0:
        parser.error("--max-new must be positive and --start-row nonnegative")
    try:
        run_batch(args)
    except (ExportError, InventoryError, OSError, ValueError, KeyError) as exc:
        print("REFUSED: " + str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
