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

    def test_cutoff_skips_newer_key_without_losing_cursor_order(self):
        with tempfile.TemporaryDirectory() as directory:
            args = self.setup_paths(directory)
            args.cutoff_utc = "2026-09-28T00:00:00Z"
            earlier = "Sun Sep 27 2026 00:00:00 GMT+0000 (Coordinated Universal Time)"
            later = "Tue Sep 29 2026 00:00:00 GMT+0000 (Coordinated Universal Time)"
            page1 = {"blocks": [{"cid": CID_A, "size": 3, "uploaded": earlier},
                                {"cid": CID_B, "size": 4, "uploaded": later}],
                     "cursor": "next", "truncated?": True}
            page2 = {"blocks": [{"cid": CID_C, "size": 5, "uploaded": earlier}],
                     "cursor": None, "truncated?": False}
            with patch.object(capture, "fetch_listing", return_value=page1):
                capture.capture(args)
            state = json.loads(args.state.read_text())
            self.assertEqual(CID_A, state["last_cid"])
            self.assertEqual(CID_B, state["last_seen_cid"])
            with patch.object(capture, "fetch_listing", return_value=page2):
                self.assertEqual("complete", capture.capture(args)["status"])
            self.assertEqual([CID_A, CID_C],
                             [json.loads(line)["cid"] for line in args.output.read_text().splitlines()])


if __name__ == "__main__":
    unittest.main()
