import hashlib
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


source = Path(__file__).resolve().parents[1] / "deploy" / "import_large_lake_batch.py"
sys.path.insert(0, str(source.parent))
spec = importlib.util.spec_from_file_location("import_large_lake_batch", source)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

CID = "bafkreia25gfj7zq7tvyky74u6groieu4hsufrnucop4klxopa6lej5vute"


class LargeBatchTests(unittest.TestCase):
    def fixture(self, directory):
        folder = Path(directory)
        inventory = folder / "inventory.jsonl"
        data = (json.dumps({"cid": CID, "bytes": 8_000_001}) + "\n").encode()
        inventory.write_bytes(data)
        sha = hashlib.sha256(data).hexdigest()
        args = SimpleNamespace(
            inventory=inventory, sha256=sha, count=1,
            expected_large_count=1, expected_large_bytes=8_000_001,
            archive=folder / "archive", receipt_dir=folder / "receipts",
            state_dir=folder / "state", importer=folder / "importer.py",
            ipfs_bin=folder / "ipfs", ipfs_path=folder / "repo",
            min_free_bytes=0, max_new=1)
        return args

    def test_plan_requires_exact_count_and_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            args = self.fixture(directory)
            inventory = module.Inventory(args.inventory, args.sha256, args.count)
            self.assertEqual([(0, CID, 8_000_001)], module.plan(inventory, 1, 8_000_001))
            with self.assertRaisesRegex(module.BatchError, "plan differs"):
                module.plan(inventory, 1, 8_000_002)

    def test_missing_source_waits_without_advancing_checkpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            args = self.fixture(directory)
            result = module.advance(args)
            self.assertEqual({"status": "waiting-source", "row": 0, "cursor": 0,
                              "imported": 0, "skipped": 0}, result)
            self.assertFalse((args.state_dir / "checkpoint.json").exists())


if __name__ == "__main__":
    unittest.main()
