#!/usr/bin/env python3
"""Pin one dated lake inventory range using only a public libp2p peer."""

import argparse
import concurrent.futures
import fcntl
import hashlib
import ipaddress
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

from export_lake_car_from_kubo import ExportError, cid_bytes, selected_rows
from import_lake_car import ImportError as LakeImportError
from import_lake_car import kubo, verify_repo, verified_root


PUBLIC_PEER = re.compile(r"/ip4/([0-9.]+)/.+/p2p/([A-Za-z0-9]+)")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")


class MirrorError(Exception):
    pass


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".partial")
    with temp.open("x") as output:
        json.dump(value, output, sort_keys=True)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    temp.replace(path)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def checked_public_peer(binary, env, address):
    match = PUBLIC_PEER.fullmatch(address)
    if not match:
        raise MirrorError("source must be an explicit public IPv4 peer multiaddress")
    ip, peer = match.groups()
    if not ipaddress.ip_address(ip).is_global:
        raise MirrorError("source is not a public IPv4 address")
    kubo(binary, env, "swarm", "connect", address, timeout=30)
    routes = kubo(binary, env, "swarm", "peers", "-v", timeout=30).splitlines()
    source_routes = [line.split()[0] for line in routes if line.split() and
                     line.split()[0].endswith("/p2p/" + peer)]
    if not any(route.startswith("/ip4/" + ip + "/") for route in source_routes):
        raise MirrorError("source peer is not connected over its public IPv4 route")
    if any(not ipaddress.ip_address(route.split("/")[2]).is_global
           for route in source_routes if route.startswith("/ip4/")):
        raise MirrorError("source peer also has a private route")
    return peer


def fetch_block(binary, env, cid, size):
    try:
        result = subprocess.run([binary, "block", "get", cid], env=env,
                                capture_output=True, timeout=60, check=True)
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise MirrorError("IPFS block fetch failed: " + cid) from exc
    _binary_cid, expected = cid_bytes(cid)
    if len(result.stdout) != size or hashlib.sha256(result.stdout).digest() != expected:
        raise MirrorError("IPFS block differs from dated inventory: " + cid)
    return size


def pinned_root(binary, env, root_record):
    data = json.dumps(root_record, separators=(",", ":")).encode()
    root = kubo(binary, env, "dag", "put", "--pin", data=data, timeout=600).strip()
    if kubo(binary, env, "pin", "ls", "--type=recursive", root,
            timeout=60).strip() != root + " recursive":
        raise MirrorError("batch root is not recursively pinned")
    verified_root(kubo(binary, env, "dag", "get", root, timeout=60), root_record)
    return root


def mirror_once(args):
    if (not SHA256.fullmatch(args.sha256) or args.count < 1 or
            not 0 <= args.start_row < args.end_row <= args.count or
            not 1 <= args.max_blocks <= 1000 or
            not 1 <= args.max_bytes <= 536_870_912 or
            not 1 <= args.workers <= 16 or args.min_free_bytes < 0):
        raise MirrorError("invalid inventory range or resource bounds")
    if not args.ipfs_bin.is_file() or not args.ipfs_path.is_dir():
        raise MirrorError("Kubo binary or repository is absent")
    args.state_dir.mkdir(parents=True, exist_ok=True)
    with (args.state_dir / "mirror.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise MirrorError("native mirror already running") from exc
        checkpoint = args.state_dir / "checkpoint.json"
        state = json.loads(checkpoint.read_text()) if checkpoint.exists() else {
            "cursor": args.start_row, "inventory_sha256": args.sha256}
        cursor = state.get("cursor")
        if (state.get("inventory_sha256") != args.sha256 or
                type(cursor) is not int or
                not args.start_row <= cursor <= args.end_row):
            raise MirrorError("native mirror checkpoint identity differs")
        if cursor == args.end_row:
            return {"status": "complete", "cursor": cursor}
        rows = selected_rows(args.inventory, args.sha256, args.count, cursor,
                             min(args.max_blocks, args.end_row - cursor),
                             args.max_bytes)
        end = cursor + len(rows)
        receipt_path = args.state_dir / ("row-{}-{}-native.json".format(cursor, end - 1))
        env = verify_repo(str(args.ipfs_bin), args.ipfs_path)
        peer = checked_public_peer(str(args.ipfs_bin), env, args.source_peer)
        root_record = {"schema": 1, "inventory-sha256": args.sha256,
                       "start-row": cursor, "end-row": end,
                       "links": [{"/": cid} for cid, _size in rows]}
        if receipt_path.exists():
            saved = json.loads(receipt_path.read_text())
            if (saved.get("status") != "complete" or saved.get("start_row") != cursor or
                    saved.get("end_row") != end or
                    saved.get("inventory_sha256") != args.sha256 or
                    saved.get("source_peer") != peer or
                    saved.get("blocks") != len(rows) or
                    saved.get("bytes") != sum(size for _cid, size in rows)):
                raise MirrorError("native mirror receipt differs from inventory")
            root = saved.get("root")
            if not isinstance(root, str) or not root:
                raise MirrorError("native mirror receipt has no root")
            if kubo(str(args.ipfs_bin), env, "pin", "ls", "--type=recursive", root,
                    timeout=60).strip() != root + " recursive":
                raise MirrorError("native mirror receipt root is no longer pinned")
            verified_root(kubo(str(args.ipfs_bin), env, "dag", "get", root,
                               timeout=60), root_record)
        else:
            total = sum(size for _cid, size in rows)
            if shutil.disk_usage(args.ipfs_path).free < total * 2 + args.min_free_bytes:
                raise MirrorError("physical disk reserve would be crossed")
            with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
                futures = [pool.submit(fetch_block, str(args.ipfs_bin), env, cid, size)
                           for cid, size in rows]
                for future in futures:
                    future.result()
            root = pinned_root(str(args.ipfs_bin), env, root_record)
            saved = {"status": "complete", "inventory_sha256": args.sha256,
                     "start_row": cursor, "end_row": end, "blocks": len(rows),
                     "bytes": total, "root": root, "source_peer": peer,
                     "repository": str(args.ipfs_path.resolve())}
            atomic_json(receipt_path, saved)
        atomic_json(checkpoint, {"cursor": end, "inventory_sha256": args.sha256,
                                 "last_root": root})
        return {"status": "mirrored", "start_row": cursor, "end_row": end,
                "blocks": len(rows), "root": root}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--count", type=int, required=True)
    parser.add_argument("--start-row", type=int, required=True)
    parser.add_argument("--end-row", type=int, required=True)
    parser.add_argument("--max-blocks", type=int, default=200)
    parser.add_argument("--max-bytes", type=int, default=100_000_000)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--source-peer", required=True)
    parser.add_argument("--ipfs-bin", type=Path, required=True)
    parser.add_argument("--ipfs-path", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--min-free-bytes", type=int, default=1_000_000_000_000)
    args = parser.parse_args()
    try:
        print(json.dumps(mirror_once(args), sort_keys=True))
        return 0
    except (MirrorError, ExportError, LakeImportError, OSError, ValueError,
            KeyError, IndexError) as exc:
        print("REFUSED: " + str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
