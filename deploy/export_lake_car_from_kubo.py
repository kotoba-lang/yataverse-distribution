#!/usr/bin/env python3
"""Export exact inventory blocks from a local Kubo repo as a bounded CARv1."""

import argparse
import base64
import hashlib
import http.client
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
import urllib.parse
from pathlib import Path


SHA256 = re.compile(r"[0-9a-f]{64}\Z")
CID = re.compile(r"(?:Qm[1-9A-HJ-NP-Za-km-z]{44}|b[a-z2-7]{20,200})\Z")
BASE58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


class ExportError(Exception):
    pass


def local_rpc(url, ipfs_path):
    parsed = urllib.parse.urlsplit(url)
    if (parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "::1") or
            parsed.username or parsed.password or parsed.path not in ("", "/") or
            parsed.query or parsed.fragment or not parsed.port):
        raise ExportError("Kubo RPC must be an uncredentialed loopback HTTP endpoint")
    connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=60)
    try:
        connection.request("POST", "/api/v0/repo/stat")
        response = connection.getresponse()
        body = response.read(1_000_001)
        if response.status != 200 or len(body) > 1_000_000:
            raise ExportError("Kubo RPC repository check failed")
        repo = json.loads(body)
        if Path(repo.get("RepoPath", "")).resolve() != ipfs_path.resolve():
            raise ExportError("Kubo RPC uses a different repository")
        return connection
    except (OSError, http.client.HTTPException, ValueError, TypeError):
        connection.close()
        raise ExportError("Kubo RPC repository check failed")
    except ExportError:
        connection.close()
        raise


def rpc_block(connection, cid, expected_size):
    try:
        path = "/api/v0/block/get?" + urllib.parse.urlencode({"arg": cid, "offline": "true"})
        connection.request("POST", path)
        response = connection.getresponse()
        body = response.read(expected_size + 1)
        if response.status != 200 or len(body) != expected_size:
            raise ExportError("local Kubo RPC block unavailable or wrong size: " + cid)
        if response.read(1):
            raise ExportError("local Kubo RPC block exceeds inventory size: " + cid)
        return body
    except (OSError, http.client.HTTPException) as exc:
        raise ExportError("local Kubo RPC block unavailable: " + cid) from exc


def varint(value):
    if value < 0:
        raise ExportError("negative varint")
    result = bytearray()
    while value >= 128:
        result.append((value & 127) | 128)
        value >>= 7
    result.append(value)
    return bytes(result)


def read_varint(data, offset):
    value = shift = 0
    while offset < len(data):
        byte = data[offset]
        offset += 1
        value |= (byte & 127) << shift
        if byte < 128:
            return value, offset
        shift += 7
        if shift > 63:
            break
    raise ExportError("invalid CID varint")


def cid_bytes(cid):
    if not CID.fullmatch(cid):
        raise ExportError("invalid inventory CID")
    if cid.startswith("Qm"):
        number = 0
        for character in cid:
            number = number * 58 + BASE58.index(character)
        multihash = number.to_bytes((number.bit_length() + 7) // 8, "big")
        data = b"\x01\x70" + multihash
    else:
        encoded = cid[1:].upper()
        data = base64.b32decode(encoded + "=" * ((-len(encoded)) % 8))
    version, offset = read_varint(data, 0)
    codec, offset = read_varint(data, offset)
    hash_code, offset = read_varint(data, offset)
    hash_length, offset = read_varint(data, offset)
    if (version != 1 or codec not in (0x55, 0x70, 0x71, 0x129) or
            hash_code != 0x12 or hash_length != 32 or len(data) != offset + 32):
        raise ExportError("unsupported inventory CID")
    return data, data[offset:]


def cbor_length(major, length):
    if length < 24:
        return bytes([(major << 5) | length])
    if length < 256:
        return bytes([(major << 5) | 24, length])
    if length < 65536:
        return bytes([(major << 5) | 25]) + length.to_bytes(2, "big")
    if length < 4294967296:
        return bytes([(major << 5) | 26]) + length.to_bytes(4, "big")
    raise ExportError("CBOR item too large")


def car_header(cids):
    # DAG-CBOR map {"roots": [CID links], "version": 1}.
    roots = b"".join(b"\xd8\x2a" + cbor_length(2, len(cid) + 1) + b"\x00" + cid
                     for cid in cids)
    header = (b"\xa2\x65roots" + cbor_length(4, len(cids)) + roots +
              b"\x67version\x01")
    return varint(len(header)) + header


def atomic_json(path, value):
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


def sha_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def selected_rows(path, expected_sha, expected_count, start, max_blocks, max_bytes):
    digest = hashlib.sha256()
    rows = []
    count = total = 0
    with path.open("rb") as source:
        for line in source:
            digest.update(line)
            if start <= count < start + max_blocks:
                try:
                    row = json.loads(line)
                    cid, size = row["cid"], row["bytes"]
                except (ValueError, KeyError, TypeError) as exc:
                    raise ExportError("invalid inventory row") from exc
                if not isinstance(cid, str) or not CID.fullmatch(cid) or type(size) is not int:
                    raise ExportError("invalid inventory CID or size")
                if size > 8_000_000 or size < 1 or total + size + 1024 * (len(rows) + 1) > max_bytes:
                    if not rows:
                        raise ExportError("first block exceeds CAR bounds")
                elif len(rows) == count - start:
                    rows.append((cid, size))
                    total += size
            count += 1
    if digest.hexdigest() != expected_sha or count != expected_count:
        raise ExportError("inventory identity differs from dated snapshot")
    if not rows:
        raise ExportError("empty inventory range")
    return rows


def export(args):
    if (not SHA256.fullmatch(args.sha256) or args.count < 1 or args.start_row < 0 or
            not 1 <= args.max_blocks <= 1000 or not 1 <= args.max_bytes <= 536_870_912):
        raise ExportError("invalid inventory identity or bounds")
    if not args.ipfs_bin.is_file() or not args.ipfs_path.is_dir():
        raise ExportError("Kubo binary or repository is absent")
    rows = selected_rows(args.inventory, args.sha256, args.count, args.start_row,
                         args.max_blocks, args.max_bytes)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.receipt.parent.mkdir(parents=True, exist_ok=True)
    if args.receipt.exists():
        saved = json.loads(args.receipt.read_text())
        if (not args.output.is_file() or saved.get("status") != "complete" or
                saved.get("inventory-sha256") != args.sha256 or
                saved.get("start-row") != args.start_row or
                saved.get("end-row") != args.start_row + len(rows) or
                saved.get("blocks") != len(rows) or
                saved.get("sha256") != sha_file(args.output)):
            raise ExportError("saved CAR differs from receipt")
        return saved
    if args.output.exists():
        raise ExportError("CAR exists without receipt")
    if shutil.disk_usage(args.output.parent).free < 2 * args.max_bytes + args.min_free_bytes:
        raise ExportError("physical disk reserve would be crossed")
    env = dict(os.environ, IPFS_PATH=str(args.ipfs_path.resolve()))
    try:
        repo = subprocess.run([str(args.ipfs_bin), "repo", "stat"], capture_output=True,
                              env=env, timeout=30, check=True).stdout.decode()
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise ExportError("Kubo repository unavailable") from exc
    path = next((line.split(":", 1)[1].strip() for line in repo.splitlines()
                 if line.startswith("RepoPath:")), None)
    if path is None or Path(path).resolve() != args.ipfs_path.resolve():
        raise ExportError("Kubo daemon uses a different repository")
    rpc = local_rpc(args.rpc_url, args.ipfs_path) if args.rpc_url else None
    encoded = [cid_bytes(cid) for cid, _ in rows]
    temp = args.output.with_name(args.output.name + "." + uuid.uuid4().hex + ".partial")
    digest = hashlib.sha256()
    written = 0
    try:
        with temp.open("xb") as output:
            def write(data):
                nonlocal written
                written += len(data)
                if written > args.max_bytes:
                    raise ExportError("CAR exceeds byte budget")
                output.write(data)
                digest.update(data)
            write(car_header([cid for cid, _ in encoded]))
            for (source_cid, expected_size), (binary_cid, expected_digest) in zip(rows, encoded):
                if rpc:
                    data = rpc_block(rpc, source_cid, expected_size)
                else:
                    try:
                        result = subprocess.run([str(args.ipfs_bin), "--offline", "block", "get",
                                                 source_cid], capture_output=True, env=env,
                                                timeout=60, check=True)
                    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
                        raise ExportError("local Kubo block unavailable: " + source_cid) from exc
                    data = result.stdout
                if len(data) != expected_size or hashlib.sha256(data).digest() != expected_digest:
                    raise ExportError("local block differs from inventory CID or size")
                write(varint(len(binary_cid) + len(data)))
                write(binary_cid)
                write(data)
            output.flush()
            os.fsync(output.fileno())
        os.link(temp, args.output)
        temp.unlink()
        directory = os.open(args.output.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        receipt = {"status": "complete", "inventory-sha256": args.sha256,
                   "start-row": args.start_row, "end-row": args.start_row + len(rows),
                   "blocks": len(rows), "bytes": written, "sha256": digest.hexdigest(),
                   "source": "local-kubo-offline-rpc" if rpc else "local-kubo-offline"}
        atomic_json(args.receipt, receipt)
        return receipt
    finally:
        temp.unlink(missing_ok=True)
        if rpc:
            rpc.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--inventory", required=True, type=Path)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--count", required=True, type=int)
    parser.add_argument("--start-row", required=True, type=int)
    parser.add_argument("--max-blocks", required=True, type=int)
    parser.add_argument("--max-bytes", required=True, type=int)
    parser.add_argument("--ipfs-bin", required=True, type=Path)
    parser.add_argument("--ipfs-path", required=True, type=Path)
    parser.add_argument("--rpc-url")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--receipt", required=True, type=Path)
    parser.add_argument("--min-free-bytes", type=int, default=50_000_000_000)
    args = parser.parse_args()
    try:
        print(json.dumps(export(args), sort_keys=True))
        return 0
    except (ExportError, OSError, ValueError, KeyError) as exc:
        print("REFUSED: " + str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
