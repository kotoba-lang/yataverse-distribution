import hashlib
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


source = Path(__file__).resolve().parents[1] / "deploy" / "import_large_lake_recovery.py"
sys.path.insert(0, str(source.parent))
spec = importlib.util.spec_from_file_location("import_large_lake_recovery", source)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

CID = "bafkreia25gfj7zq7tvyky74u6groieu4hsufrnucop4klxopa6lej5vute"
BLOCK_SHA = "1ae98a9fe61f9d70ac7f94f1a2e4129c3ca858b68273f8a5ddcf079644f6b499"
ROOT = "bafybeic4uk6d2ose5wyadez6df4vtfnj7ypghxgrrwo5f2eddashia4gma"


class LargeRecoveryTests(unittest.TestCase):
    def fixture(self, directory):
        folder = Path(directory)
        inventory = folder / "inventory.jsonl"
        data = (json.dumps({"cid": CID, "bytes": 203_519_718}) + "\n").encode()
        inventory.write_bytes(data)
        sha = hashlib.sha256(data).hexdigest()
        car = folder / "recovery.car"
        car.write_bytes(b"test-car")
        receipt = folder / "recovery.car.json"
        record = {"schema": 1, "row": 0, "inventory_sha256": sha,
                  "original_cid": CID, "original_bytes": 203_519_718,
                  "original_sha256": BLOCK_SHA, "recovery_root": ROOT,
                  "car_bytes": 8, "car_sha256": hashlib.sha256(b"test-car").hexdigest(),
                  "validation": "fresh-offline-kubo-import-cat-and-original-cid-rehydration"}
        receipt.write_text(json.dumps(record))
        args = SimpleNamespace(inventory=inventory, sha256=sha, count=1, row=0,
                               car=car, source_receipt=receipt)
        return args, record

    def test_source_receipt_binds_car_and_inventory(self):
        with tempfile.TemporaryDirectory() as directory:
            args, record = self.fixture(directory)
            inventory = module.Inventory(args.inventory, args.sha256, args.count)
            self.assertEqual(record, module.receipt_matches(args, inventory))
            args.car.write_bytes(b"tampered!")
            with self.assertRaisesRegex(module.RecoveryError, "CAR differs"):
                module.receipt_matches(args, inventory)

    def test_wrong_inventory_row_receipt_refuses(self):
        with tempfile.TemporaryDirectory() as directory:
            args, record = self.fixture(directory)
            record["original_bytes"] += 1
            args.source_receipt.write_text(json.dumps(record))
            inventory = module.Inventory(args.inventory, args.sha256, args.count)
            with self.assertRaisesRegex(module.RecoveryError, "differs from immutable inventory"):
                module.receipt_matches(args, inventory)


if __name__ == "__main__":
    unittest.main()
