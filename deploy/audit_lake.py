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
from raw_block_store import RawBlockStore, RawBlockStoreError, _sha256_digest
from serve_lake import Inventory, InventoryError


CID = re.compile(r"^(?:Qm[1-9A-HJ-NP-Za-km-z]{44}|b[a-z2-7]{20,200})$")


class AuditError(Exception):
    pass


def pins_of_type(ipfs_bin, pin_type):
    if not Path(ipfs_bin).is_file():
        raise AuditError("Kubo binary does not exist")
    pins = set()
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


def durable_pins(ipfs_bin):
    pins = set()
    for pin_type in ("direct", "recursive", "indirect"):
        pins.update(pins_of_type(ipfs_bin, pin_type))
    return pins


def receipt_root(ipfs_bin, root):
    try:
        result = subprocess.run([ipfs_bin, "--offline", "dag", "get", root],
                                capture_output=True, timeout=60, check=False,
                                env=os.environ.copy())
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AuditError("CAR root read failed: " + root) from exc
    if result.returncode:
        raise AuditError("CAR root is unavailable: " + root)
    try:
        return json.loads(result.stdout)
    except ValueError as exc:
        raise AuditError("CAR root is not JSON: " + root) from exc


def verify_repo(ipfs_bin, ipfs_path):
    """Check the selected repository's live daemon without walking its blocks.

    `repo stat` enumerates the entire 18 TiB repository and can exceed the
    audit's 30-second guard while the daemon is healthy. The repository's API
    file selects its daemon; compare that daemon's ID with the configured ID.
    """
    repo = Path(ipfs_path).resolve()
    try:
        expected = json.loads((repo / "config").read_text())["Identity"]["PeerID"]
        api_address = (repo / "api").read_text().strip()
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise AuditError("Kubo repository identity is unavailable") from exc
    if not isinstance(expected, str) or not expected or not api_address:
        raise AuditError("Kubo repository identity is invalid")
    env = os.environ.copy()
    env["IPFS_PATH"] = str(repo)
    try:
        result = subprocess.run([ipfs_bin, "id", "-f", "<id>"], capture_output=True,
                                timeout=10, check=False, env=env)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AuditError("Kubo repository check failed") from exc
    if result.returncode:
        raise AuditError("Kubo repository is unavailable")
    if result.stdout.decode("utf-8", "replace").strip() != expected:
        raise AuditError("Kubo daemon uses a different repository")


def audit_receipts(inventory, car_dir, large_dir, ipfs_path, recursive, direct,
                   raw_store, root_reader, native_dir=None):
    """Count CID-bound rows whose current durable roots still match receipts.

    This deliberately excludes unreceipted indirect pins and does not read
    every leaf block. It is a lower bound, not a full content audit.
    """
    count = len(inventory.offsets)
    covered = bytearray(count)
    car_rows = native_rows = large_rows = direct_rows = raw_rows = 0
    unpinned_car_roots = unpinned_native_roots = unpinned_large_roots = 0
    repository = str(Path(ipfs_path).resolve())
    for path in sorted(Path(car_dir).glob("row-*-import.json")):
        match = re.fullmatch(r"row-(\d+)-(\d+)-import\.json", path.name)
        if not match:
            raise AuditError("invalid CAR receipt name")
        start, last = map(int, match.groups())
        end = last + 1
        if not 0 <= start < end <= count or end - start > 1000:
            raise AuditError("CAR receipt range is invalid")
        rows, actual_end = inventory.rows(start, end - start)
        record = json.loads(path.read_text())
        root = record.get("root")
        if (actual_end != end or record.get("status") != "complete" or
                record.get("inventory_sha256") != inventory.sha256 or
                record.get("start_row") != start or record.get("end_row") != end or
                record.get("blocks") != len(rows) or
                record.get("bytes") != sum(row["size"] for row in rows) or
                record.get("repository") != repository or
                not isinstance(root, str) or not CID.fullmatch(root) or
                not isinstance(record.get("car_sha256"), str) or
                not re.fullmatch(r"[0-9a-f]{64}", record["car_sha256"])):
            raise AuditError("CAR receipt differs from inventory: " + path.name)
        if root not in recursive:
            unpinned_car_roots += 1
            continue
        expected = {"schema": 1, "inventory-sha256": inventory.sha256,
                    "start-row": start, "end-row": end,
                    "links": [{"/": row["cid"]} for row in rows]}
        if root_reader(root) != expected:
            raise AuditError("pinned CAR root differs from inventory: " + root)
        for row in range(start, end):
            covered[row] = 1
        car_rows += len(rows)
    if native_dir is not None:
        for path in sorted(Path(native_dir).glob("row-*-native.json")):
            match = re.fullmatch(r"row-(\d+)-(\d+)-native\.json", path.name)
            if not match:
                raise AuditError("invalid native receipt name")
            start, last = map(int, match.groups())
            end = last + 1
            if not 0 <= start < end <= count or end - start > 1000:
                raise AuditError("native receipt range is invalid")
            rows, actual_end = inventory.rows(start, end - start)
            record = json.loads(path.read_text())
            root = record.get("root")
            if (actual_end != end or record.get("status") != "complete" or
                    record.get("inventory_sha256") != inventory.sha256 or
                    record.get("start_row") != start or record.get("end_row") != end or
                    record.get("blocks") != len(rows) or
                    record.get("bytes") != sum(row["size"] for row in rows) or
                    record.get("repository") != repository or
                    not isinstance(root, str) or not CID.fullmatch(root) or
                    not isinstance(record.get("source_peer"), str) or
                    not record["source_peer"]):
                raise AuditError("native receipt differs from inventory: " + path.name)
            if root not in recursive:
                unpinned_native_roots += 1
                continue
            expected = {"schema": 1, "inventory-sha256": inventory.sha256,
                        "start-row": start, "end-row": end,
                        "links": [{"/": row["cid"]} for row in rows]}
            if root_reader(root) != expected:
                raise AuditError("pinned native root differs from inventory: " + root)
            for row in range(start, end):
                covered[row] = 1
            native_rows += len(rows)
    for path in sorted(Path(large_dir).glob("row-*-jacob-import.json")):
        match = re.fullmatch(r"row-(\d+)-jacob-import\.json", path.name)
        if not match:
            raise AuditError("invalid large recovery receipt name")
        row = int(match.group(1))
        if not 0 <= row < count:
            raise AuditError("large recovery row is invalid")
        item = inventory.rows(row, 1)[0][0]
        record = json.loads(path.read_text())
        root = record.get("recovery_root")
        if (record.get("status") != "complete" or
                record.get("row") != row or
                record.get("inventory_sha256") != inventory.sha256 or
                record.get("original_cid") != item["cid"] or
                record.get("original_bytes") != item["size"] or
                record.get("original_sha256") != _sha256_digest(item["cid"]).hex() or
                record.get("repository") != repository or
                not isinstance(root, str) or not CID.fullmatch(root) or
                not isinstance(record.get("car_sha256"), str) or
                not re.fullmatch(r"[0-9a-f]{64}", record["car_sha256"])):
            raise AuditError("large recovery receipt differs from inventory: " + path.name)
        if root not in recursive:
            unpinned_large_roots += 1
            continue
        if item["cid"] in direct or item["cid"] in recursive:
            covered[row] = 1
            large_rows += 1
    for cid in direct | recursive:
        row = inventory.positions.get(cid)
        if row is not None:
            covered[row] = 1
            direct_rows += 1
    if raw_store is not None:
        for cid in raw_store.cids():
            row = inventory.positions.get(cid)
            if row is not None:
                size = inventory.sizes[cid]
                body = raw_store.read(cid, max_bytes=size)
                if body is None or len(body) != size:
                    raise AuditError("raw block differs from inventory: " + cid)
                covered[row] = 1
                raw_rows += 1
    rows = byte_count = 0
    missing = []
    for cid, row in inventory.positions.items():
        if covered[row]:
            rows += 1
            byte_count += inventory.sizes[cid]
        elif len(missing) < 3:
            missing.append(cid)
    return {"status": "complete" if rows == count else "partial",
            "inventory_rows": count, "inventory_bytes": inventory.total_bytes,
            "covered_rows": rows, "covered_bytes": byte_count,
            "missing_rows": count - rows,
            "missing_bytes": inventory.total_bytes - byte_count,
            "car_root_rows": car_rows, "native_root_rows": native_rows,
            "large_receipt_rows": large_rows,
            "direct_pin_rows": direct_rows, "raw_rows": raw_rows,
            "unpinned_car_roots": unpinned_car_roots,
            "unpinned_native_roots": unpinned_native_roots,
            "unpinned_large_roots": unpinned_large_roots,
            "missing_sample": missing, "inventory_sha256": inventory.sha256,
            "scope": "receipt-and-current-root-pin-lower-bound-not-leaf-readback"}


def audit_inventory(path, expected_sha256, expected_count, pins, raw_store=None):
    if expected_count < 1 or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise AuditError("declared snapshot identity is invalid")
    digest = hashlib.sha256()
    seen = set()
    count = total_bytes = pinned_count = pinned_bytes = raw_count = raw_bytes = 0
    missing = []
    raw_cids = raw_store.cids() if raw_store is not None else set()
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
                elif cid in raw_cids:
                    raw = raw_store.read(cid, max_bytes=size)
                    if raw is None or len(raw) != size:
                        raise AuditError("raw block size differs from inventory: " + cid)
                    raw_count += 1
                    raw_bytes += size
                elif len(missing) < 3:
                    missing.append(cid)
    except OSError as exc:
        raise AuditError("inventory unreadable: " + str(exc)) from exc
    if count != expected_count or digest.hexdigest() != expected_sha256:
        raise AuditError("inventory count or SHA-256 differs from declared snapshot")
    missing_count = count - pinned_count - raw_count
    return {"status": "complete" if missing_count == 0 else "partial",
            "inventory_rows": count, "inventory_bytes": total_bytes,
            "pinned_rows": pinned_count, "pinned_bytes": pinned_bytes,
            "raw_rows": raw_count, "raw_bytes": raw_bytes,
            "missing_rows": missing_count, "missing_bytes": total_bytes - pinned_bytes - raw_bytes,
            "missing_sample": missing, "inventory_sha256": digest.hexdigest(),
            "scope": "durable-pins-plus-cid-verified-raw-blocks"}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--inventory", required=True, type=Path)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--count", required=True, type=int)
    parser.add_argument("--ipfs-bin", required=True)
    parser.add_argument("--raw-block-store", type=Path)
    parser.add_argument("--car-receipts", type=Path)
    parser.add_argument("--large-receipts", type=Path)
    parser.add_argument("--native-receipts", type=Path)
    parser.add_argument("--ipfs-path", type=Path)
    parser.add_argument("--require-complete", action="store_true")
    args = parser.parse_args()
    try:
        raw = RawBlockStore(args.raw_block_store) if args.raw_block_store else None
        if args.car_receipts or args.large_receipts or args.native_receipts or args.ipfs_path:
            if not all((args.car_receipts, args.large_receipts, args.ipfs_path)):
                raise AuditError("receipt audit requires both directories and Kubo repository")
            if not args.car_receipts.is_dir() or not args.large_receipts.is_dir():
                raise AuditError("receipt directory is absent")
            if args.native_receipts and not args.native_receipts.is_dir():
                raise AuditError("native receipt directory is absent")
            os.environ["IPFS_PATH"] = str(args.ipfs_path.resolve())
            verify_repo(args.ipfs_bin, args.ipfs_path)
            inventory = Inventory(args.inventory, args.sha256, args.count)
            report = audit_receipts(
                inventory, args.car_receipts, args.large_receipts, args.ipfs_path,
                pins_of_type(args.ipfs_bin, "recursive"),
                pins_of_type(args.ipfs_bin, "direct"), raw,
                lambda root: receipt_root(args.ipfs_bin, root), args.native_receipts)
        else:
            report = audit_inventory(args.inventory, args.sha256, args.count,
                                     durable_pins(args.ipfs_bin), raw)
    except (AuditError, InventoryError, RawBlockStoreError, OSError, ValueError) as exc:
        print("REFUSED: " + str(exc), file=sys.stderr)
        return 2
    print(json.dumps(report, sort_keys=True))
    if args.require_complete and report["status"] != "complete":
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
