#!/usr/bin/env python3
"""Resume an offline, CID-checked readback of one dated lake inventory.

This measures block bytes at scan time. Durable pin coverage is a separate
question answered by audit_lake.py; a scan during transfer is a lower bound.
"""

import argparse
import fcntl
import hashlib
import http.client
import json
import os
import re
import subprocess
import sys
import uuid
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from raw_block_store import RawBlockStore, RawBlockStoreError, verify_cid
from serve_lake import Inventory, InventoryError


SHA256 = re.compile(r"[0-9a-f]{64}\Z")
MAX_BLOCK = 256_000_000


class ReadbackError(Exception):
    pass


def verify_repository(ipfs_bin, ipfs_path, rpc_url):
    repo = ipfs_path.resolve()
    parsed = urlsplit(rpc_url)
    if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or
            not parsed.port or parsed.path not in ("", "/") or parsed.query or
            parsed.fragment or parsed.username or parsed.password):
        raise ReadbackError("Kubo RPC must be uncredentialed loopback HTTP")
    try:
        config = json.loads((repo / "config").read_text())
        peer = config["Identity"]["PeerID"]
        api = (repo / "api").read_text().strip()
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ReadbackError("Kubo repository identity is unavailable") from exc
    if (not isinstance(peer, str) or not peer or
            api != "/ip4/127.0.0.1/tcp/" + str(parsed.port)):
        raise ReadbackError("Kubo repository API or identity differs")
    env = dict(os.environ, IPFS_PATH=str(repo))
    try:
        result = subprocess.run([str(ipfs_bin), "id", "-f", "<id>"],
                                capture_output=True, timeout=10, env=env)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ReadbackError("Kubo daemon identity check failed") from exc
    if result.returncode or result.stdout.decode("utf-8", "replace").strip() != peer:
        raise ReadbackError("Kubo daemon uses a different repository")
    return peer, parsed.port


class OfflineBlocks:
    def __init__(self, port, raw_store):
        self.connection = http.client.HTTPConnection("127.0.0.1", port, timeout=120)
        self.raw_store = raw_store

    def close(self):
        self.connection.close()

    def read(self, cid, size):
        if not 1 <= size <= MAX_BLOCK:
            raise ReadbackError("inventory block exceeds readback limit")
        try:
            raw = self.raw_store.read(cid, max_bytes=MAX_BLOCK)
        except RawBlockStoreError as exc:
            raise ReadbackError("raw block store refused " + cid) from exc
        if raw is not None:
            return raw
        path = "/api/v0/block/get?" + urlencode({"arg": cid, "offline": "true"})
        try:
            self.connection.request("POST", path)
            response = self.connection.getresponse()
            body = response.read(size + 1 if response.status == 200 else 4096)
        except (OSError, http.client.HTTPException) as exc:
            self.connection.close()
            raise ReadbackError("Kubo RPC block read failed") from exc
        if response.status == 200:
            if len(body) != size:
                self.connection.close()
                raise ReadbackError("local block size differs for " + cid)
            return body
        if response.status == 500:
            try:
                message = json.loads(body)["Message"]
            except (ValueError, KeyError, TypeError) as exc:
                raise ReadbackError("Kubo RPC returned invalid error") from exc
            if (isinstance(message, str) and
                    message.startswith("block was not found locally (offline):") and
                    cid in message):
                self.connection.close()
                return None
        self.connection.close()
        raise ReadbackError("Kubo RPC refused block read for " + cid)


def fresh_state(inventory, repo, peer):
    return {"schema": 1, "inventory_sha256": inventory.sha256,
            "inventory_rows": len(inventory.offsets), "repository": str(repo),
            "peer_id": peer, "cursor": 0, "present_rows": 0,
            "present_bytes": 0, "missing_rows": 0, "missing_bytes": 0,
            "scan_sha256": "0" * 64}


def load_state(path, inventory, repo, peer):
    expected = fresh_state(inventory, repo, peer)
    if not path.exists():
        return expected
    try:
        state = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise ReadbackError("readback checkpoint is unreadable") from exc
    if (not isinstance(state, dict) or set(state) != set(expected) or
            any(state[key] != expected[key] for key in
                ("schema", "inventory_sha256", "inventory_rows", "repository", "peer_id")) or
            any(type(state[key]) is not int or state[key] < 0 for key in
                ("cursor", "present_rows", "present_bytes", "missing_rows", "missing_bytes")) or
            state["cursor"] > expected["inventory_rows"] or
            state["present_rows"] + state["missing_rows"] != state["cursor"] or
            not isinstance(state["scan_sha256"], str) or
            not SHA256.fullmatch(state["scan_sha256"])):
        raise ReadbackError("readback checkpoint differs from repository or inventory")
    return state


def save_state(path, state):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".partial")
    with temp.open("x") as output:
        json.dump(state, output, sort_keys=True)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    os.replace(temp, path)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def advance(inventory, state, reader, max_rows, max_bytes, stop_at_missing=True):
    if state["cursor"] == len(inventory.offsets):
        return dict(state, status="complete" if not state["missing_rows"] else "partial")
    rows, _ = inventory.rows(state["cursor"], max_rows)
    added_bytes = 0
    current = dict(state)
    waiting = False
    for row in rows:
        cid, size = row["cid"], row["size"]
        if added_bytes and added_bytes + size > max_bytes:
            break
        if size > max_bytes:
            raise ReadbackError("single inventory block exceeds batch byte limit")
        data = reader.read(cid, size)
        if data is None and stop_at_missing:
            waiting = True
            break
        if data is not None:
            if len(data) != size:
                raise ReadbackError("local block size differs for " + cid)
            try:
                verify_cid(cid, data)
            except RawBlockStoreError as exc:
                raise ReadbackError("local block CID differs from bytes: " + cid) from exc
            digest = hashlib.sha256(data).digest()
            current["present_rows"] += 1
            current["present_bytes"] += size
            kind = b"present"
        else:
            digest = b"\x00" * 32
            current["missing_rows"] += 1
            current["missing_bytes"] += size
            kind = b"missing"
        current["scan_sha256"] = hashlib.sha256(
            bytes.fromhex(current["scan_sha256"]) +
            current["cursor"].to_bytes(8, "big") + cid.encode("ascii") +
            size.to_bytes(8, "big") + kind + digest).hexdigest()
        current["cursor"] += 1
        added_bytes += size
    current["status"] = ("waiting" if waiting else
                         "complete" if current["cursor"] == len(inventory.offsets)
                         and current["missing_rows"] == 0 else "partial")
    return current


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--count", type=int, required=True)
    parser.add_argument("--ipfs-bin", type=Path, required=True)
    parser.add_argument("--ipfs-path", type=Path, required=True)
    parser.add_argument("--rpc-url", required=True)
    parser.add_argument("--raw-block-store", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--max-rows", type=int, default=200)
    parser.add_argument("--max-bytes", type=int, default=256_000_000)
    parser.add_argument("--scan-gaps", action="store_true",
                        help="continue past locally missing rows; requires a fresh pass after transfer")
    args = parser.parse_args()
    if (not SHA256.fullmatch(args.sha256) or args.count < 1 or
            not 1 <= args.max_rows <= 1000 or not 1 <= args.max_bytes <= 512_000_000):
        parser.error("invalid inventory or batch bound")
    try:
        inventory = Inventory(args.inventory, args.sha256, args.count)
        peer, port = verify_repository(args.ipfs_bin, args.ipfs_path, args.rpc_url)
        args.state.parent.mkdir(parents=True, exist_ok=True)
        with args.state.with_name(args.state.name + ".lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ReadbackError("readback already running") from exc
            state = load_state(args.state, inventory, args.ipfs_path.resolve(), peer)
            reader = OfflineBlocks(port, RawBlockStore(args.raw_block_store))
            try:
                result = advance(inventory, state, reader, args.max_rows, args.max_bytes,
                                 stop_at_missing=not args.scan_gaps)
            finally:
                reader.close()
            status = result.pop("status")
            if result != state:
                save_state(args.state, result)
        print(json.dumps({**result, "status": status,
                          "scope": "offline leaf size and CID at scan time; pin coverage separate"},
                         sort_keys=True))
    except (InventoryError, ReadbackError, OSError, ValueError) as exc:
        print("REFUSED: " + str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
