import importlib.util
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


source = Path(__file__).resolve().parents[1] / "deploy" / "replicate_lake.py"
spec = importlib.util.spec_from_file_location("replicate_lake", source)
replica = importlib.util.module_from_spec(spec)
spec.loader.exec_module(replica)

CID_A = "QmNLgJDagPKXUg4C1vKtvETQhTxEDScNYwEhYBke5uBan8"
CID_B = "QmNLhRArQBktFvoG2vgmoM3oMZC8vgWGhk9sM4Q4KkGUxh"


class FakeKubo:
    def __init__(self):
        self.pins = set()
        self.data = {}
        self.repo_size = 0
        self.storage_max = 1000

    def preflight(self):
        pass

    def block_size(self, cid):
        return len(self.data[cid])

    def put_verified(self, cid, data):
        self.data[cid] = data
        self.pins.add(cid)

    def require_disk_reserve(self, _size, _reserve):
        pass


def args(directory, budget=100, pages=10):
    return SimpleNamespace(
        state_dir=Path(directory), api_url="https://example.test/blocks",
        block_url_base="https://example.test/ipfs/", max_pages=pages,
        max_new_bytes=budget, max_block_bytes=100, min_free_bytes=0,
    )


def page(blocks, cursor=None):
    return {
        "ok": True,
        "blocks": [{"cid": cid, "size": len(data)} for cid, data in blocks],
        "cursor": cursor,
        "truncated?": cursor is not None,
    }


class ReplicationTests(unittest.TestCase):
    def test_preflight_recognizes_existing_recursive_root(self):
        with tempfile.TemporaryDirectory() as directory:
            node = replica.Kubo(shutil.which("true"))
            calls = []

            def run(*argv, data=None):
                calls.append(argv)
                if argv == ("pin", "ls", "--type=direct"):
                    return (CID_A + " direct\n").encode()
                if argv == ("pin", "ls", "--type=recursive"):
                    return (CID_B + " recursive\n").encode()
                if argv == ("repo", "stat"):
                    return ("RepoSize: 10\nStorageMax: 1000\nRepoPath: " + directory + "\n").encode()
                self.fail("unexpected Kubo call: " + repr(argv))

            node.run = run
            node.preflight()
            self.assertEqual({CID_A, CID_B}, node.pins)
            self.assertIn(("pin", "ls", "--type=recursive"), calls)

    def test_pin_add_race_accepts_confirmed_recursive_pin(self):
        node = replica.Kubo(shutil.which("true"))
        data = b"abc"
        calls = []

        def run(*argv, data=None):
            calls.append(argv)
            if argv[:2] == ("cid", "format"):
                return b"cidv0 dag-pb sha2-256 32\n"
            if argv[:2] == ("block", "put"):
                return (CID_A + "\n").encode()
            if argv[:2] == ("block", "get"):
                return b"abc"
            if argv[:2] == ("pin", "add"):
                raise replica.ReplicationError("already pinned recursively")
            if argv[:2] == ("pin", "ls"):
                return (CID_A + " recursive\n").encode()
            self.fail("unexpected Kubo call: " + repr(argv))

        node.run = run
        node.put_verified(CID_A, data)
        self.assertIn(("pin", "ls", "--type=all", CID_A), calls)
        self.assertIn(CID_A, node.pins)

    def test_pin_add_failure_requires_exact_durable_pin(self):
        for pin_listing in ((CID_A + " indirect\n").encode(), b"", (CID_B + " direct\n").encode()):
            with self.subTest(pin_listing=pin_listing):
                node = replica.Kubo(shutil.which("true"))

                def run(*argv, data=None):
                    if argv[:2] == ("cid", "format"):
                        return b"cidv0 dag-pb sha2-256 32\n"
                    if argv[:2] == ("block", "put"):
                        return (CID_A + "\n").encode()
                    if argv[:2] == ("block", "get"):
                        return b"abc"
                    if argv[:2] == ("pin", "add"):
                        raise replica.ReplicationError("pin command failed")
                    if argv[:2] == ("pin", "ls"):
                        return pin_listing
                    self.fail("unexpected Kubo call: " + repr(argv))

                node.run = run
                with self.assertRaisesRegex(replica.ReplicationError, "pin command failed"):
                    node.put_verified(CID_A, b"abc")
                self.assertNotIn(CID_A, node.pins)

    def test_physical_reserve_refuses_before_source_read(self):
        node = replica.Kubo(shutil.which("true"))
        node.repo_path = Path(tempfile.gettempdir())
        with patch.object(replica.shutil, "disk_usage", return_value=SimpleNamespace(free=100)):
            with self.assertRaisesRegex(replica.ReplicationError, "physical disk reserve"):
                node.require_disk_reserve(30, 50)

    def test_large_cidv1_uses_explicit_large_block_mode(self):
        cid = "bafkreihg6pmtrfuwpybthr6nrhrqkxke3tosiecp2rhosu7psuzaagpsyy"
        data = b"x" * (2 * 1024 * 1024 + 1)
        node = replica.Kubo(shutil.which("true"))
        calls = []

        def run(*argv, data=None):
            calls.append(argv)
            if argv[:2] == ("cid", "format"):
                return b"cidv1 raw sha2-256 32\n"
            if argv[:2] == ("block", "put"):
                return (cid + "\n").encode()
            if argv[:2] == ("block", "get"):
                return data_value
            return b""

        data_value = data
        node.run = run
        node.put_verified(cid, data)
        put = next(call for call in calls if call[:2] == ("block", "put"))
        self.assertIn("--allow-big-block", put)
        self.assertIn("--mhlen=32", put)
        self.assertIn(cid, node.pins)

    def test_curl_follows_https_cid_redirect_with_size_limit(self):
        with patch.object(replica, "command", return_value=b"block") as invoke:
            self.assertEqual(b"block", replica.curl("https://ipfs.example/ipfs/cid", 10))
        argv = invoke.call_args.args[0]
        self.assertIn("-fLsS", argv)
        self.assertEqual("=https", argv[argv.index("--proto-redir") + 1])
        self.assertEqual("10", argv[argv.index("--max-filesize") + 1])

    def test_budget_keeps_the_page_cursor_and_resumes(self):
        with tempfile.TemporaryDirectory() as directory:
            node = FakeKubo()
            listing = page([(CID_A, b"abc"), (CID_B, b"xyz")], "next")
            fetch_list = lambda _url, _cursor: listing
            fetch_block = lambda url, _limit: b"abc" if CID_A in url else b"xyz"
            first = replica.run_batch(args(directory, budget=3), node, fetch_list, fetch_block)
            self.assertEqual("byte-budget", first["status"])
            self.assertFalse(first["cursor_advanced"])
            self.assertEqual(1, json.loads((Path(directory) / "checkpoint.json").read_text())["new_blocks_total"])
            self.assertIsNone(json.loads((Path(directory) / "checkpoint.json").read_text())["cursor"])
            self.assertEqual({CID_A}, node.pins)

            second = replica.run_batch(args(directory, budget=3, pages=1), node, fetch_list, fetch_block)
            self.assertEqual("page-limit", second["status"])
            state = json.loads((Path(directory) / "checkpoint.json").read_text())
            self.assertEqual("next", state["cursor"])
            self.assertEqual(2, state["new_blocks_total"])
            self.assertEqual(2, len((Path(directory) / "receipts.jsonl").read_text().splitlines()))

    def test_complete_cycle_resets_cursor(self):
        with tempfile.TemporaryDirectory() as directory:
            node = FakeKubo()
            pages = {None: page([(CID_A, b"abc")], "next"),
                     "next": page([(CID_B, b"xyz")])}
            result = replica.run_batch(
                args(directory), node, lambda _url, cursor: pages[cursor],
                lambda url, _limit: b"abc" if CID_A in url else b"xyz",
            )
            self.assertEqual("cycle-complete", result["status"])
            state = json.loads((Path(directory) / "checkpoint.json").read_text())
            self.assertIsNone(state["cursor"])
            self.assertEqual(1, state["cycles"])
            self.assertEqual(2, state["pages_total"])
            self.assertEqual(6, state["new_bytes_total"])

    def test_source_size_mismatch_refuses_without_pin(self):
        with tempfile.TemporaryDirectory() as directory:
            node = FakeKubo()
            with self.assertRaisesRegex(replica.ReplicationError, "byte count"):
                replica.run_batch(args(directory), node,
                                  lambda _url, _cursor: page([(CID_A, b"abc")]),
                                  lambda _url, _limit: b"ab")
            self.assertFalse(node.pins)
            self.assertFalse((Path(directory) / "receipts.jsonl").exists())

    def test_listing_must_have_measured_blocks_and_truncation(self):
        with patch.object(replica, "curl", return_value=b'{"ok":true,"blocks":[]}'):
            with self.assertRaisesRegex(replica.ReplicationError, "bounded block page"):
                replica.fetch_listing("https://example.test/blocks", None)
        invalid = json.dumps({"ok": True, "blocks": [{"cid": CID_A, "size": 3}]}).encode()
        with patch.object(replica, "curl", return_value=invalid):
            with self.assertRaisesRegex(replica.ReplicationError, "truncation verdict"):
                replica.fetch_listing("https://example.test/blocks", None)


if __name__ == "__main__":
    unittest.main()
