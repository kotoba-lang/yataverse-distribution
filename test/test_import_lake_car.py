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
    def test_pin_listing_requires_every_exact_inventory_cid(self):
        module.verified_pins(CID_A + " indirect through " + CID_B + "\n" +
                             CID_B + " direct\n", [CID_A, CID_B])
        for output in (CID_A + " indirect\n", CID_A + " direct\n",
                       CID_A + " direct\n" + CID_A + " direct\n"):
            with self.subTest(output=output), self.assertRaises(module.ImportError):
                module.verified_pins(output, [CID_A, CID_B])

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
