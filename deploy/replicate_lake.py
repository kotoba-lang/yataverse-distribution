#!/usr/bin/env python3
"""Bounded, resumable Yataverse lake copy into Kubo.

The pinned inventory and a peer's HTTPS gateway can replace Cloudflare reads.
Kubo rederives every listed CID before a block is pinned. A durable cursor
advances only after every block on a page has been checked.
"""

import argparse
import base64
import binascii
from collections import deque
from concurrent.futures import ThreadPoolExecutor
import datetime
import fcntl
import hashlib
import http.client
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import uuid
from pathlib import Path
from raw_block_store import RawBlockStore, RawBlockStoreError


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


def local_listing(url):
    parsed = urllib.parse.urlsplit(url)
    return (parsed.scheme == "http" and parsed.hostname == "127.0.0.1" and
            parsed.path == "/api/v1/lake/blocks" and not parsed.query)


def curl(url, limit, resolve=None, max_seconds=240, retries=2):
    parsed = urllib.parse.urlsplit(url)
    protocol = "=http" if parsed.scheme == "http" and parsed.hostname == "127.0.0.1" else "=https"
    if ((protocol == "=http" and parsed.hostname != "127.0.0.1") or
            (protocol == "=https" and parsed.scheme != "https")):
        raise ReplicationError("HTTP source must be loopback or HTTPS")
    # The public IPFS gateway may redirect to a CID host. A loopback inventory
    # or named LAN peer must not redirect outside its own authority.
    follow = protocol == "=https" and resolve is None
    argv = ["curl", "-fLsS" if follow else "-fsS", "--retry", str(retries),
            "--retry-delay", "1", "--max-redirs", "3" if follow else "0",
            "--proto", protocol, "--proto-redir", protocol,
            "--connect-timeout", "2", "--max-time", str(max_seconds),
            "--max-filesize", str(limit)]
    if resolve:
        argv += ["--noproxy", "*", "--resolve", resolve]
    argv.append(url)
    data = command(
        argv,
        max_seconds=max_seconds + 30,
    )
    if len(data) > limit:
        raise ReplicationError("HTTP body exceeds configured maximum")
    return data


def fetch_listing(api_url, cursor):
    query = "" if local_listing(api_url) else "?limit=200"
    if cursor:
        query += ("?" if not query else "&") + "cursor=" + urllib.parse.quote(cursor, safe="")
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
    def __init__(self, binary, raw_store=None):
        if not Path(binary).is_file():
            raise ReplicationError("Kubo binary does not exist")
        self.binary = binary
        self.pins = set()
        self.repo_size = 0
        self.storage_max = 0
        self.repo_path = None
        self.raw_store = raw_store
        self.raw_cids = set()

    def run(self, *args, data=None):
        return command([self.binary] + list(args), data=data)

    def preflight(self):
        # Recursive roots are already durable pins. Kubo refuses a second,
        # direct pin on the same CID, so include both kinds on every resume.
        self.pins = set()
        for pin_type in ("direct", "recursive"):
            pins = self.run("pin", "ls", "--type=" + pin_type).decode("utf-8")
            self.pins.update(line.split()[0] for line in pins.splitlines() if line.strip())
        if self.raw_store is not None:
            self.raw_cids = self.raw_store.cids()
            self.pins.update(self.raw_cids)
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
        if cid in self.raw_cids:
            return len(self.raw_store.read(cid))
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


class KuboRPC(Kubo):
    """Use the daemon's loopback RPC for per-block work; retain CLI preflight.

    RPC never leaves loopback. The CLI and RPC repository paths must agree so
    the speedup cannot silently write to a different Kubo daemon.
    """

    def __init__(self, binary, api_url, raw_store=None):
        super().__init__(binary, raw_store=raw_store)
        parsed = urllib.parse.urlsplit(api_url)
        if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or
                parsed.path not in ("", "/") or parsed.query or parsed.fragment or
                parsed.username or parsed.password or not parsed.port):
            raise ReplicationError("Kubo RPC must be a loopback HTTP origin")
        self.api_port = parsed.port
        self.connection = None

    def _request(self, command_name, params=None, data=None, max_response=2_000_000):
        query = urllib.parse.urlencode(params or {})
        path = "/api/v0/" + command_name + ("?" + query if query else "")
        if self.connection is None:
            self.connection = http.client.HTTPConnection("127.0.0.1", self.api_port, timeout=300)
        try:
            if data is None:
                self.connection.request("POST", path)
            else:
                boundary = "yataverse-" + uuid.uuid4().hex
                prefix = ("--" + boundary + "\r\nContent-Disposition: form-data; name=\"file\"; "
                          "filename=\"block\"\r\nContent-Type: application/octet-stream\r\n\r\n").encode()
                suffix = ("\r\n--" + boundary + "--\r\n").encode()
                self.connection.putrequest("POST", path)
                self.connection.putheader("Content-Type", "multipart/form-data; boundary=" + boundary)
                self.connection.putheader("Content-Length", str(len(prefix) + len(data) + len(suffix)))
                self.connection.endheaders()
                self.connection.send(prefix)
                self.connection.send(data)
                self.connection.send(suffix)
            response = self.connection.getresponse()
            body = response.read(max_response + 1)
            if len(body) > max_response:
                raise ReplicationError("Kubo RPC response exceeds bound: " + command_name)
            if response.status != 200:
                try:
                    detail = json.loads(body).get("Message", "")
                except (ValueError, UnicodeDecodeError, AttributeError):
                    detail = ""
                detail = detail[:160] if isinstance(detail, str) else ""
                raise ReplicationError("Kubo RPC refused " + command_name +
                                       " (HTTP " + str(response.status) + "): " + detail)
            return body
        except (OSError, http.client.HTTPException) as exc:
            raise ReplicationError("Kubo RPC unavailable: " + command_name) from exc
        finally:
            # An error may leave unread response bytes or an uncertain write.
            if sys.exc_info()[0] is not None and self.connection is not None:
                self.connection.close()
                self.connection = None

    def _json(self, command_name, params=None, data=None):
        try:
            result = json.loads(self._request(command_name, params, data))
        except (ValueError, UnicodeDecodeError) as exc:
            raise ReplicationError("Kubo RPC returned invalid JSON: " + command_name) from exc
        if not isinstance(result, dict):
            raise ReplicationError("Kubo RPC returned wrong shape: " + command_name)
        return result

    def preflight(self):
        super().preflight()
        status = self._json("repo/stat")
        if (not isinstance(status.get("RepoPath"), str) or
                Path(status["RepoPath"]).resolve() != self.repo_path.resolve()):
            raise ReplicationError("Kubo RPC and CLI repository paths differ")

    def block_size(self, cid):
        if cid in self.raw_cids:
            return len(self.raw_store.read(cid))
        result = self._json("block/stat", {"arg": cid})
        if type(result.get("Size")) is not int or result["Size"] < 0:
            raise ReplicationError("Kubo RPC block size unreadable for " + cid)
        return result["Size"]

    def durable_pin_type(self, cid):
        try:
            result = self._json("pin/ls", {"arg": cid, "type": "all"})
        except ReplicationError:
            return None
        pins = result.get("Keys")
        if isinstance(pins, dict) and isinstance(pins.get(cid), dict):
            kind = pins[cid].get("Type")
            if kind in ("direct", "recursive"):
                return kind
        return None

    def put_verified(self, cid, data):
        result = self._json("cid/format", {"arg": cid, "f": "%v %c %h %L"})
        prefix = str(result.get("Formatted", "")).split()
        if len(prefix) != 4:
            raise ReplicationError("Kubo RPC could not decode CID prefix " + cid)
        version, codec, hash_name, hash_length = prefix
        if version == "cidv0":
            options = {"format": "v0"}
        elif version == "cidv1":
            options = {"cid-codec": codec, "mhtype": hash_name, "mhlen": hash_length}
        else:
            raise ReplicationError("unsupported CID version " + cid)
        if len(data) > 2 * 1024 * 1024:
            options["allow-big-block"] = "true"
        produced = self._json("block/put", options, data).get("Key")
        if produced != cid:
            raise ReplicationError("Kubo RPC rederived a different CID for " + cid)
        reread = self._request("block/get", {"arg": cid}, max_response=len(data))
        if reread != data:
            raise ReplicationError("Kubo RPC readback differs for " + cid)
        try:
            pinned = self._json("pin/add", {"arg": cid, "recursive": "false"})
            if not isinstance(pinned.get("Pins"), list) or cid not in pinned["Pins"]:
                raise ReplicationError("Kubo RPC pin receipt differs for " + cid)
        except ReplicationError as exc:
            if self.durable_pin_type(cid) is None:
                # Some upstream blocks carry a dag-pb CID over bytes that are
                # not decodable as dag-pb. Kubo rederived the CID and returned
                # the exact bytes above, but its pin/add decoder refuses them.
                # Preserve only this observed refusal in an independently
                # CID-checked, fsynced raw store; all other pin errors fail.
                if (self.raw_store is None or "pin: protobuf:" not in str(exc)):
                    raise
                self.raw_store.put(cid, data)
                self.raw_cids.add(cid)
                self.pins.add(cid)
                return
        if self.durable_pin_type(cid) is None:
            raise ReplicationError("Kubo RPC durable pin missing for " + cid)
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


def legacy_cursor_cid(cursor):
    if not isinstance(cursor, str) or not re.fullmatch(r"1-[A-Za-z0-9_-]+", cursor):
        raise ReplicationError("legacy listing cursor has unknown format")
    encoded = cursor[2:]
    try:
        padded = encoded + "=" * ((4 - len(encoded) % 4) % 4)
        payload = json.loads(urllib.parse.unquote(
            base64.urlsafe_b64decode(padded).decode("utf-8")))
    except (ValueError, UnicodeDecodeError, binascii.Error) as exc:
        raise ReplicationError("legacy listing cursor cannot be decoded") from exc
    key = payload.get("startAfter") if isinstance(payload, dict) and payload.get("v") == 1 else None
    cid = key[5:] if isinstance(key, str) and key.startswith("ipld/") else None
    if not valid_cid(cid):
        raise ReplicationError("legacy listing cursor has no valid block CID")
    return cid


def inventory_offset(path, expected_sha256, after_cid):
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise ReplicationError("inventory digest is invalid")
    digest = hashlib.sha256()
    offset = None
    with path.open("rb") as source:
        for index, line in enumerate(source):
            digest.update(line)
            if after_cid is not None and offset is None:
                try:
                    row = json.loads(line)
                except ValueError as exc:
                    raise ReplicationError("inventory row is unreadable") from exc
                if row.get("cid") == after_cid:
                    offset = index + 1
    if digest.hexdigest() != expected_sha256:
        raise ReplicationError("inventory digest differs from pinned snapshot")
    if after_cid is not None and offset is None:
        raise ReplicationError("legacy cursor CID absent from local inventory")
    return str(offset) if offset is not None else None


def inventory_sizes(path, expected_sha256):
    """Read a complete pinned snapshot before changing the durable cursor."""
    if not isinstance(expected_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise ReplicationError("inventory digest is invalid")
    digest = hashlib.sha256()
    sizes = {}
    with Path(path).open("rb") as source:
        for line in source:
            digest.update(line)
            try:
                row = json.loads(line)
                cid, size = row["cid"], row["bytes"]
            except (ValueError, KeyError, TypeError) as exc:
                raise ReplicationError("inventory row is unreadable") from exc
            if not valid_cid(cid) or type(size) is not int or size <= 0 or cid in sizes:
                raise ReplicationError("inventory CID, size, or uniqueness is invalid")
            sizes[cid] = size
    if digest.hexdigest() != expected_sha256 or not sizes:
        raise ReplicationError("inventory digest differs from pinned snapshot or is empty")
    return sizes


def prepare_listing_state(args, path, state):
    local = local_listing(args.api_url)
    marker = state.get("listing_source")
    if marker is not None and not isinstance(marker, str):
        raise ReplicationError("checkpoint listing source is invalid")
    if not local:
        if marker is not None:
            raise ReplicationError("checkpoint listing source cannot use public listing")
        return state
    inventory = getattr(args, "inventory_path", None)
    expected = getattr(args, "inventory_sha256", None)
    if not inventory or not expected:
        raise ReplicationError("local listing requires a pinned inventory path and digest")
    target_marker = "local-inventory:" + expected
    if marker and marker != target_marker:
        previous_path = getattr(args, "previous_inventory_path", None)
        previous_sha256 = getattr(args, "previous_inventory_sha256", None)
        if not previous_path or marker != "local-inventory:" + str(previous_sha256):
            raise ReplicationError("checkpoint inventory identity differs")
        if state["cursor"] is not None and not re.fullmatch(r"[0-9]+", state["cursor"]):
            raise ReplicationError("previous inventory cursor is not a numeric offset")
        old_sizes = inventory_sizes(previous_path, previous_sha256)
        new_sizes = inventory_sizes(inventory, expected)
        for cid, size in old_sizes.items():
            if new_sizes.get(cid) != size:
                raise ReplicationError("new inventory omits or changes previous CID")
        if fetch_listing(args.api_url, None).get("inventory-sha256") != expected:
            raise ReplicationError("local listing is not serving the new inventory")
        if path.exists():
            old = path.read_bytes()
            backup = path.with_name("checkpoint.pre-inventory-" + previous_sha256 + ".json")
            if backup.exists():
                if backup.read_bytes() != old:
                    raise ReplicationError("inventory migration backup differs")
            else:
                with backup.open("xb") as handle:
                    handle.write(old)
                    handle.flush()
                    os.fsync(handle.fileno())
        state["cursor"] = "0"
        state["listing_source"] = target_marker
        state_save(path, state)
        return state
    if marker and state["cursor"] is not None and not re.fullmatch(r"[0-9]+", state["cursor"]):
        raise ReplicationError("local listing cursor is not a numeric offset")
    after_cid = legacy_cursor_cid(state["cursor"]) if not marker and state["cursor"] else None
    offset = inventory_offset(inventory, expected, after_cid)
    if not marker:
        if path.exists():
            old = path.read_bytes()
            backup = path.with_name("checkpoint.pre-local-inventory.json")
            if backup.exists():
                if backup.read_bytes() != old:
                    raise ReplicationError("checkpoint migration backup differs")
            else:
                with backup.open("xb") as handle:
                    handle.write(old)
                    handle.flush()
                    os.fsync(handle.fileno())
        state["cursor"] = offset
        state["listing_source"] = target_marker
        state_save(path, state)
    return state


def peer_first_block_fetcher(args, public_fetch):
    peer_base = getattr(args, "peer_block_url_base", None)
    if not peer_base:
        if getattr(args, "peer_only", False):
            raise ReplicationError("peer-only mode has no peer source")
        return public_fetch
    peer = urllib.parse.urlsplit(peer_base)
    resolve = getattr(args, "peer_resolve", None)
    if (peer.scheme != "https" or not peer.hostname or not peer_base.endswith("/ipfs/") or
            not resolve or not resolve.startswith(peer.hostname + ":443:")):
        raise ReplicationError("peer source must be an explicit HTTPS host and address")

    def fetch(url, limit):
        if not url.startswith(args.block_url_base):
            raise ReplicationError("block source URL differs from configured base")
        cid = url[len(args.block_url_base):]
        if not valid_cid(cid):
            raise ReplicationError("block source URL has invalid CID")
        # A peer-only follower may reach a CID before the leader has pinned
        # it. Bound the wait; never advance the cursor or use the public
        # source while waiting. curl itself retries transient HTTP failures.
        deadline = time.monotonic() + getattr(args, "peer_wait_seconds", 0)
        while True:
            try:
                return curl(peer_base + cid, limit, resolve=resolve, max_seconds=12, retries=3)
            except ReplicationError as exc:
                if not getattr(args, "peer_only", False):
                    return public_fetch(url, limit)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ReplicationError(
                        "peer-only block source unavailable: {}: {}".format(cid, exc)) from exc
                time.sleep(min(10, remaining))

    return fetch


def origin_fallback_block_fetcher(args, primary_fetch):
    """Use the public origin only when a configured local bootstrap source fails.

    The source counts are emitted with each batch result, so the bridge failing
    cannot be mistaken for a successful bridge-backed copy.
    """
    fallback_base = getattr(args, "fallback_block_url_base", None)
    if not fallback_base:
        return primary_fetch
    primary_base = args.block_url_base
    if (not primary_base.endswith("/ipfs/") or
            not fallback_base.startswith("https://") or
            not fallback_base.endswith("/ipfs/") or
            primary_base == fallback_base):
        raise ReplicationError("invalid origin fallback configuration")
    counts = {"primary": 0, "fallback": 0}
    count_lock = threading.Lock()
    primary_suppressed_until = 0.0

    def fetch(url, limit):
        nonlocal primary_suppressed_until
        if not url.startswith(primary_base):
            raise ReplicationError("block source URL differs from configured base")
        cid = url[len(primary_base):]
        if not valid_cid(cid):
            raise ReplicationError("block source URL has invalid CID")
        with count_lock:
            suppressed = time.monotonic() < primary_suppressed_until
        try:
            if suppressed:
                raise ReplicationError("primary bridge temporarily unavailable")
            data = primary_fetch(url, limit)
            source = "primary"
        except ReplicationError as primary_error:
            if not suppressed:
                with count_lock:
                    primary_suppressed_until = time.monotonic() + 60
            try:
                data = primary_fetch(fallback_base + cid, limit)
                source = "fallback"
            except ReplicationError as fallback_error:
                raise ReplicationError(
                    "both block origins failed: primary={}, fallback={}".format(
                        primary_error, fallback_error)) from fallback_error
        with count_lock:
            counts[source] += 1
        return data

    fetch.source_counts = counts
    return fetch


def append_receipt(path, cid, size, data):
    receipt = {
        "at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "cid": cid, "bytes": size, "sha256": hashlib.sha256(data).hexdigest(),
    }
    with path.open("a") as handle:
        handle.write(json.dumps(receipt, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def prefetch_ordered(items, block_fn, base, max_block_bytes, workers, max_prefetch_bytes):
    """Fetch a bounded window concurrently; yield bytes in inventory order.

    The caller alone writes Kubo and the cursor. A failed fetch therefore
    leaves the page cursor where it was, and completed downloads are disposable.
    """
    pending = deque()
    pending_bytes = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for item in items:
            size = item["size"]
            if size > max_prefetch_bytes:
                raise ReplicationError("block exceeds prefetch memory budget: " + item["cid"])
            while pending and (len(pending) >= workers or
                               pending_bytes + size > max_prefetch_bytes):
                oldest, future = pending.popleft()
                pending_bytes -= oldest["size"]
                yield oldest, future.result()
            # Bound actual response bytes by the inventory claim as well as
            # the aggregate in-flight reservation above.
            pending.append((item, pool.submit(block_fn, base + item["cid"],
                                              min(size, max_block_bytes))))
            pending_bytes += size
        while pending:
            oldest, future = pending.popleft()
            pending_bytes -= oldest["size"]
            yield oldest, future.result()


def run_batch(args, kubo, listing_fn=fetch_listing, block_fn=curl):
    args.state_dir.mkdir(parents=True, exist_ok=True)
    state_path = args.state_dir / "checkpoint.json"
    receipt_path = args.state_dir / "receipts.jsonl"
    state = prepare_listing_state(args, state_path, state_load(state_path))
    origin_fetch = origin_fallback_block_fetcher(args, block_fn)
    fetch_block = peer_first_block_fetcher(args, origin_fetch)

    def result_with_sources(result):
        if hasattr(origin_fetch, "source_counts"):
            result["origin_sources"] = dict(origin_fetch.source_counts)
        return result
    kubo.preflight()
    new_bytes = 0
    new_blocks = 0
    checked_blocks = 0
    pages = 0

    for _ in range(args.max_pages):
        listing = listing_fn(args.api_url, state["cursor"])
        if local_listing(args.api_url) and listing.get("inventory-sha256") != args.inventory_sha256:
            raise ReplicationError("local listing inventory identity differs")
        selected = []
        planned_bytes = 0
        budget_stop = False
        for item in listing["blocks"]:
            cid, size = item["cid"], item["size"]
            if size > args.max_block_bytes:
                raise ReplicationError("block exceeds configured maximum: " + cid)
            if cid in kubo.pins:
                if kubo.block_size(cid) != size:
                    raise ReplicationError("pinned block size differs from listing: " + cid)
                checked_blocks += 1
                continue
            if new_bytes + planned_bytes + size > args.max_new_bytes:
                budget_stop = True
                break
            if kubo.repo_size + 2 * (new_bytes + planned_bytes + size) >= kubo.storage_max:
                raise ReplicationError("Kubo storage limit would be exceeded before " + cid)
            # free disk already reflects earlier pages in this batch; reserve
            # only the page whose downloads may be in flight concurrently.
            kubo.require_disk_reserve(planned_bytes + size, args.min_free_bytes)
            selected.append(item)
            planned_bytes += size

        for item, data in prefetch_ordered(
                selected, fetch_block, args.block_url_base, args.max_block_bytes,
                args.fetch_workers, args.max_prefetch_bytes):
            cid, size = item["cid"], item["size"]
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

        if budget_stop:
            return result_with_sources({"status": "byte-budget", "pages": pages,
                    "checked_blocks": checked_blocks, "new_blocks": new_blocks,
                    "new_bytes": new_bytes, "cursor_advanced": False})

        pages += 1
        state["pages_total"] += 1
        if listing.get("truncated?"):
            state["cursor"] = listing["cursor"]
            state_save(state_path, state)
        else:
            state["cursor"] = None
            state["cycles"] += 1
            state_save(state_path, state)
            return result_with_sources({"status": "cycle-complete", "pages": pages,
                    "checked_blocks": checked_blocks, "new_blocks": new_blocks,
                    "new_bytes": new_bytes, "cursor_advanced": True})
    return result_with_sources({"status": "page-limit", "pages": pages,
            "checked_blocks": checked_blocks, "new_blocks": new_blocks,
            "new_bytes": new_bytes, "cursor_advanced": True})


def run_and_record_batch(args, kubo, listing_fn=fetch_listing, block_fn=curl):
    """Persist the completed batch and its source counts before reporting success."""
    started_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    result = run_batch(args, kubo, listing_fn, block_fn)
    checkpoint = state_load(args.state_dir / "checkpoint.json")
    receipt = {
        "schema": 1,
        "started_at": started_at,
        "completed_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "pid": os.getpid(),
        "systemd_invocation_id": os.environ.get("INVOCATION_ID"),
        "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "inventory_sha256": getattr(args, "inventory_sha256", None),
        "listing_source": checkpoint.get("listing_source"),
        "checkpoint": checkpoint,
        "result": result,
    }
    receipt_dir = args.state_dir / "batch-receipts"
    receipt_dir.mkdir(parents=True, exist_ok=True)
    name = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    state_save(receipt_dir / (name + "-" + uuid.uuid4().hex + ".json"), receipt)
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ipfs-bin", required=True)
    parser.add_argument("--kubo-api-url", help="loopback Kubo RPC origin for per-block operations")
    parser.add_argument("--raw-block-store", type=Path,
                        help="durable CID-checked store for Kubo-undecodable blocks")
    parser.add_argument("--state-dir", required=True, type=Path)
    parser.add_argument("--api-url", default=DEFAULT_API)
    parser.add_argument("--inventory-path", type=Path)
    parser.add_argument("--inventory-sha256")
    parser.add_argument("--previous-inventory-path", type=Path)
    parser.add_argument("--previous-inventory-sha256")
    parser.add_argument("--block-url-base", default=DEFAULT_BLOCKS)
    parser.add_argument("--fallback-block-url-base")
    parser.add_argument("--peer-block-url-base")
    parser.add_argument("--peer-resolve")
    parser.add_argument("--peer-only", action="store_true")
    parser.add_argument("--peer-wait-seconds", type=int, default=0)
    parser.add_argument("--max-pages", type=int, default=20)
    parser.add_argument("--max-new-bytes", type=int, default=512_000_000)
    parser.add_argument("--max-block-bytes", type=int, default=256_000_000)
    parser.add_argument("--fetch-workers", type=int, default=4)
    parser.add_argument("--max-prefetch-bytes", type=int, default=256_000_000)
    parser.add_argument("--min-free-bytes", type=int, default=50_000_000_000)
    args = parser.parse_args()
    if (args.max_pages < 1 or args.max_new_bytes < 1 or args.max_block_bytes < 1 or
            args.fetch_workers < 1 or args.max_prefetch_bytes < 1 or args.min_free_bytes < 0 or
            args.peer_wait_seconds < 0):
        parser.error("batch budgets must be positive and disk reserve nonnegative")
    if args.peer_only and not local_listing(args.api_url):
        parser.error("peer-only mode requires a local inventory listing")
    args.state_dir.mkdir(parents=True, exist_ok=True)
    lock_path = args.state_dir / "replicate.lock"
    try:
        with lock_path.open("w") as lock:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ReplicationError("another lake replication is running")
            raw_store = RawBlockStore(args.raw_block_store) if args.raw_block_store else None
            kubo = (KuboRPC(args.ipfs_bin, args.kubo_api_url, raw_store=raw_store)
                    if args.kubo_api_url else Kubo(args.ipfs_bin, raw_store=raw_store))
            result = run_and_record_batch(args, kubo)
        print(json.dumps(result, sort_keys=True))
    except (ReplicationError, RawBlockStoreError, OSError, subprocess.TimeoutExpired) as exc:
        print("REFUSED: {}".format(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
