#!/usr/bin/env python3
"""Capture a complete, resumable public lake listing without publishing a partial snapshot.

The R2 listing is ordered by CID, but is not an atomic snapshot. A completed
walk is therefore a dated candidate until a second walk agrees and the old
inventory is proved to be a subset. Never replace a live reader's inventory
with a candidate by merely changing this script's output path.
"""

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sys
import uuid

from replicate_lake import ReplicationError, fetch_listing


class CaptureError(Exception):
    pass


def atomic_json(path, value):
    tmp = path.with_name(path.name + "." + uuid.uuid4().hex + ".partial")
    with tmp.open("x") as file:
        file.write(json.dumps(value, sort_keys=True) + "\n")
        file.flush()
        os.fsync(file.fileno())
    tmp.replace(path)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def digest_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def scan_partial(path, size):
    if path.stat().st_size < size:
        raise CaptureError("partial inventory is shorter than checkpoint")
    with path.open("r+b") as file:
        file.truncate(size)  # A crash may leave one uncheckpointed page.
        file.flush()
        os.fsync(file.fileno())
    digest = hashlib.sha256()
    rows = byte_count = 0
    previous = ""
    with path.open("rb") as file:
        for line in file:
            digest.update(line)
            try:
                row = json.loads(line)
                cid, block_size = row["cid"], row["bytes"]
            except (ValueError, KeyError, TypeError) as exc:
                raise CaptureError("partial inventory has invalid row") from exc
            if (not isinstance(cid, str) or cid <= previous or
                    type(block_size) is not int or block_size < 1):
                raise CaptureError("partial inventory has invalid order or size")
            previous = cid
            rows += 1
            byte_count += block_size
    return {"sha256": digest.hexdigest(), "rows": rows,
            "bytes": byte_count, "last_cid": previous}


def require_superset(old_path, new_path, old_sha256):
    if digest_file(old_path) != old_sha256:
        raise CaptureError("old inventory digest differs")
    old_rows = 0
    with old_path.open("rb") as old, new_path.open("rb") as new:
        current = None
        previous_old = ""
        for line in old:
            row = json.loads(line)
            cid = row["cid"]
            if cid <= previous_old:
                raise CaptureError("old inventory is not sorted")
            previous_old = cid
            old_rows += 1
            while current is None or current["cid"] < cid:
                next_line = new.readline()
                if not next_line:
                    raise CaptureError("new inventory omits old CID: " + cid)
                current = json.loads(next_line)
            if current["cid"] != cid or current["bytes"] != row["bytes"]:
                raise CaptureError("new inventory changes old CID or size: " + cid)
    return old_rows


def capture(args):
    output = args.output.resolve()
    partial = output.with_name(output.name + ".partial")
    state_path = args.state.resolve()
    if args.old_inventory.resolve() in (output, partial) or state_path in (output, partial):
        raise CaptureError("capture paths overlap")
    old_sha = digest_file(args.old_inventory)
    if old_sha != args.old_sha256:
        raise CaptureError("old inventory digest differs")
    if output.exists():
        if not state_path.exists() or partial.exists():
            raise CaptureError("completed output already exists")
        state = json.loads(state_path.read_text())
        if (state.get("schema") != 1 or state.get("api_url") != args.api_url or
                state.get("output") != str(output) or state.get("old_sha256") != old_sha or
                state.get("size") != output.stat().st_size or
                state.get("sha256") != digest_file(output)):
            raise CaptureError("completed output differs from checkpoint")
        if state.get("status") == "complete":
            return {"status": "complete", "rows": state["rows"],
                    "bytes": state["bytes"], "sha256": state["sha256"],
                    "old_rows": state["old_rows"], "pages": state["pages"]}
        if state.get("status") != "in-progress":
            raise CaptureError("completed output has invalid checkpoint")
        old_rows = require_superset(args.old_inventory, output, old_sha)
        atomic_json(state_path, {**state, "status": "complete", "old_rows": old_rows})
        return {"status": "complete", "rows": state["rows"],
                "bytes": state["bytes"], "sha256": state["sha256"],
                "old_rows": old_rows, "pages": state["pages"]}
    if state_path.exists():
        state = json.loads(state_path.read_text())
        if (state.get("schema") != 1 or state.get("api_url") != args.api_url or
                state.get("output") != str(output) or state.get("old_sha256") != old_sha or
                state.get("status") != "in-progress"):
            raise CaptureError("checkpoint identity differs")
        if not partial.is_file():
            raise CaptureError("checkpoint has no partial inventory")
    else:
        if partial.exists():
            raise CaptureError("uncheckpointed partial inventory exists")
        partial.parent.mkdir(parents=True, exist_ok=True)
        partial.touch(exist_ok=False)
        state = {"schema": 1, "api_url": args.api_url, "output": str(output),
                 "old_sha256": old_sha, "status": "in-progress", "cursor": None,
                 "pages": 0, "size": 0, "rows": 0, "bytes": 0, "last_cid": "",
                 "sha256": hashlib.sha256(b"").hexdigest()}
        atomic_json(state_path, state)
    scanned = scan_partial(partial, state["size"])
    if any(scanned[key] != state[key] for key in ("sha256", "rows", "bytes", "last_cid")):
        raise CaptureError("partial inventory differs from checkpoint")
    running_digest = hashlib.sha256()
    with partial.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            running_digest.update(chunk)

    pages = 0
    while args.max_pages is None or pages < args.max_pages:
        listing = fetch_listing(args.api_url, state["cursor"])
        cursor = listing["cursor"]
        if listing["truncated?"] and cursor == state["cursor"]:
            raise CaptureError("listing cursor did not advance")
        previous = state["last_cid"]
        lines = []
        page_bytes = 0
        for block in listing["blocks"]:
            cid, size = block["cid"], block["size"]
            if cid <= previous:
                raise CaptureError("listing order repeated or moved backward")
            previous = cid
            page_bytes += size
            lines.append(json.dumps({"cid": cid, "bytes": size}, separators=(",", ":")) + "\n")
        payload = "".join(lines).encode()
        with partial.open("ab") as file:
            file.write(payload)
            file.flush()
            os.fsync(file.fileno())
        running_digest.update(payload)
        # The digest is recomputed from disk at resume before trusting a cursor.
        state = {**state, "cursor": cursor, "pages": state["pages"] + 1,
                 "size": state["size"] + len(payload),
                 "rows": state["rows"] + len(lines),
                 "bytes": state["bytes"] + page_bytes,
                 "last_cid": previous, "sha256": running_digest.hexdigest()}
        atomic_json(state_path, state)
        pages += 1
        if not listing["truncated?"]:
            old_rows = require_superset(args.old_inventory, partial, old_sha)
            partial.replace(output)
            directory = os.open(output.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            atomic_json(state_path, {**state, "status": "complete",
                                     "old_rows": old_rows})
            return {"status": "complete", "rows": state["rows"],
                    "bytes": state["bytes"], "sha256": state["sha256"],
                    "old_rows": old_rows, "pages": state["pages"]}
    return {"status": "in-progress", "rows": state["rows"],
            "pages": state["pages"], "cursor": state["cursor"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-url", default="https://yataverse.com/api/v1/lake/blocks")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--state", required=True, type=Path)
    parser.add_argument("--old-inventory", required=True, type=Path)
    parser.add_argument("--old-sha256", required=True)
    parser.add_argument("--max-pages", type=int)
    args = parser.parse_args()
    if args.max_pages is not None and args.max_pages < 1:
        parser.error("max-pages must be positive")
    try:
        args.state.parent.mkdir(parents=True, exist_ok=True)
        lock_path = args.state.with_name(args.state.name + ".lock")
        with lock_path.open("a+") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise CaptureError("inventory capture already running") from exc
            print(json.dumps(capture(args), sort_keys=True))
    except (CaptureError, ReplicationError, OSError, ValueError, KeyError, TypeError) as exc:
        print("REFUSED: " + str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
