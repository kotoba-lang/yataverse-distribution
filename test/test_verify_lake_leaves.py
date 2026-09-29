import base64
import hashlib
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


source = Path(__file__).resolve().parents[1] / "deploy" / "verify_lake_leaves.py"
sys.path.insert(0, str(source.parent))
spec = importlib.util.spec_from_file_location("verify_lake_leaves", source)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def cid(data):
    encoded = b"\x01\x55\x12\x20" + hashlib.sha256(data).digest()
    return "b" + base64.b32encode(encoded).decode().lower().rstrip("=")


class Reader:
    def __init__(self, blocks):
        self.blocks = blocks

    def read(self, name, _size):
        return self.blocks.get(name)


class LeafReadbackTests(unittest.TestCase):
    def inventory(self, directory, blocks):
        path = Path(directory) / "inventory.jsonl"
        payload = b"".join(json.dumps({"cid": cid(data), "bytes": len(data)}).encode() +
                           b"\n" for data in blocks)
        path.write_bytes(payload)
        return module.Inventory(path, hashlib.sha256(payload).hexdigest(), len(blocks))

    def test_resume_counts_present_and_missing_without_claiming_custody(self):
        with tempfile.TemporaryDirectory() as directory:
            inventory = self.inventory(directory, [b"one", b"two", b"three"])
            state_path = Path(directory) / "readback.json"
            state = module.fresh_state(inventory, Path(directory), "peer-a")
            first = module.advance(inventory, state, Reader({cid(b"one"): b"one"}),
                                   2, 100, stop_at_missing=False)
            self.assertEqual((2, 1, 1, "partial"),
                             (first["cursor"], first["present_rows"], first["missing_rows"], first["status"]))
            first.pop("status")
            module.save_state(state_path, first)
            resumed = module.load_state(state_path, inventory, Path(directory), "peer-a")
            second = module.advance(inventory, resumed, Reader({cid(b"three"): b"three"}),
                                    2, 100, stop_at_missing=False)
            self.assertEqual((3, 2, 1, 8, 3, "partial"),
                             (second["cursor"], second["present_rows"], second["missing_rows"],
                              second["present_bytes"], second["missing_bytes"], second["status"]))
            self.assertNotEqual(first["scan_sha256"], second["scan_sha256"])

    def test_default_waits_at_first_missing_and_can_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            inventory = self.inventory(directory, [b"one", b"two", b"three"])
            state = module.fresh_state(inventory, Path(directory), "peer-a")
            first = module.advance(inventory, state, Reader({cid(b"one"): b"one"}), 3, 100)
            self.assertEqual((1, 1, 0, "waiting"),
                             (first["cursor"], first["present_rows"], first["missing_rows"], first["status"]))
            first.pop("status")
            second = module.advance(inventory, first, Reader({cid(b"two"): b"two",
                                                               cid(b"three"): b"three"}), 3, 100)
            self.assertEqual((3, 3, "complete"),
                             (second["cursor"], second["present_rows"], second["status"]))

    def test_wrong_cid_or_size_refuses_without_progress(self):
        with tempfile.TemporaryDirectory() as directory:
            inventory = self.inventory(directory, [b"one"])
            state = module.fresh_state(inventory, Path(directory), "peer-a")
            with self.assertRaisesRegex(module.ReadbackError, "CID differs"):
                module.advance(inventory, state, Reader({cid(b"one"): b"two"}), 1, 100)
            with self.assertRaisesRegex(module.ReadbackError, "size differs"):
                module.advance(inventory, state, Reader({cid(b"one"): b"four"}), 1, 100)
            self.assertEqual(0, state["cursor"])

    def test_repository_and_checkpoint_identity_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / "config").write_text(json.dumps({"Identity": {"PeerID": "peer-a"}}))
            (repo / "api").write_text("/ip4/127.0.0.1/tcp/5002\n")
            response = SimpleNamespace(returncode=0, stdout=b"peer-a\n")
            with patch.object(module.subprocess, "run", return_value=response) as run:
                self.assertEqual(("peer-a", 5002), module.verify_repository(
                    Path("/bin/ipfs"), repo, "http://127.0.0.1:5002"))
            self.assertEqual(10, run.call_args.kwargs["timeout"])
            with self.assertRaisesRegex(module.ReadbackError, "loopback"):
                module.verify_repository(Path("/bin/ipfs"), repo, "http://example.com:5002")
            (repo / "api").write_text("/ip4/127.0.0.1/tcp/5001\n")
            with self.assertRaisesRegex(module.ReadbackError, "API or identity differs"):
                module.verify_repository(Path("/bin/ipfs"), repo, "http://127.0.0.1:5002")

            inventory = self.inventory(directory, [b"one"])
            path = repo / "readback.json"
            state = module.fresh_state(inventory, repo, "peer-a")
            state["cursor"] = 1
            path.write_text(json.dumps(state))
            with self.assertRaisesRegex(module.ReadbackError, "checkpoint differs"):
                module.load_state(path, inventory, repo, "peer-a")


if __name__ == "__main__":
    unittest.main()
