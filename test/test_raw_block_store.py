"""Malformed dag-pb blocks remain CID checked and durable outside Kubo pins."""

import base64
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


deploy = Path(__file__).resolve().parents[1] / "deploy"
sys.path.insert(0, str(deploy))
from raw_block_store import RawBlockStore, RawBlockStoreError

spec = importlib.util.spec_from_file_location("serve_lake", deploy / "serve_lake.py")
serve = importlib.util.module_from_spec(spec)
spec.loader.exec_module(serve)


def dag_pb_cid(data):
    return "b" + base64.b32encode(b"\x01\x70\x12\x20" + hashlib.sha256(data).digest()).decode().lower().rstrip("=")


def cid_v0(data):
    alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    value = int.from_bytes(b"\x12\x20" + hashlib.sha256(data).digest(), "big")
    result = ""
    while value:
        value, digit = divmod(value, 58)
        result = alphabet[digit] + result
    return result


class RawBlockStoreTests(unittest.TestCase):
    def test_cidv0_sha256_is_checked_too(self):
        data = b"invalid-pb-node"
        cid = cid_v0(data)
        self.assertTrue(cid.startswith("Qm"))
        with tempfile.TemporaryDirectory() as directory:
            store = RawBlockStore(directory)
            store.put(cid, data)
            self.assertEqual(data, store.read(cid))

    def test_undecodable_dag_pb_bytes_survive_and_serve_from_sidecar(self):
        data = b"\x0a\x03not-valid-dag-pb"
        cid = dag_pb_cid(data)
        with tempfile.TemporaryDirectory() as directory:
            store = RawBlockStore(Path(directory) / "raw")
            store.put(cid, data)
            store.put(cid, data)  # idempotent after a crash and replay
            self.assertEqual({cid}, store.cids())
            self.assertEqual(data, RawBlockStore(store.root).read(cid))
            inventory_file = Path(directory) / "inventory.jsonl"
            payload = (json.dumps({"cid": cid, "bytes": len(data)}) + "\n").encode()
            inventory_file.write_bytes(payload)
            inventory = serve.Inventory(inventory_file, hashlib.sha256(payload).hexdigest(), 1)
            blocks = serve.LocalBlocks("/not-used", directory, inventory, 100, raw_store=store)
            with patch.object(serve.subprocess, "run", side_effect=AssertionError("Kubo must not be called")):
                self.assertEqual(data, blocks.read(cid))

    def test_changed_bytes_metadata_and_cid_are_refused(self):
        data = b"\x0a\x03not-valid-dag-pb"
        cid = dag_pb_cid(data)
        with tempfile.TemporaryDirectory() as directory:
            store = RawBlockStore(Path(directory))
            with self.assertRaisesRegex(RawBlockStoreError, "CID differs"):
                store.put(cid, data + b"x")
            store.put(cid, data)
            path, meta = store._paths(cid)
            path.write_bytes(data + b"x")
            with self.assertRaisesRegex(RawBlockStoreError, "data differs"):
                store.read(cid)
            path.write_bytes(data)
            metadata = json.loads(meta.read_text())
            metadata["bytes"] += 1
            meta.write_text(json.dumps(metadata))
            with self.assertRaisesRegex(RawBlockStoreError, "data differs"):
                store.read(cid)
            meta.unlink()
            with self.assertRaisesRegex(RawBlockStoreError, "metadata missing"):
                store.read(cid)


if __name__ == "__main__":
    unittest.main()
