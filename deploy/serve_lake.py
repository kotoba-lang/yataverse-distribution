#!/usr/bin/env python3
"""Read-only Yataverse lake listing and CID-checked local blocks."""

import argparse
import bisect
import hashlib
import json
import re
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from raw_block_store import RawBlockStore, RawBlockStoreError, verify_cid


CID = re.compile(r"^[a-zA-Z0-9]{46,100}$")
PAGE_SIZE = 200
LARGE_BLOCK_THRESHOLD = 8_000_000
MAX_BLOCK_BYTES = 256_000_000


class InventoryError(Exception):
    pass


class Inventory:
    def __init__(self, path, expected_sha256, expected_count):
        self.path = Path(path)
        self.offsets = []
        self.sizes = {}
        self.positions = {}
        digest = hashlib.sha256()
        total = 0
        with self.path.open("rb") as source:
            while True:
                offset = source.tell()
                line = source.readline()
                if not line:
                    break
                digest.update(line)
                try:
                    row = json.loads(line)
                    cid, size = row["cid"], row["bytes"]
                except (ValueError, KeyError, TypeError) as exc:
                    raise InventoryError("invalid inventory row at offset {}".format(offset)) from exc
                if not isinstance(cid, str) or not CID.fullmatch(cid) or type(size) is not int or size <= 0:
                    raise InventoryError("invalid CID or size at offset {}".format(offset))
                if cid in self.sizes:
                    raise InventoryError("duplicate CID in inventory: " + cid)
                self.positions[cid] = len(self.offsets)
                self.offsets.append(offset)
                self.sizes[cid] = size
                total += size
        if digest.hexdigest() != expected_sha256 or len(self.offsets) != expected_count:
            raise InventoryError("inventory digest or row count differs from declared snapshot")
        self.sha256 = expected_sha256
        self.total_bytes = total
        self.file_identity = self.path.stat()

    def rows(self, offset, limit):
        current = self.path.stat()
        if (current.st_ino, current.st_size, current.st_mtime_ns) != (
                self.file_identity.st_ino, self.file_identity.st_size,
                self.file_identity.st_mtime_ns):
            raise InventoryError("inventory file changed since startup")
        if offset < 0 or offset >= len(self.offsets):
            raise InventoryError("cursor outside inventory")
        if not 1 <= limit <= 1000:
            raise InventoryError("inventory row limit must be within 1..1000")
        end = min(offset + limit, len(self.offsets))
        rows = []
        with self.path.open("rb") as source:
            source.seek(self.offsets[offset])
            for _ in range(offset, end):
                row = json.loads(source.readline())
                rows.append({"cid": row["cid"], "size": row["bytes"]})
        return rows, end

    def page(self, offset):
        rows, end = self.rows(offset, PAGE_SIZE)
        return {"ok": True, "inventory-sha256": self.sha256, "blocks": rows,
                "cursor": str(end) if end < len(self.offsets) else None,
                "truncated?": end < len(self.offsets)}


class CarReceipts:
    """Exact inventory ranges proven by completed CAR or native import receipts.

    Native receipts (row-*-native.json) are the rows audit_lake.py counts as
    covered through a bitswap-mirrored root. Without them every such row fell
    through to `ipfs pin ls --type=all`, which walks all recursive pins and
    outlives the 15 s budget on a large repository — so the reader answered
    503 for blocks the audit reported as held.
    """

    def __init__(self, directory, inventory, ipfs_path, native_directories=()):
        self.directory = None if directory is None else Path(directory)
        self.native_directories = [Path(item) for item in native_directories]
        if self.directory is None and not self.native_directories:
            raise InventoryError("no receipt directory given")
        self.inventory = inventory
        self.ipfs_path = Path(ipfs_path).resolve()
        self.lock = threading.RLock()
        self.mtime_ns = None
        self.starts = []
        self.ranges = []
        self.refresh()

    def _native_range(self, path):
        match = re.fullmatch(r"row-([0-9]+)-([0-9]+)-native\.json", path.name)
        if not match:
            raise InventoryError("invalid native receipt name")
        try:
            record = json.loads(path.read_text())
        except (OSError, ValueError) as exc:
            raise InventoryError("native receipt unreadable") from exc
        if not isinstance(record, dict):
            raise InventoryError("native receipt must be an object")
        start, last = map(int, match.groups())
        end = last + 1
        if not 0 <= start < end <= len(self.inventory.offsets) or end - start > 1000:
            raise InventoryError("native receipt range is invalid")
        rows, actual_end = self.inventory.rows(start, end - start)
        root = record.get("root")
        if (actual_end != end or record.get("status") != "complete" or
                record.get("inventory_sha256") != self.inventory.sha256 or
                record.get("start_row") != start or record.get("end_row") != end or
                record.get("blocks") != len(rows) or
                record.get("bytes") != sum(row["size"] for row in rows) or
                record.get("repository") != str(self.ipfs_path) or
                not isinstance(root, str) or not CID.fullmatch(root) or
                not isinstance(record.get("source_peer"), str) or
                not record["source_peer"]):
            raise InventoryError("native receipt differs from inventory or repository: " + path.name)
        return start, end

    def refresh(self):
        directories = ([] if self.directory is None else [self.directory]) + self.native_directories
        for directory in directories:
            if not directory.is_dir():
                raise InventoryError("receipt directory is absent: " + str(directory))
        mtime = tuple(directory.stat().st_mtime_ns for directory in directories)
        if mtime == self.mtime_ns:
            return
        ranges = []
        for directory in self.native_directories:
            for path in directory.glob("row-*-native.json"):
                ranges.append(self._native_range(path))
        car_paths = [] if self.directory is None else self.directory.glob("row-*-import.json")
        for path in car_paths:
            match = re.fullmatch(r"row-([0-9]+)-([0-9]+)-import\.json", path.name)
            if not match:
                raise InventoryError("invalid CAR receipt name")
            try:
                record = json.loads(path.read_text())
            except (OSError, ValueError) as exc:
                raise InventoryError("CAR receipt unreadable") from exc
            if not isinstance(record, dict):
                raise InventoryError("CAR receipt must be an object")
            start, last = map(int, match.groups())
            end = last + 1
            root = record.get("root")
            if (record.get("status") != "complete" or
                    record.get("inventory_sha256") != self.inventory.sha256 or
                    record.get("start_row") != start or record.get("end_row") != end or
                    record.get("blocks") != end - start or
                    not 0 <= start < end <= len(self.inventory.offsets) or
                    not isinstance(root, str) or not CID.fullmatch(root) or
                    not isinstance(record.get("car_sha256"), str) or
                    not re.fullmatch(r"[0-9a-f]{64}", record["car_sha256"]) or
                    record.get("repository") != str(self.ipfs_path)):
                raise InventoryError("CAR receipt differs from inventory or repository")
            ranges.append((start, end))
        ranges.sort()
        covered = []
        for start, end in ranges:
            if covered and start <= covered[-1][1]:
                covered[-1] = (covered[-1][0], max(covered[-1][1], end))
            else:
                covered.append((start, end))
        self.starts = [item[0] for item in covered]
        self.ranges = covered
        self.mtime_ns = mtime

    def covers(self, cid):
        with self.lock:
            self.refresh()
            row = self.inventory.positions[cid]
            index = bisect.bisect_right(self.starts, row) - 1
            return index >= 0 and row < self.ranges[index][1]


class LocalBlocks:
    def __init__(self, ipfs_bin, ipfs_path, inventory, max_block_bytes,
                 raw_store=None, car_receipts=None):
        if not 1 <= max_block_bytes <= MAX_BLOCK_BYTES:
            raise InventoryError("block byte limit must be within 1..256000000")
        self.ipfs_bin = ipfs_bin
        self.ipfs_path = ipfs_path
        self.inventory = inventory
        self.max_block_bytes = max_block_bytes
        self.raw_store = raw_store
        self.car_receipts = car_receipts

    def read(self, cid):
        size = self.inventory.sizes.get(cid)
        if size is None:
            raise InventoryError("CID absent from inventory")
        if size > self.max_block_bytes:
            raise InventoryError("block exceeds API byte limit")
        if self.raw_store is not None:
            try:
                data = self.raw_store.read(cid, max_bytes=self.max_block_bytes)
            except RawBlockStoreError as exc:
                raise InventoryError("raw block store refused " + cid) from exc
            if data is not None:
                if len(data) != size:
                    raise InventoryError("raw block size differs from inventory")
                return data
        import os
        env = dict(os.environ, IPFS_PATH=self.ipfs_path)
        if self.car_receipts is None or not self.car_receipts.covers(cid):
            direct = subprocess.run([self.ipfs_bin, "pin", "ls", "--type=direct", cid],
                                    capture_output=True, env=env, timeout=15)
            durable = (direct.returncode == 0 and
                       direct.stdout.decode("utf-8", "replace").strip() == cid + " direct")
            if not durable:
                pin = subprocess.run([self.ipfs_bin, "pin", "ls", "--type=all", cid],
                                     capture_output=True, env=env, timeout=15)
                pin_line = pin.stdout.decode("utf-8", "replace").strip()
                durable = (pin.returncode == 0 and
                           (pin_line == cid + " recursive" or
                            bool(re.fullmatch(re.escape(cid) +
                                              r" indirect through (?:Qm[1-9A-HJ-NP-Za-km-z]{44}|b[a-z2-7]{20,200})",
                                              pin_line))))
            if not durable:
                raise InventoryError("block is not durably pinned or receipted")
        block = subprocess.run([self.ipfs_bin, "--offline", "block", "get", cid],
                               capture_output=True, env=env, timeout=30)
        if block.returncode or len(block.stdout) != size:
            raise InventoryError("local block unavailable or size differs")
        try:
            verify_cid(cid, block.stdout)
        except RawBlockStoreError as exc:
            raise InventoryError("local block CID differs from bytes") from exc
        return block.stdout


class EpochInventory:
    """The lake as its log describes it: epoch inventories in order.

    Each epoch keeps its own Inventory, receipts and LocalBlocks, because every
    receipt is bound to the sha256 of the inventory it was made against; one
    concatenated file would make each of them look foreign. The composite's
    sha256 is that of the concatenated epochs, which is what
    `lake_head.cljk resolve` writes from the inga log, so a reader can show the
    same digest the consensus log resolves to (ADR-2610062000 P3)."""

    def __init__(self, parts, expected_sha256, expected_count):
        if not parts:
            raise InventoryError("an epoch list needs at least one inventory")
        digest = hashlib.sha256()
        for part in parts:
            with part.path.open("rb") as source:
                for chunk in iter(lambda: source.read(1 << 20), b""):
                    digest.update(chunk)
        total = sum(len(part.offsets) for part in parts)
        if digest.hexdigest() != expected_sha256 or total != expected_count:
            raise InventoryError("epoch inventories differ from the declared lake log")
        self.parts = parts
        self.starts = []
        start = 0
        for part in parts:
            self.starts.append(start)
            start += len(part.offsets)
        self.sizes = {}
        self.owner = {}
        for index, part in enumerate(parts):
            for cid, size in part.sizes.items():
                if cid in self.sizes:
                    raise InventoryError("CID appears in two epochs: " + cid)
                self.sizes[cid] = size
                self.owner[cid] = index
        self.offsets = range(total)
        self.sha256 = expected_sha256
        self.total_bytes = sum(part.total_bytes for part in parts)

    def rows(self, offset, limit):
        if offset < 0 or offset >= len(self.offsets):
            raise InventoryError("cursor outside inventory")
        if not 1 <= limit <= 1000:
            raise InventoryError("inventory row limit must be within 1..1000")
        index = bisect.bisect_right(self.starts, offset) - 1
        local = offset - self.starts[index]
        rows, end = self.parts[index].rows(local, limit)
        return rows, self.starts[index] + end

    def page(self, offset):
        rows, end = self.rows(offset, PAGE_SIZE)
        return {"ok": True, "inventory-sha256": self.sha256, "blocks": rows,
                "cursor": str(end) if end < len(self.offsets) else None,
                "truncated?": end < len(self.offsets)}


class EpochBlocks:
    """Read a block through the epoch that lists it."""

    def __init__(self, inventory, epoch_blocks):
        if len(epoch_blocks) != len(inventory.parts):
            raise InventoryError("one block reader per epoch")
        self.inventory = inventory
        self.epoch_blocks = epoch_blocks

    def read(self, cid):
        index = self.inventory.owner.get(cid)
        if index is None:
            raise InventoryError("CID absent from inventory")
        return self.epoch_blocks[index].read(cid)


def handler_for(inventory, blocks, max_concurrent_requests=8,
                large_client_timeout=120):
    if not 1 <= max_concurrent_requests <= 64:
        raise InventoryError("concurrent request limit must be within 1..64")
    if not 120 <= large_client_timeout <= 1800:
        raise InventoryError("large client timeout must be within 120..1800 seconds")
    request_slots = threading.BoundedSemaphore(max_concurrent_requests)
    large_block_slot = threading.BoundedSemaphore(1)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if not request_slots.acquire(blocking=False):
                self.send_error(503, "reader busy")
                return
            parsed = urlsplit(self.path)
            large_block_acquired = False
            try:
                if parsed.path == "/health":
                    payload = json.dumps({"ok": True, "inventory-sha256": inventory.sha256,
                                          "rows": len(inventory.offsets),
                                          "bytes": inventory.total_bytes}).encode()
                    content_type = "application/json"
                elif parsed.path == "/api/v1/lake/blocks":
                    query = parse_qs(parsed.query, keep_blank_values=True)
                    if set(query) - {"cursor"} or len(query.get("cursor", ["0"])) != 1:
                        raise InventoryError("invalid query")
                    cursor = query.get("cursor", ["0"])[0]
                    if not re.fullmatch(r"0|[1-9][0-9]*", cursor):
                        raise InventoryError("invalid cursor")
                    payload = json.dumps(inventory.page(int(cursor)), separators=(",", ":")).encode()
                    content_type = "application/json"
                elif parsed.path.startswith("/ipfs/") and not parsed.query:
                    cid = parsed.path[len("/ipfs/"):]
                    if not CID.fullmatch(cid):
                        raise InventoryError("invalid CID")
                    if inventory.sizes.get(cid, 0) > LARGE_BLOCK_THRESHOLD:
                        if not large_block_slot.acquire(blocking=False):
                            self.send_error(503, "large block reader busy")
                            return
                        large_block_acquired = True
                        self.connection.settimeout(large_client_timeout)
                    payload = blocks.read(cid)
                    content_type = "application/octet-stream"
                else:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Cache-Control", "public, max-age=60")
                self.end_headers()
                self.wfile.write(payload)
            except subprocess.TimeoutExpired:
                self.send_error(503, "local IPFS temporarily busy")
            except InventoryError as exc:
                self.send_error(404, str(exc))
            finally:
                if large_block_acquired:
                    large_block_slot.release()
                request_slots.release()

    return Handler


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--inventory")
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--count", required=True, type=int)
    parser.add_argument("--ipfs-bin", required=True)
    parser.add_argument("--ipfs-path", required=True)
    parser.add_argument("--raw-block-store", type=Path)
    parser.add_argument("--car-receipts", type=Path)
    parser.add_argument("--native-receipts", type=Path, action="append", default=[])
    parser.add_argument("--max-block-bytes", type=int, default=LARGE_BLOCK_THRESHOLD)
    parser.add_argument("--max-concurrent-requests", type=int, default=8)
    parser.add_argument("--large-client-timeout", type=int, default=120)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8090)
    parser.add_argument("--epoch", action="append", default=[],
                        help="INVENTORY:SHA256:COUNT[:CAR_RECEIPTS[:NATIVE_RECEIPTS...]] in log order; "
                             "with --epoch, --inventory/--sha256/--count describe the whole log")
    args = parser.parse_args()
    if args.epoch:
        parts, readers = [], []
        for spec in args.epoch:
            fields = spec.split(":")
            if len(fields) < 3:
                raise SystemExit("--epoch needs INVENTORY:SHA256:COUNT")
            part = Inventory(fields[0], fields[1], int(fields[2]))
            car = Path(fields[3]) if len(fields) > 3 and fields[3] else None
            natives = [Path(f) for f in fields[4:] if f]
            receipts = (CarReceipts(car, part, args.ipfs_path, native_directories=natives)
                        if car or natives else None)
            parts.append(part)
            readers.append(LocalBlocks(args.ipfs_bin, args.ipfs_path, part, args.max_block_bytes,
                                       raw_store=RawBlockStore(args.raw_block_store) if args.raw_block_store else None,
                                       car_receipts=receipts))
        inventory = EpochInventory(parts, args.sha256, args.count)
        blocks = EpochBlocks(inventory, readers)
        ThreadingHTTPServer((args.host, args.port),
                            handler_for(inventory, blocks, args.max_concurrent_requests,
                                        args.large_client_timeout)).serve_forever()
        return
    inventory = Inventory(args.inventory, args.sha256, args.count)
    raw_store = RawBlockStore(args.raw_block_store) if args.raw_block_store else None
    car_receipts = (CarReceipts(args.car_receipts, inventory, args.ipfs_path,
                                native_directories=args.native_receipts)
                    if args.car_receipts or args.native_receipts else None)
    blocks = LocalBlocks(args.ipfs_bin, args.ipfs_path, inventory,
                         args.max_block_bytes, raw_store=raw_store,
                         car_receipts=car_receipts)
    ThreadingHTTPServer((args.host, args.port),
                        handler_for(inventory, blocks, args.max_concurrent_requests,
                                    args.large_client_timeout)).serve_forever()


if __name__ == "__main__":
    main()
