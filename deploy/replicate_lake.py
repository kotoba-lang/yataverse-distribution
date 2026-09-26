#!/usr/bin/env python3
"""Bounded, resumable copy of the public Yataverse R2 block listing into Kubo.

This is an interim source adapter: the read API still runs on Cloudflare.
Kubo rederives every listed CID before a block is pinned. A durable cursor
advances only after every block on a page has been checked.
"""

import argparse
import datetime
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.parse
from pathlib import Path


CID_V0 = re.compile(r"^Qm[1-9A-HJ-NP-Za-km-z]{44}$")
CID_V1 = re.compile(r"^b[a-z2-7]{20,200}$")
DEFAULT_API = "https://yataverse.com/api/v1/lake/blocks"
DEFAULT_BLOCKS = "https://ipfs.yataverse.com/ipfs/"


class ReplicationError(Exception):
    pass


def valid_cid(cid):
    return isinstance(cid, str) and bool(CID_V0.fullmatch(cid) or CID_V1.fullmatch(cid))


def command(argv, data=None, max_seconds=300):
    result = subprocess.run(
        argv, input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        timeout=max_seconds, check=False,
    )
    if result.returncode:
        detail = result.stderr.decode("utf-8", "replace")[:240].strip()
        raise ReplicationError("{} failed ({}): {}".format(argv[0], result.returncode, detail))
    return result.stdout


def curl(url, limit):
    data = command(
        ["curl", "-fLsS", "--retry", "2", "--retry-delay", "1",
         "--max-redirs", "3", "--proto", "=https", "--proto-redir", "=https",
         "--max-time", "240", "--max-filesize", str(limit), url],
        max_seconds=270,
    )
    if len(data) > limit:
        raise ReplicationError("HTTP body exceeds configured maximum")
    return data


def fetch_listing(api_url, cursor):
    query = "?limit=200"
    if cursor:
        query += "&cursor=" + urllib.parse.quote(cursor, safe="")
    try:
        listing = json.loads(curl(api_url + query, 2_000_000))
    except (ValueError, UnicodeDecodeError) as exc:
        raise ReplicationError("lake listing is not JSON: {}".format(exc))
    if listing.get("ok") is not True:
        raise ReplicationError("lake listing did not return ok=true")
    blocks = listing.get("blocks")
    if not isinstance(blocks, list) or not blocks or len(blocks) > 200:
        raise ReplicationError("lake listing has no bounded block page")
    if type(listing.get("truncated?")) is not bool:
        raise ReplicationError("lake listing has no truncation verdict")
    if listing.get("cursor") is not None and not isinstance(listing["cursor"], str):
        raise ReplicationError("lake listing cursor is not a string")
    if listing.get("truncated?") and not listing.get("cursor"):
        raise ReplicationError("truncated lake listing has no cursor")
    for block in blocks:
        if not isinstance(block, dict) or not valid_cid(block.get("cid")):
            raise ReplicationError("lake listing contains an invalid CID")
        if type(block.get("size")) is not int or block["size"] <= 0:
            raise ReplicationError("lake listing contains an invalid block size")
    return listing


class Kubo:
    def __init__(self, binary):
        if not Path(binary).is_file():
            raise ReplicationError("Kubo binary does not exist")
        self.binary = binary
        self.pins = set()
        self.repo_size = 0
        self.storage_max = 0
        self.repo_path = None

    def run(self, *args, data=None):
        return command([self.binary] + list(args), data=data)

    def preflight(self):
        # Recursive roots are already durable pins. Kubo refuses a second,
        # direct pin on the same CID, so include both kinds on every resume.
        self.pins = set()
        for pin_type in ("direct", "recursive"):
            pins = self.run("pin", "ls", "--type=" + pin_type).decode("utf-8")
            self.pins.update(line.split()[0] for line in pins.splitlines() if line.strip())
        stat = self.run("repo", "stat").decode("utf-8")
        fields = dict(
            line.split(":", 1) for line in stat.splitlines() if ":" in line
        )
        try:
            self.repo_size = int(fields["RepoSize"].strip())
            self.storage_max = int(fields["StorageMax"].strip())
            self.repo_path = Path(fields["RepoPath"].strip())
        except (KeyError, ValueError) as exc:
            raise ReplicationError("Kubo repo capacity unreadable: {}".format(exc))
        if not self.repo_path.is_dir():
            raise ReplicationError("Kubo repo path is not a directory")
        if self.storage_max <= self.repo_size:
            raise ReplicationError("Kubo repo has no configured storage headroom")

    def require_disk_reserve(self, new_bytes, reserve):
        free = shutil.disk_usage(self.repo_path).free
        if free - 2 * new_bytes < reserve:
            raise ReplicationError("physical disk reserve would be crossed")

    def block_size(self, cid):
        stat = self.run("block", "stat", cid).decode("utf-8")
        found = re.search(r"^Size:\s*(\d+)\s*$", stat, re.MULTILINE)
        if not found:
            raise ReplicationError("Kubo block size unreadable for " + cid)
        return int(found.group(1))

    def durable_pin_type(self, cid):
        try:
            result = self.run("pin", "ls", "--type=all", cid).decode("utf-8").strip()
        except ReplicationError:
            return None
        match = re.fullmatch(re.escape(cid) + r"\s+(direct|recursive)", result)
        return match.group(1) if match else None

    def put_verified(self, cid, data):
        prefix = self.run("cid", "format", "-f", "%v %c %h %L", cid).decode("utf-8").strip().split()
        if len(prefix) != 4:
            raise ReplicationError("Kubo could not decode CID prefix " + cid)
        version, codec, hash_name, hash_length = prefix
        if version == "cidv0":
            argv = ("block", "put", "--format=v0", "-")
        elif version == "cidv1":
            argv = ("block", "put", "--cid-codec=" + codec, "--mhtype=" + hash_name,
                    "--mhlen=" + hash_length, "-")
        else:
            raise ReplicationError("unsupported CID version " + version)
        if len(data) > 2 * 1024 * 1024:
            argv = argv[:-1] + ("--allow-big-block", "-")
        produced = self.run(*argv, data=data).decode("utf-8").strip()
        if produced != cid:
            raise ReplicationError("Kubo rederived a different CID for " + cid)
        reread = self.run("block", "get", cid)
        if reread != data:
            raise ReplicationError("Kubo readback differs for " + cid)
        try:
            self.run("pin", "add", "--recursive=false", cid)
        except ReplicationError:
            # Another writer may have pinned it after preflight. A failed pin
            # is acceptable only when Kubo itself confirms a durable pin.
            if self.durable_pin_type(cid) is None:
                raise
        self.pins.add(cid)


def state_load(path):
    if not path.exists():
        return {"schema": 1, "cursor": None, "cycles": 0,
                "pages_total": 0, "new_blocks_total": 0, "new_bytes_total": 0}
    try:
        state = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise ReplicationError("checkpoint unreadable: {}".format(exc))
    required = ("cursor", "cycles", "pages_total", "new_blocks_total", "new_bytes_total")
    if state.get("schema") != 1 or any(key not in state for key in required):
        raise ReplicationError("checkpoint has unknown schema")
    if state["cursor"] is not None and not isinstance(state["cursor"], str):
        raise ReplicationError("checkpoint cursor is invalid")
    if any(type(state[key]) is not int or state[key] < 0 for key in required[1:]):
        raise ReplicationError("checkpoint counters are invalid")
    return state


def state_save(path, state):
    temp = path.with_suffix(path.suffix + ".tmp")
    with temp.open("w") as handle:
        json.dump(state, handle, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(str(temp), str(path))


def append_receipt(path, cid, size, data):
    receipt = {
        "at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "cid": cid, "bytes": size, "sha256": hashlib.sha256(data).hexdigest(),
    }
    with path.open("a") as handle:
        handle.write(json.dumps(receipt, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def run_batch(args, kubo, listing_fn=fetch_listing, block_fn=curl):
    args.state_dir.mkdir(parents=True, exist_ok=True)
    state_path = args.state_dir / "checkpoint.json"
    receipt_path = args.state_dir / "receipts.jsonl"
    state = state_load(state_path)
    kubo.preflight()
    new_bytes = 0
    new_blocks = 0
    checked_blocks = 0
    pages = 0

    for _ in range(args.max_pages):
        listing = listing_fn(args.api_url, state["cursor"])
        for item in listing["blocks"]:
            cid, size = item["cid"], item["size"]
            if size > args.max_block_bytes:
                raise ReplicationError("block exceeds configured maximum: " + cid)
            if cid in kubo.pins:
                if kubo.block_size(cid) != size:
                    raise ReplicationError("pinned block size differs from listing: " + cid)
                checked_blocks += 1
                continue
            if new_bytes + size > args.max_new_bytes:
                return {"status": "byte-budget", "pages": pages,
                        "checked_blocks": checked_blocks, "new_blocks": new_blocks,
                        "new_bytes": new_bytes, "cursor_advanced": False}
            if kubo.repo_size + 2 * (new_bytes + size) >= kubo.storage_max:
                raise ReplicationError("Kubo storage limit would be exceeded before " + cid)
            kubo.require_disk_reserve(size, args.min_free_bytes)
            data = block_fn(args.block_url_base + cid, args.max_block_bytes)
            if len(data) != size:
                raise ReplicationError("source byte count differs from listing: " + cid)
            kubo.put_verified(cid, data)
            append_receipt(receipt_path, cid, size, data)
            new_bytes += size
            new_blocks += 1
            checked_blocks += 1
            state["new_blocks_total"] += 1
            state["new_bytes_total"] += size
            state_save(state_path, state)

        pages += 1
        state["pages_total"] += 1
        if listing.get("truncated?"):
            state["cursor"] = listing["cursor"]
            state_save(state_path, state)
        else:
            state["cursor"] = None
            state["cycles"] += 1
            state_save(state_path, state)
            return {"status": "cycle-complete", "pages": pages,
                    "checked_blocks": checked_blocks, "new_blocks": new_blocks,
                    "new_bytes": new_bytes, "cursor_advanced": True}
    return {"status": "page-limit", "pages": pages,
            "checked_blocks": checked_blocks, "new_blocks": new_blocks,
            "new_bytes": new_bytes, "cursor_advanced": True}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ipfs-bin", required=True)
    parser.add_argument("--state-dir", required=True, type=Path)
    parser.add_argument("--api-url", default=DEFAULT_API)
    parser.add_argument("--block-url-base", default=DEFAULT_BLOCKS)
    parser.add_argument("--max-pages", type=int, default=20)
    parser.add_argument("--max-new-bytes", type=int, default=512_000_000)
    parser.add_argument("--max-block-bytes", type=int, default=256_000_000)
    parser.add_argument("--min-free-bytes", type=int, default=50_000_000_000)
    args = parser.parse_args()
    if args.max_pages < 1 or args.max_new_bytes < 1 or args.max_block_bytes < 1 or args.min_free_bytes < 0:
        parser.error("batch budgets must be positive and disk reserve nonnegative")
    args.state_dir.mkdir(parents=True, exist_ok=True)
    lock_path = args.state_dir / "replicate.lock"
    try:
        with lock_path.open("w") as lock:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ReplicationError("another lake replication is running")
            result = run_batch(args, Kubo(args.ipfs_bin))
        print(json.dumps(result, sort_keys=True))
    except (ReplicationError, OSError, subprocess.TimeoutExpired) as exc:
        print("REFUSED: {}".format(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
