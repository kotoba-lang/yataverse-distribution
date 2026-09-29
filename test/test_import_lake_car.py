import hashlib
import importlib.util
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


source = Path(__file__).resolve().parents[1] / "deploy" / "import_lake_car.py"
sys.path.insert(0, str(source.parent))
spec = importlib.util.spec_from_file_location("import_lake_car", source)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

CID_A = "QmNLgJDagPKXUg4C1vKtvETQhTxEDScNYwEhYBke5uBan8"
CID_B = "QmNLhRArQBktFvoG2vgmoM3oMZC8vgWGhk9sM4Q4KkGUxh"


class ImportLakeCarTests(unittest.TestCase):
    def test_repository_identity_is_fast_and_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / "config").write_text(json.dumps({"Identity": {"PeerID": "peer-a"}}))
            (repo / "api").write_text("/ip4/127.0.0.1/tcp/5002\n")
            with patch.object(module, "kubo", return_value="peer-a\n") as kubo:
                env = module.verify_repo("/bin/ipfs", repo)
            self.assertEqual(str(repo.resolve()), env["IPFS_PATH"])
            kubo.assert_called_once_with("/bin/ipfs", env, "id", "-f", "<id>", timeout=10)

            with patch.object(module, "kubo", return_value="peer-b\n"):
                with self.assertRaisesRegex(module.ImportError, "different repository"):
                    module.verify_repo("/bin/ipfs", repo)
            (repo / "api").unlink()
            with self.assertRaisesRegex(module.ImportError, "identity is unavailable"):
                module.verify_repo("/bin/ipfs", repo)

    def test_root_must_preserve_every_exact_inventory_link(self):
        record = {"schema": 1, "inventory-sha256": "a" * 64,
                  "start-row": 0, "end-row": 2,
                  "links": [{"/": CID_A}, {"/": CID_B}]}
        module.verified_root(json.dumps(record), record)
        for changed in ({**record, "links": [{"/": CID_A}]},
                        {**record, "inventory-sha256": "b" * 64}):
            with self.subTest(changed=changed), self.assertRaises(module.ImportError):
                module.verified_root(json.dumps(changed), record)

    def test_car_digest_mismatch_refuses_before_kubo(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inventory = root / "inventory.jsonl"
            data = (json.dumps({"cid": CID_A, "bytes": 3}) + "\n").encode()
            inventory.write_bytes(data)
            car = root / "lake.car"
            car.write_bytes(b"not-the-exported-car")
            args = SimpleNamespace(
                inventory=inventory, sha256=hashlib.sha256(data).hexdigest(),
                count=1, start_row=0, max_blocks=1, car=car,
                car_sha256="0" * 64, ipfs_bin=Path(shutil.which("true")),
                ipfs_path=root, min_free_bytes=0, receipt=None)
            with patch.object(module, "kubo") as kubo, self.assertRaisesRegex(
                    module.ImportError, "CAR SHA-256 differs"):
                module.import_batch(args)
            kubo.assert_not_called()


if __name__ == "__main__":
    unittest.main()
