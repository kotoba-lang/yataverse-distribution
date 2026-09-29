import json
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "deploy"))
import diff_lake_inventory as diff


CID_A = "QmNLgJDagPKXUg4C1vKtvETQhTxEDScNYwEhYBke5uBan8"
CID_B = "QmNLhRArQBktFvoG2vgmoM3oMZC8vgWGhk9sM4Q4KkGUxh"
CID_C = "QmNLi4aMV82xKkoUbhQEM3rD4HfPHffu2Ye8SLU6nFKEBE"


def write_rows(path, rows):
    path.write_text("".join(json.dumps({"cid": cid, "bytes": size},
                                       separators=(",", ":")) + "\n"
                            for cid, size in rows))


def completion(path, state, old_sha, rows):
    state.write_text(json.dumps({"schema": 1, "status": "complete",
                                 "output": str(path.resolve()),
                                 "old_sha256": old_sha,
                                 "sha256": diff.sha_file(path),
                                 "size": path.stat().st_size,
                                 "rows": len(rows),
                                 "bytes": sum(size for _, size in rows)}))


class DiffLakeInventoryTests(unittest.TestCase):
    def fixture(self, directory, new_rows=None):
        root = Path(directory)
        old_rows = [(CID_A, 3), (CID_C, 5)]
        new_rows = new_rows if new_rows is not None else [(CID_A, 3), (CID_B, 4), (CID_C, 5)]
        old, a, b = (root / name for name in ("old.jsonl", "a.jsonl", "b.jsonl"))
        sa, sb = root / "a.state.json", root / "b.state.json"
        write_rows(old, old_rows)
        write_rows(a, new_rows)
        write_rows(b, new_rows)
        old_sha = diff.sha_file(old)
        completion(a, sa, old_sha, new_rows)
        completion(b, sb, old_sha, new_rows)
        return SimpleNamespace(old_inventory=old, old_sha256=old_sha,
                               candidate_a=a, candidate_a_state=sa,
                               candidate_b=b, candidate_b_state=sb,
                               output=root / "delta.jsonl", receipt=root / "delta.receipt.json")

    def test_two_matching_walks_publish_only_new_cid(self):
        with tempfile.TemporaryDirectory() as directory:
            args = self.fixture(directory)
            record = diff.delta(args)
            self.assertEqual(1, record["delta_rows"])
            self.assertEqual(4, record["delta_bytes"])
            self.assertEqual([{"cid": CID_B, "bytes": 4}],
                             [json.loads(line) for line in args.output.read_text().splitlines()])
            self.assertEqual(record, json.loads(args.receipt.read_text()))

    def test_different_walks_refuse_without_delta(self):
        with tempfile.TemporaryDirectory() as directory:
            args = self.fixture(directory)
            rows = [(CID_A, 3), (CID_B, 6), (CID_C, 5)]
            write_rows(args.candidate_b, rows)
            completion(args.candidate_b, args.candidate_b_state, args.old_sha256, rows)
            with self.assertRaisesRegex(diff.DeltaError, "walks differ"):
                diff.delta(args)
            self.assertFalse(args.output.exists())

    def test_missing_prior_cid_refuses_without_delta(self):
        with tempfile.TemporaryDirectory() as directory:
            args = self.fixture(directory, [(CID_A, 3), (CID_B, 4)])
            with self.assertRaisesRegex(diff.DeltaError, "omits old CID"):
                diff.delta(args)
            self.assertFalse(args.output.exists())


if __name__ == "__main__":
    unittest.main()
