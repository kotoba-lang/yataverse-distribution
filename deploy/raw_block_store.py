"""Durable CID-checked raw blocks for bytes Kubo cannot decode and pin."""

import base64
import hashlib
import json
import os
import re
import uuid
from pathlib import Path


CID_V0 = re.compile(r"^Qm[1-9A-HJ-NP-Za-km-z]{44}$")
CID_V1 = re.compile(r"^b[a-z2-7]{20,200}$")
BASE58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
MAX_BLOCK = 256_000_000


class RawBlockStoreError(Exception):
    pass


def _varint(data, offset):
    value = 0
    for shift in range(0, 70, 7):
        if offset >= len(data):
            raise RawBlockStoreError("truncated CID varint")
        byte = data[offset]
        offset += 1
        value |= (byte & 0x7f) << shift
        if not byte & 0x80:
            return value, offset
    raise RawBlockStoreError("oversized CID varint")


def _sha256_digest(cid):
    if CID_V0.fullmatch(cid):
        value = 0
        for char in cid:
            value = value * 58 + BASE58.index(char)
        encoded = value.to_bytes((value.bit_length() + 7) // 8, "big")
        encoded = b"\x00" * (len(cid) - len(cid.lstrip("1"))) + encoded
    elif CID_V1.fullmatch(cid):
        text = cid[1:].upper()
        try:
            encoded = base64.b32decode(text + "=" * ((8 - len(text) % 8) % 8))
        except ValueError as exc:
            raise RawBlockStoreError("invalid CID base32") from exc
        version, offset = _varint(encoded, 0)
        if version != 1:
            raise RawBlockStoreError("unsupported CID version")
        _, offset = _varint(encoded, offset)  # codec: identity is already in CID
        encoded = encoded[offset:]
    else:
        raise RawBlockStoreError("invalid CID syntax")
    code, offset = _varint(encoded, 0)
    length, offset = _varint(encoded, offset)
    if code != 0x12 or length != 32 or len(encoded) - offset != 32:
        raise RawBlockStoreError("raw store requires a sha2-256 CID")
    return encoded[offset:]


def verify_cid(cid, data):
    if hashlib.sha256(data).digest() != _sha256_digest(cid):
        raise RawBlockStoreError("raw block CID differs from bytes")


def _sync_directory(path):
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class RawBlockStore:
    def __init__(self, root):
        self.root = Path(root)

    def _paths(self, cid):
        _sha256_digest(cid)
        shard = self.root / hashlib.sha256(cid.encode("ascii")).hexdigest()[:2]
        return shard / (cid + ".block"), shard / (cid + ".json")

    def cids(self):
        if not self.root.exists():
            return set()
        result = set()
        for meta in self.root.glob("*/*.json"):
            cid = meta.stem
            _, expected = self._paths(cid)
            if meta != expected:
                raise RawBlockStoreError("raw block metadata path differs")
            result.add(cid)
        return result

    def read(self, cid, max_bytes=MAX_BLOCK):
        data_path, meta_path = self._paths(cid)
        if not data_path.exists() and not meta_path.exists():
            return None
        if not data_path.is_file() or not meta_path.is_file() or data_path.is_symlink() or meta_path.is_symlink():
            raise RawBlockStoreError("raw block data or metadata missing")
        try:
            meta = json.loads(meta_path.read_text())
        except (OSError, ValueError) as exc:
            raise RawBlockStoreError("raw block metadata unreadable") from exc
        if (meta.get("schema") != 1 or meta.get("cid") != cid or
                type(meta.get("bytes")) is not int or meta["bytes"] < 1 or
                meta["bytes"] > max_bytes or
                not isinstance(meta.get("sha256"), str) or
                not re.fullmatch(r"[0-9a-f]{64}", meta["sha256"])):
            raise RawBlockStoreError("raw block metadata differs")
        with data_path.open("rb") as source:
            data = source.read(max_bytes + 1)
        if (len(data) != meta["bytes"] or
                hashlib.sha256(data).hexdigest() != meta["sha256"]):
            raise RawBlockStoreError("raw block data differs from metadata")
        verify_cid(cid, data)
        return data

    def put(self, cid, data):
        if not data or len(data) > MAX_BLOCK:
            raise RawBlockStoreError("raw block size exceeds bound")
        verify_cid(cid, data)
        data_path, meta_path = self._paths(cid)
        if meta_path.exists():
            if self.read(cid) != data:
                raise RawBlockStoreError("existing raw block differs")
            return
        if data_path.exists() and data_path.read_bytes() != data:
            raise RawBlockStoreError("incomplete raw block differs")
        root_existed = self.root.exists()
        self.root.mkdir(parents=True, exist_ok=True)
        if not root_existed:
            _sync_directory(self.root.parent)
        data_path.parent.mkdir(exist_ok=True)
        _sync_directory(self.root)
        if not data_path.exists():
            temp = data_path.with_name(data_path.name + "." + uuid.uuid4().hex + ".tmp")
            try:
                with temp.open("xb") as target:
                    target.write(data)
                    target.flush()
                    os.fsync(target.fileno())
                os.replace(temp, data_path)
                _sync_directory(data_path.parent)
            finally:
                temp.unlink(missing_ok=True)
        meta = {"schema": 1, "cid": cid, "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest()}
        temp = meta_path.with_name(meta_path.name + "." + uuid.uuid4().hex + ".tmp")
        try:
            with temp.open("x") as target:
                json.dump(meta, target, sort_keys=True)
                target.write("\n")
                target.flush()
                os.fsync(target.fileno())
            os.replace(temp, meta_path)
            _sync_directory(meta_path.parent)
        finally:
            temp.unlink(missing_ok=True)
