import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "deploy"))
import capture_lake_inventory as capture


CID_A = "QmNLgJDagPKXUg4C1vKtvETQhTxEDScNYwEhYBke5uBan8"
CID_B = "QmNLhRArQBktFvoG2vgmoM3oMZC8vgWGhk9sM4Q4KkGUxh"
CID_C = "QmNLi4aMV82xKkoUbhQEM3rD4HfPHffu2Ye8SLU6nFKEBE"


class CaptureLakeInventoryTests(unittest.TestCase):
    def setup_paths(self, directory):
        root = Path(directory)
        old = root / "old.jsonl"
        old.write_text("".join(json.dumps({"cid": cid, "bytes": size},
                                          separators=(",", ":")) + "\n"
                               for cid, size in ((CID_A, 3), (CID_C, 5))))
        args = SimpleNamespace(api_url="https://example.invalid/api/v1/lake/blocks",
                               output=root / "new.jsonl", state=root / "state.json",
                               old_inventory=old,
                               old_sha256=hashlib.sha256(old.read_bytes()).hexdigest(),
                               max_pages=1)
        return args

    def test_resume_truncates_uncheckpointed_page_and_requires_old_superset(self):
        with tempfile.TemporaryDirectory() as directory:
            args = self.setup_paths(directory)
            page1 = {"blocks": [{"cid": CID_A, "size": 3},
                                {"cid": CID_B, "size": 4}],
                     "cursor": "next", "truncated?": True}
            page2 = {"blocks": [{"cid": CID_C, "size": 5}],
                     "cursor": None, "truncated?": False}
            with patch.object(capture, "fetch_listing", return_value=page1) as fetch:
                self.assertEqual("in-progress", capture.capture(args)["status"])
                fetch.assert_called_once_with(args.api_url, None)
            with (Path(directory) / "new.jsonl.partial").open("ab") as file:
                file.write(b"incomplete page after crash")
            with patch.object(capture, "fetch_listing", return_value=page2) as fetch:
                self.assertEqual("complete", capture.capture(args)["status"])
                fetch.assert_called_once_with(args.api_url, "next")
            result = [json.loads(line) for line in args.output.read_text().splitlines()]
            self.assertEqual([CID_A, CID_B, CID_C], [row["cid"] for row in result])
            self.assertEqual(2, json.loads(args.state.read_text())["old_rows"])
            self.assertEqual("complete", capture.capture(args)["status"])

    def test_old_cid_missing_refuses_to_publish(self):
        with tempfile.TemporaryDirectory() as directory:
            args = self.setup_paths(directory)
            page = {"blocks": [{"cid": CID_A, "size": 3}],
                    "cursor": None, "truncated?": False}
            with patch.object(capture, "fetch_listing", return_value=page):
                with self.assertRaisesRegex(capture.CaptureError, "omits old CID"):
                    capture.capture(args)
            self.assertFalse(args.output.exists())

    def test_repeated_cid_refuses_before_checkpoint_advance(self):
        with tempfile.TemporaryDirectory() as directory:
            args = self.setup_paths(directory)
            page = {"blocks": [{"cid": CID_A, "size": 3},
                               {"cid": CID_A, "size": 3}],
                    "cursor": "next", "truncated?": True}
            with patch.object(capture, "fetch_listing", return_value=page):
                with self.assertRaisesRegex(capture.CaptureError, "order repeated"):
                    capture.capture(args)
            self.assertEqual(0, json.loads(args.state.read_text())["rows"])


if __name__ == "__main__":
    unittest.main()
