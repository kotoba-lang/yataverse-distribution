#!/usr/bin/env python3
"""Restore one oversized lake block from a receipted UnixFS recovery CAR."""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

from raw_block_store import _sha256_digest
from serve_lake import Inventory, InventoryError


CID = re.compile(r"b[a-z2-7]{20,200}\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
MAX_BLOCK = 256_000_000
MAX_CAR = 536_870_912


class RecoveryError(Exception):
    pass


def file_digest(path, ceiling):
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            size += len(chunk)
            if size > ceiling:
                raise RecoveryError("file exceeds recovery byte ceiling")
            digest.update(chunk)
    return size, digest.hexdigest()


def run(ipfs, env, *args, stdout=None, timeout=900):
    try:
        result = subprocess.run([ipfs, *args], env=env,
                                stdout=stdout if stdout is not None else subprocess.PIPE,
                                stderr=subprocess.PIPE, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RecoveryError("Kubo command failed: " + " ".join(args[:2])) from exc
    if result.returncode:
        raise RecoveryError("Kubo refused " + " ".join(args[:2]) + ": " +
                            result.stderr.decode("utf-8", "replace")[:250].strip())
    return result.stdout.decode("utf-8", "replace") if stdout is None else ""


def verify_daemon_identity(ipfs, env):
    configured_id = run(ipfs, env, "config", "Identity.PeerID", timeout=30).strip()
    daemon_id = run(ipfs, env, "id", "-f=<id>", timeout=30).strip()
    if not configured_id or configured_id != daemon_id:
        raise RecoveryError("Kubo daemon uses a different repository identity")


def receipt_matches(args, inventory):
    with args.inventory.open("rb") as source:
        source.seek(inventory.offsets[args.row])
        row = json.loads(source.readline())
    try:
        receipt = json.loads(args.source_receipt.read_text())
    except (OSError, ValueError) as exc:
        raise RecoveryError("recovery source receipt is unreadable") from exc
    cid, size = row["cid"], row["bytes"]
    if (receipt.get("schema") != 1 or receipt.get("row") != args.row or
            receipt.get("inventory_sha256") != args.sha256 or
            receipt.get("original_cid") != cid or
            receipt.get("original_bytes") != size or
            not isinstance(cid, str) or not CID.fullmatch(cid) or
            type(size) is not int or not 8_000_000 < size <= MAX_BLOCK or
            receipt.get("original_sha256") != _sha256_digest(cid).hex() or
            receipt.get("validation") !=
            "fresh-offline-kubo-import-cat-and-original-cid-rehydration" or
            not isinstance(receipt.get("recovery_root"), str) or
            not CID.fullmatch(receipt["recovery_root"]) or
            not isinstance(receipt.get("car_sha256"), str) or
            not SHA256.fullmatch(receipt["car_sha256"]) or
            type(receipt.get("car_bytes")) is not int or
            not 1 <= receipt["car_bytes"] <= MAX_CAR):
        raise RecoveryError("recovery receipt differs from immutable inventory")
    car_size, car_sha = file_digest(args.car, MAX_CAR)
    if car_size != receipt["car_bytes"] or car_sha != receipt["car_sha256"]:
        raise RecoveryError("recovery CAR differs from source receipt")
    return receipt


def recover(args):
    if not SHA256.fullmatch(args.sha256) or args.count < 1 or args.row < 0:
        raise RecoveryError("invalid inventory identity or row")
    if args.min_free_bytes < 0 or not args.ipfs_bin.is_file() or not args.ipfs_path.is_dir():
        raise RecoveryError("invalid disk reserve or Kubo location")
    inventory = Inventory(args.inventory, args.sha256, args.count)
    if args.row >= len(inventory.offsets):
        raise RecoveryError("row outside inventory")
    receipt = receipt_matches(args, inventory)
    if (shutil.disk_usage(args.ipfs_path).free -
            2 * (receipt["car_bytes"] + receipt["original_bytes"]) < args.min_free_bytes):
        raise RecoveryError("physical disk reserve would be crossed")
    ipfs = str(args.ipfs_bin)
    env = dict(os.environ, IPFS_PATH=str(args.ipfs_path.resolve()))
    # repo stat walks the growing Jacob datastore and can exceed its timeout
    # while concurrent CAR imports are active. The selected repository's
    # configured peer ID must match the daemon reached through its API file.
    verify_daemon_identity(ipfs, env)
    root = receipt["recovery_root"]
    try:
        root_pin = run(ipfs, env, "pin", "ls", "--type=recursive", root, timeout=60).strip()
    except RecoveryError:
        root_pin = None
    if root_pin != root + " recursive":
        run(ipfs, env, "dag", "import", "--allow-big-block", str(args.car), timeout=900)
        if run(ipfs, env, "pin", "ls", "--type=recursive", root,
               timeout=60).strip() != root + " recursive":
            raise RecoveryError("recovery root is not recursively pinned")

    restored = args.car.with_name(args.car.name + "." + uuid.uuid4().hex + ".restored.partial")
    try:
        with restored.open("xb") as target:
            run(ipfs, env, "--offline", "cat", root, stdout=target, timeout=900)
            target.flush()
            os.fsync(target.fileno())
        size, digest = file_digest(restored, MAX_BLOCK)
        if size != receipt["original_bytes"] or digest != receipt["original_sha256"]:
            raise RecoveryError("restored bytes differ from original CID")
        cid = receipt["original_cid"]
        produced = run(ipfs, env, "block", "put", "--cid-codec=raw",
                       "--mhtype=sha2-256", "--allow-big-block", str(restored),
                       timeout=900).strip()
        if produced != cid:
            raise RecoveryError("Kubo rederived a different original CID")
        # type=all traverses indirect pins and can stall on Jacob's growing
        # CAR graph. Only an explicit pin on the original CID is sufficient.
        try:
            pin = run(ipfs, env, "pin", "ls", "--type=direct", cid, timeout=60).strip()
        except RecoveryError:
            pin = None
        if pin not in (cid + " direct", cid + " recursive"):
            run(ipfs, env, "pin", "add", "--recursive=false", cid, timeout=300)
        if run(ipfs, env, "pin", "ls", "--type=direct", cid,
               timeout=60).strip() != cid + " direct":
            raise RecoveryError("original CID is not durably pinned")
        block_stat = run(ipfs, env, "block", "stat", cid, timeout=60)
        if "Size: " + str(size) not in block_stat.splitlines():
            raise RecoveryError("Kubo original block size differs")
    finally:
        restored.unlink(missing_ok=True)
    result = {"status": "complete", "row": args.row,
              "inventory_sha256": args.sha256, "original_cid": receipt["original_cid"],
              "original_bytes": receipt["original_bytes"],
              "original_sha256": receipt["original_sha256"],
              "recovery_root": root, "car_sha256": receipt["car_sha256"],
              "repository": str(args.ipfs_path.resolve())}
    if args.output_receipt:
        args.output_receipt.parent.mkdir(parents=True, exist_ok=True)
        if args.output_receipt.exists():
            if json.loads(args.output_receipt.read_text()) != result:
                raise RecoveryError("existing import receipt differs")
        else:
            temp = args.output_receipt.with_name(args.output_receipt.name + "." +
                                                 uuid.uuid4().hex + ".partial")
            with temp.open("x") as output:
                json.dump(result, output, sort_keys=True)
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
            temp.replace(args.output_receipt)
            fd = os.open(args.output_receipt.parent, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--inventory", required=True, type=Path)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--count", required=True, type=int)
    parser.add_argument("--row", required=True, type=int)
    parser.add_argument("--car", required=True, type=Path)
    parser.add_argument("--source-receipt", required=True, type=Path)
    parser.add_argument("--ipfs-bin", required=True, type=Path)
    parser.add_argument("--ipfs-path", required=True, type=Path)
    parser.add_argument("--min-free-bytes", type=int, default=1_000_000_000_000)
    parser.add_argument("--output-receipt", type=Path)
    args = parser.parse_args()
    try:
        print(json.dumps(recover(args), sort_keys=True))
        return 0
    except (RecoveryError, InventoryError, OSError, ValueError, KeyError) as exc:
        print("REFUSED: " + str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
