#!/usr/bin/env python3
"""Make an importable UnixFS CAR for one oversized, CID-checked lake block.

A CAR frame containing a 200 MB raw block is legal as bytes but Kubo's CAR
importer refuses its section. This stores the *same bytes* as 256 KiB UnixFS
leaves, then proves a fresh offline repo can restore the original CID.
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

from raw_block_store import _sha256_digest
from serve_lake import Inventory, InventoryError


MIN_LARGE_BLOCK = 8_000_000
MAX_BLOCK = 256_000_000
MAX_CAR = 536_870_912


class ExportError(Exception):
    pass


def run(argv, env=None, stdout=None, timeout=300):
    try:
        result = subprocess.run(argv, env=env, stdout=stdout or subprocess.PIPE,
                                stderr=subprocess.PIPE, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ExportError("command failed to run: " + argv[0]) from exc
    if result.returncode:
        raise ExportError("command refused: " + argv[0] + " " +
                          result.stderr.decode("utf8", "replace")[:180].strip())
    return result.stdout


def digest_file(path, ceiling):
    digest = hashlib.sha256()
    total = 0
    with Path(path).open("rb") as source:
        while chunk := source.read(1024 * 1024):
            total += len(chunk)
            if total > ceiling:
                raise ExportError("file exceeds byte ceiling")
            digest.update(chunk)
    return total, digest.digest()


def checked_source_url(base, cid):
    parts = urlsplit(base)
    if (parts.scheme != "https" or not parts.hostname or
            parts.path != "/ipfs/" or parts.query or parts.fragment or
            parts.username or parts.password or
            not re.fullmatch(r"[A-Za-z0-9.-]+", parts.hostname)):
        raise ExportError("source must be an HTTPS /ipfs/ gateway base")
    return base + cid


def inventory_row(args):
    inventory = Inventory(args.inventory, args.sha256, args.count)
    if not 0 <= args.row < len(inventory.offsets):
        raise ExportError("row outside immutable inventory")
    with Path(args.inventory).open("rb") as source:
        source.seek(inventory.offsets[args.row])
        row = json.loads(source.readline())
    cid, size = row["cid"], row["bytes"]
    if not MIN_LARGE_BLOCK < size <= MAX_BLOCK:
        raise ExportError("row is outside oversized-block bounds")
    _sha256_digest(cid)
    return cid, size


def fresh_repo(ipfs, path):
    env = dict(os.environ, IPFS_PATH=str(path))
    run([ipfs, "init", "--profile=server"], env=env, timeout=120)
    return env


def restore_original(ipfs, car, root, original_cid, expected_size, expected_digest, directory):
    env = fresh_repo(ipfs, directory / "restore-repo")
    run([ipfs, "dag", "import", str(car)], env=env)
    restored = directory / "restored.block"
    with restored.open("xb") as target:
        run([ipfs, "--offline", "cat", root], env=env, stdout=target)
        target.flush()
        os.fsync(target.fileno())
    size, digest = digest_file(restored, MAX_BLOCK)
    if size != expected_size or digest != expected_digest:
        raise ExportError("offline UnixFS recovery differs from original CID bytes")
    if original_cid.startswith("Qm"):
        put = [ipfs, "block", "put", "--format=v0", "--allow-big-block", str(restored)]
    else:
        prefix = run([ipfs, "cid", "format", "-f", "%c %h %L", original_cid],
                     env=env).decode().strip().split()
        if len(prefix) != 3 or prefix[1] != "sha2-256" or prefix[2] != "32":
            raise ExportError("original CID prefix is unsupported")
        put = [ipfs, "block", "put", "--cid-codec=" + prefix[0],
               "--mhtype=sha2-256", "--allow-big-block", str(restored)]
    produced = run(put, env=env).decode().strip()
    if produced != original_cid:
        raise ExportError("rehydrated original CID differs")
    return size


def export(args):
    if not Path(args.ipfs_bin).is_file():
        raise ExportError("Kubo binary does not exist")
    original_cid, size = inventory_row(args)
    output = Path(args.output)
    receipt = Path(str(output) + ".json")
    if output.exists() or receipt.exists():
        raise ExportError("output or receipt already exists")
    url = checked_source_url(args.base_url, original_cid)
    with tempfile.TemporaryDirectory(prefix="yataverse-large-car-") as tmp:
        directory = Path(tmp)
        source = directory / "source.block"
        run(["curl", "-fsS", "--proto", "=https", "--max-redirs", "0",
             "--connect-timeout", "5", "--max-time", "300",
             "--max-filesize", str(size), "--output", str(source), url], timeout=310)
        actual_size, digest = digest_file(source, MAX_BLOCK)
        if actual_size != size or digest != _sha256_digest(original_cid):
            raise ExportError("source byte count or CID digest differs")
        env = fresh_repo(args.ipfs_bin, directory / "source-repo")
        root = run([args.ipfs_bin, "add", "--cid-version=1", "--raw-leaves",
                    "--chunker=size-262144", "--quiet", str(source)], env=env).decode().strip()
        if not re.fullmatch(r"b[a-z2-7]{20,200}", root):
            raise ExportError("UnixFS root CID is invalid")
        car = directory / "recovery.car"
        with car.open("xb") as target:
            run([args.ipfs_bin, "dag", "export", root], env=env, stdout=target)
            target.flush()
            os.fsync(target.fileno())
        car_bytes, car_digest = digest_file(car, MAX_CAR)
        restore_original(args.ipfs_bin, car, root, original_cid, size, digest, directory)
        record = {"schema": 1, "inventory_sha256": args.sha256,
                  "row": args.row, "original_cid": original_cid,
                  "original_bytes": size, "original_sha256": digest.hex(),
                  "recovery_root": root, "car_bytes": car_bytes,
                  "car_sha256": car_digest.hex(),
                  "validation": "fresh-offline-kubo-import-cat-and-original-cid-rehydration"}
        with tempfile.NamedTemporaryFile(dir=output.parent, prefix=output.name + ".",
                                         suffix=".partial", delete=False) as temp:
            pending_car = Path(temp.name)
            with car.open("rb") as source_file:
                while chunk := source_file.read(1024 * 1024):
                    temp.write(chunk)
            temp.flush()
            os.fsync(temp.fileno())
        pending_receipt = Path(str(pending_car) + ".json")
        published_car = False
        published_receipt = False
        try:
            with pending_receipt.open("x") as target:
                json.dump(record, target, sort_keys=True)
                target.write("\n")
                target.flush()
                os.fsync(target.fileno())
            os.link(pending_car, output)
            published_car = True
            os.link(pending_receipt, receipt)
            published_receipt = True
            dir_fd = os.open(output.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except BaseException:
            if published_receipt:
                receipt.unlink(missing_ok=True)
            if published_car:
                output.unlink(missing_ok=True)
            raise
        finally:
            pending_car.unlink(missing_ok=True)
            pending_receipt.unlink(missing_ok=True)
        print(json.dumps(record, sort_keys=True))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--inventory", required=True)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--count", required=True, type=int)
    parser.add_argument("--row", required=True, type=int)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--ipfs-bin", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    try:
        export(args)
    except (ExportError, InventoryError, OSError, ValueError) as exc:
        print("REFUSED: " + str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
