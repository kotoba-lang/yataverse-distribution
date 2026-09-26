#!/usr/bin/env python3
"""Read-only Yataverse lake listing and pinned raw blocks from one local node."""

import argparse
import hashlib
import json
import re
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit


CID = re.compile(r"^[a-zA-Z0-9]{46,100}$")
PAGE_SIZE = 200


class InventoryError(Exception):
    pass


class Inventory:
    def __init__(self, path, expected_sha256, expected_count):
        self.path = Path(path)
        self.offsets = []
        self.sizes = {}
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
                self.offsets.append(offset)
                self.sizes[cid] = size
                total += size
        if digest.hexdigest() != expected_sha256 or len(self.offsets) != expected_count:
            raise InventoryError("inventory digest or row count differs from declared snapshot")
        self.total_bytes = total

    def page(self, offset):
        if offset < 0 or offset >= len(self.offsets):
            raise InventoryError("cursor outside inventory")
        end = min(offset + PAGE_SIZE, len(self.offsets))
        rows = []
        with self.path.open("rb") as source:
            source.seek(self.offsets[offset])
            for _ in range(offset, end):
                row = json.loads(source.readline())
                rows.append({"cid": row["cid"], "size": row["bytes"]})
        return {"ok": True, "blocks": rows,
                "cursor": str(end) if end < len(self.offsets) else None,
                "truncated?": end < len(self.offsets)}


class LocalBlocks:
    def __init__(self, ipfs_bin, ipfs_path, inventory, max_block_bytes):
        self.ipfs_bin = ipfs_bin
        self.ipfs_path = ipfs_path
        self.inventory = inventory
        self.max_block_bytes = max_block_bytes

    def read(self, cid):
        size = self.inventory.sizes.get(cid)
        if size is None:
            raise InventoryError("CID absent from inventory")
        if size > self.max_block_bytes:
            raise InventoryError("block exceeds API byte limit")
        import os
        env = dict(os.environ, IPFS_PATH=self.ipfs_path)
        pin = subprocess.run([self.ipfs_bin, "pin", "ls", "--type=all", cid],
                             capture_output=True, env=env, timeout=15)
        if pin.returncode or pin.stdout.strip() not in (
                (cid + " direct").encode(), (cid + " recursive").encode()):
            raise InventoryError("block is not directly or recursively pinned")
        block = subprocess.run([self.ipfs_bin, "--offline", "block", "get", cid],
                               capture_output=True, env=env, timeout=30)
        if block.returncode or len(block.stdout) != size:
            raise InventoryError("local block unavailable or size differs")
        return block.stdout


def handler_for(inventory, blocks):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urlsplit(self.path)
            try:
                if parsed.path == "/health":
                    payload = json.dumps({"ok": True, "rows": len(inventory.offsets),
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
                    payload = blocks.read(cid)
                    content_type = "application/octet-stream"
                else:
                    self.send_error(404)
                    return
            except (InventoryError, subprocess.TimeoutExpired) as exc:
                self.send_error(404, str(exc))
                return
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "public, max-age=60")
            self.end_headers()
            self.wfile.write(payload)

    return Handler


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--inventory", required=True)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--count", required=True, type=int)
    parser.add_argument("--ipfs-bin", required=True)
    parser.add_argument("--ipfs-path", required=True)
    parser.add_argument("--max-block-bytes", type=int, default=8_000_000)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8090)
    args = parser.parse_args()
    inventory = Inventory(args.inventory, args.sha256, args.count)
    blocks = LocalBlocks(args.ipfs_bin, args.ipfs_path, inventory, args.max_block_bytes)
    ThreadingHTTPServer((args.host, args.port), handler_for(inventory, blocks)).serve_forever()


if __name__ == "__main__":
    main()
