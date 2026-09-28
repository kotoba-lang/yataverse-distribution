#!/usr/bin/env python3
"""Import a bounded lake CAR and pin its inventory CIDs through one DAG root."""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from serve_lake import Inventory, InventoryError


CID = re.compile(r"(?:Qm[1-9A-HJ-NP-Za-km-z]{44}|b[a-z2-7]{20,200})\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")


class ImportError(Exception):
    pass


def digest_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def kubo(binary, env, *args, data=None, timeout=300):
    try:
        result = subprocess.run([binary, *args], input=data, capture_output=True,
                                env=env, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ImportError("Kubo command failed: " + " ".join(args[:2])) from exc
    if result.returncode:
        raise ImportError("Kubo refused " + " ".join(args[:2]) + ": " +
                          result.stderr.decode("utf-8", "replace")[:300].strip())
    return result.stdout.decode("utf-8", "replace")


def verified_root(output, expected):
    try:
        actual = json.loads(output)
    except ValueError as exc:
        raise ImportError("Kubo returned invalid batch root JSON") from exc
    if actual != expected:
        raise ImportError("Kubo batch root differs from inventory links")


def import_batch(args):
    if not SHA256.fullmatch(args.sha256) or not SHA256.fullmatch(args.car_sha256):
        raise ImportError("invalid SHA-256 argument")
    if (not 1 <= args.max_blocks <= 1000 or args.start_row < 0 or args.count < 1 or
            args.min_free_bytes < 0):
        raise ImportError("invalid inventory range")
    if not args.ipfs_bin.is_file() or not args.ipfs_path.is_dir() or not args.car.is_file():
        raise ImportError("Kubo binary, repository, or CAR is absent")
    inventory = Inventory(args.inventory, args.sha256, args.count)
    rows, _end = inventory.rows(args.start_row, args.max_blocks)
    cids = [row["cid"] for row in rows]
    if not cids or any(not CID.fullmatch(cid) for cid in cids):
        raise ImportError("invalid selected CID")
    car_size = args.car.stat().st_size
    if car_size < 1 or digest_file(args.car) != args.car_sha256:
        raise ImportError("CAR SHA-256 differs from export receipt")
    if shutil.disk_usage(args.ipfs_path).free - 2 * car_size < args.min_free_bytes:
        raise ImportError("physical disk reserve would be crossed")
    env = dict(os.environ, IPFS_PATH=str(args.ipfs_path.resolve()))
    repo = kubo(str(args.ipfs_bin), env, "repo", "stat", timeout=30)
    repo_path = next((line.split(":", 1)[1].strip() for line in repo.splitlines()
                      if line.startswith("RepoPath:")), None)
    if repo_path is None or Path(repo_path).resolve() != args.ipfs_path.resolve():
        raise ImportError("Kubo daemon uses a different repository")

    # Kubo's CAR roots use CIDv1. The dated inventory contains CIDv0 aliases,
    # so pin the original inventory links under one durable root instead.
    kubo(str(args.ipfs_bin), env, "dag", "import", "--allow-big-block",
         "--pin-roots=false", "--fast-provide-root=false",
         "--fast-provide-dag=false", str(args.car), timeout=600)
    root_record = {"schema": 1, "inventory-sha256": args.sha256,
                   "start-row": args.start_row,
                   "end-row": args.start_row + len(cids),
                   "links": [{"/": cid} for cid in cids]}
    root_data = json.dumps(root_record,
                           separators=(",", ":")).encode()
    root = kubo(str(args.ipfs_bin), env, "dag", "put", "--pin",
                data=root_data, timeout=600).strip()
    if not CID.fullmatch(root):
        raise ImportError("Kubo returned an invalid batch root")
    if kubo(str(args.ipfs_bin), env, "pin", "ls", "--type=recursive", root,
            timeout=60).strip() != root + " recursive":
        raise ImportError("batch root is not recursively pinned")
    verified_root(kubo(str(args.ipfs_bin), env, "dag", "get", root, timeout=60),
                  root_record)
    result = {"status": "complete", "inventory_sha256": args.sha256,
              "start_row": args.start_row, "end_row": args.start_row + len(cids),
              "blocks": len(cids), "bytes": sum(row["size"] for row in rows),
              "car_sha256": args.car_sha256, "root": root,
              "repository": str(args.ipfs_path.resolve())}
    if args.receipt:
        args.receipt.parent.mkdir(parents=True, exist_ok=True)
        temp = args.receipt.with_name(args.receipt.name + ".partial")
        with temp.open("x") as destination:
            json.dump(result, destination, sort_keys=True)
            destination.write("\n")
            destination.flush()
            os.fsync(destination.fileno())
        temp.replace(args.receipt)
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--inventory", required=True, type=Path)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--count", required=True, type=int)
    parser.add_argument("--start-row", required=True, type=int)
    parser.add_argument("--max-blocks", required=True, type=int)
    parser.add_argument("--car", required=True, type=Path)
    parser.add_argument("--car-sha256", required=True)
    parser.add_argument("--ipfs-bin", required=True, type=Path)
    parser.add_argument("--ipfs-path", required=True, type=Path)
    parser.add_argument("--min-free-bytes", type=int, default=1_000_000_000_000)
    parser.add_argument("--receipt", type=Path)
    args = parser.parse_args()
    try:
        print(json.dumps(import_batch(args), sort_keys=True))
        return 0
    except (ImportError, InventoryError, OSError, ValueError) as exc:
        print("REFUSED: " + str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
