import hashlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

source = Path(__file__).resolve().parents[1] / "deploy" / "project_lake_log.py"
spec = importlib.util.spec_from_file_location("project_lake_log", source)
pl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pl)


def inv(d, name, rows):
    p = Path(d) / name
    p.write_text("".join(json.dumps({"cid": c, "bytes": b}) + "\n" for c, b in rows))
    return {"path": str(p), "sha256": hashlib.sha256(p.read_bytes()).hexdigest(), "rows": len(rows)}


class Projection(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.e0 = dict(inv(self.d, "e0", [("a", 1), ("b", 2), ("c", 3)]), epoch=0, inventory="cid0")
        self.e1 = dict(inv(self.d, "e1", [("d", 4), ("e", 5)]), epoch=1, inventory="cid1")
        self.head = {"seq": 1, "manifest": "m1", "bundle": "b1"}

    def test_pages_cover_every_row_in_order_and_head_is_last(self):
        objs = pl.build([self.e0, self.e1], self.head, page_rows=2)
        self.assertEqual(objs[-1][0], "lake-log/head.json")
        rows = [r["cid"] for k, v in objs[:-1] for r in json.loads(v)]
        self.assertEqual(rows, ["a", "b", "c", "d", "e"])
        head = json.loads(objs[-1][1])
        self.assertEqual(head["rows"], 5)
        self.assertEqual([e["rows"] for e in head["epochs"]], [3, 2])
        self.assertEqual(sorted(k for k, _ in objs[:-1]),
                         sorted([f"lake-log/pages/{self.e0['sha256']}/0.json", f"lake-log/pages/{self.e0['sha256']}/1.json",
                                 f"lake-log/pages/{self.e1['sha256']}/0.json"]))

    def test_refuses_an_inventory_that_is_not_the_manifests(self):
        with self.assertRaisesRegex(ValueError, "sha256"):
            pl.build([dict(self.e0, sha256="0" * 64), self.e1], self.head)
        with self.assertRaisesRegex(ValueError, "rows"):
            pl.build([dict(self.e0, rows=4), self.e1], self.head)

    def test_refuses_epochs_out_of_order_or_a_head_that_does_not_match(self):
        with self.assertRaisesRegex(ValueError, "log order"):
            pl.build([self.e1, self.e0], self.head)
        with self.assertRaisesRegex(ValueError, "head seq"):
            pl.build([self.e0, self.e1], dict(self.head, seq=2))

    def test_same_input_same_bytes(self):
        self.assertEqual(pl.build([self.e0, self.e1], self.head), pl.build([self.e0, self.e1], self.head))


if __name__ == "__main__":
    unittest.main()
