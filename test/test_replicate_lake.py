import importlib.util
import base64
import hashlib
import json
import shutil
import tempfile
import threading
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote
from unittest.mock import patch


source = Path(__file__).resolve().parents[1] / "deploy" / "replicate_lake.py"
sys.path.insert(0, str(source.parent))
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
        fetch_workers=4, max_prefetch_bytes=100,
    )


def page(blocks, cursor=None):
    return {
        "ok": True,
        "blocks": [{"cid": cid, "size": len(data)} for cid, data in blocks],
        "cursor": cursor,
        "truncated?": cursor is not None,
    }


class ReplicationTests(unittest.TestCase):
    def test_local_listing_uses_offset_cursor_without_public_limit(self):
        payload = json.dumps(page([(CID_A, b"abc")], "200")).encode()
        with patch.object(replica, "curl", return_value=payload) as fetch:
            listing = replica.fetch_listing("http://127.0.0.1:8090/api/v1/lake/blocks", "100")
        self.assertEqual(CID_A, listing["blocks"][0]["cid"])
        self.assertEqual("http://127.0.0.1:8090/api/v1/lake/blocks?cursor=100",
                         fetch.call_args.args[0])
        with patch.object(replica, "command") as invoke:
            with self.assertRaisesRegex(replica.ReplicationError,
                                        "HTTP source must be loopback or HTTPS"):
                replica.curl("http://example.test/blocks", 100)
        invoke.assert_not_called()
        with patch.object(replica, "command", return_value=b"ok") as invoke:
            self.assertEqual(b"ok", replica.curl(
                "http://127.0.0.1:8090/api/v1/lake/blocks", 10))
        argv = invoke.call_args.args[0]
        self.assertIn("-fsS", argv)
        self.assertEqual("0", argv[argv.index("--max-redirs") + 1])

    def test_legacy_cursor_migrates_to_exact_inventory_offset(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inventory = root / "inventory.jsonl"
            inventory.write_text("".join(json.dumps({"cid": cid, "bytes": 3}) + "\n"
                                         for cid in (CID_A, CID_B)))
            digest = hashlib.sha256(inventory.read_bytes()).hexdigest()
            token = "1-" + base64.urlsafe_b64encode(
                quote(json.dumps({"v": 1, "startAfter": "ipld/" + CID_A})).encode()
            ).decode().rstrip("=")
            state_path = root / "checkpoint.json"
            state = replica.state_load(state_path)
            state["cursor"] = token
            replica.state_save(state_path, state)
            old = state_path.read_bytes()
            local_args = SimpleNamespace(
                api_url="http://127.0.0.1:8090/api/v1/lake/blocks",
                inventory_path=inventory, inventory_sha256=digest)
            migrated = replica.prepare_listing_state(local_args, state_path, state)
            self.assertEqual("1", migrated["cursor"])
            self.assertEqual("local-inventory:" + digest, migrated["listing_source"])
            self.assertEqual(old, (root / "checkpoint.pre-local-inventory.json").read_bytes())
            self.assertEqual("1", replica.prepare_listing_state(
                local_args, state_path, replica.state_load(state_path))["cursor"])
            with self.assertRaisesRegex(replica.ReplicationError, "checkpoint inventory identity differs"):
                replica.prepare_listing_state(
                    SimpleNamespace(api_url=local_args.api_url, inventory_path=inventory,
                                    inventory_sha256="0" * 64), state_path,
                    replica.state_load(state_path))

    def test_migration_refuses_missing_cursor_cid_without_writing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inventory = root / "inventory.jsonl"
            inventory.write_text(json.dumps({"cid": CID_A, "bytes": 3}) + "\n")
            state_path = root / "checkpoint.json"
            state = replica.state_load(state_path)
            payload = quote(json.dumps({"v": 1, "startAfter": "ipld/" + CID_B}))
            state["cursor"] = "1-" + base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
            replica.state_save(state_path, state)
            old = state_path.read_bytes()
            with self.assertRaisesRegex(replica.ReplicationError,
                                        "legacy cursor CID absent from local inventory"):
                replica.prepare_listing_state(
                    SimpleNamespace(api_url="http://127.0.0.1:8090/api/v1/lake/blocks",
                                    inventory_path=inventory,
                                    inventory_sha256=hashlib.sha256(inventory.read_bytes()).hexdigest()),
                    state_path, state)
            self.assertEqual(old, state_path.read_bytes())
            self.assertFalse((root / "checkpoint.pre-local-inventory.json").exists())

    def test_new_inventory_rewinds_and_copies_interleaved_block(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old = root / "old.jsonl"
            new = root / "new.jsonl"
            old.write_text(json.dumps({"cid": CID_B, "bytes": 3}) + "\n")
            new.write_text("".join(json.dumps({"cid": cid, "bytes": 3}) + "\n"
                                   for cid in (CID_A, CID_B)))
            old_sha = hashlib.sha256(old.read_bytes()).hexdigest()
            new_sha = hashlib.sha256(new.read_bytes()).hexdigest()
            state_path = root / "checkpoint.json"
            state = replica.state_load(state_path)
            state.update(cursor=None, listing_source="local-inventory:" + old_sha,
                         new_blocks_total=1, new_bytes_total=3)
            replica.state_save(state_path, state)
            original = state_path.read_bytes()
            config = args(directory)
            config.api_url = "http://127.0.0.1:8090/api/v1/lake/blocks"
            config.inventory_path = new
            config.inventory_sha256 = new_sha
            config.previous_inventory_path = old
            config.previous_inventory_sha256 = old_sha
            node = FakeKubo()
            node.pins.add(CID_B)
            node.data[CID_B] = b"bbb"
            listing = page([(CID_A, b"aaa"), (CID_B, b"bbb")])
            listing["inventory-sha256"] = new_sha
            with patch.object(replica, "fetch_listing", return_value=listing):
                result = replica.run_batch(config, node,
                                           lambda _url, _cursor: listing,
                                           lambda _url, _limit: b"aaa")
            self.assertEqual("cycle-complete", result["status"])
            self.assertEqual(1, result["new_blocks"])
            self.assertEqual({CID_A, CID_B}, node.pins)
            self.assertEqual(original, (root / ("checkpoint.pre-inventory-" + old_sha + ".json")).read_bytes())
            migrated = replica.state_load(state_path)
            self.assertEqual("local-inventory:" + new_sha, migrated["listing_source"])
            self.assertEqual(2, migrated["new_blocks_total"])

    def test_new_inventory_refuses_omission_and_stale_reader_without_checkpoint_write(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old = root / "old.jsonl"
            new = root / "new.jsonl"
            old.write_text(json.dumps({"cid": CID_B, "bytes": 3}) + "\n")
            new.write_text(json.dumps({"cid": CID_A, "bytes": 3}) + "\n")
            old_sha = hashlib.sha256(old.read_bytes()).hexdigest()
            new_sha = hashlib.sha256(new.read_bytes()).hexdigest()
            state_path = root / "checkpoint.json"
            state = replica.state_load(state_path)
            state.update(cursor="0", listing_source="local-inventory:" + old_sha)
            replica.state_save(state_path, state)
            original = state_path.read_bytes()
            config = args(directory)
            config.api_url = "http://127.0.0.1:8090/api/v1/lake/blocks"
            config.inventory_path = new
            config.inventory_sha256 = new_sha
            config.previous_inventory_path = old
            config.previous_inventory_sha256 = old_sha
            with patch.object(replica, "fetch_listing") as listing:
                with self.assertRaisesRegex(replica.ReplicationError,
                                            "new inventory omits or changes previous CID"):
                    replica.prepare_listing_state(config, state_path, state)
            listing.assert_not_called()
            self.assertEqual(original, state_path.read_bytes())
            new.write_text("".join(json.dumps({"cid": cid, "bytes": 3}) + "\n"
                                   for cid in (CID_A, CID_B)))
            config.inventory_sha256 = hashlib.sha256(new.read_bytes()).hexdigest()
            with patch.object(replica, "fetch_listing", return_value={"inventory-sha256": old_sha}):
                with self.assertRaisesRegex(replica.ReplicationError,
                                            "local listing is not serving the new inventory"):
                    replica.prepare_listing_state(config, state_path, state)
            self.assertEqual(original, state_path.read_bytes())
            self.assertFalse((root / ("checkpoint.pre-inventory-" + old_sha + ".json")).exists())

    def test_local_page_identity_refuses_before_block_fetch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inventory = root / "inventory.jsonl"
            inventory.write_text(json.dumps({"cid": CID_A, "bytes": 3}) + "\n")
            digest = hashlib.sha256(inventory.read_bytes()).hexdigest()
            config = args(directory)
            config.api_url = "http://127.0.0.1:8090/api/v1/lake/blocks"
            config.inventory_path = inventory
            config.inventory_sha256 = digest
            node = FakeKubo()
            with self.assertRaisesRegex(replica.ReplicationError,
                                        "local listing inventory identity differs"):
                replica.run_batch(config, node,
                                  lambda _url, _cursor: page([(CID_A, b"abc")]),
                                  lambda _url, _limit: self.fail("block fetch reached"))
            self.assertEqual(set(), node.pins)

    def test_peer_first_source_and_peer_only_refusal(self):
        peer = "https://yataverse-data.example/ipfs/"
        args_peer = SimpleNamespace(
            block_url_base="https://public.example/ipfs/",
            peer_block_url_base=peer,
            peer_resolve="yataverse-data.example:443:192.168.1.16",
            peer_only=False)
        urls = []

        def public(url, limit):
            urls.append(("public", url, limit))
            return b"abc"

        def peer_fetch(url, limit, resolve=None, **options):
            urls.append(("peer", url, limit, resolve, options))
            return b"abc"

        with patch.object(replica, "curl", peer_fetch):
            data = replica.peer_first_block_fetcher(args_peer, public)(
                args_peer.block_url_base + CID_A, 3)
        self.assertEqual(b"abc", data)
        self.assertEqual("peer", urls[0][0])
        self.assertEqual(args_peer.peer_resolve, urls[0][3])
        self.assertEqual({"max_seconds": 12, "retries": 3}, urls[0][4])
        self.assertEqual(1, len(urls))
        with patch.object(replica, "command", return_value=b"abc") as invoke:
            replica.curl(peer + CID_A, 3, resolve=args_peer.peer_resolve,
                         max_seconds=12, retries=0)
        argv = invoke.call_args.args[0]
        self.assertIn("-fsS", argv)
        self.assertEqual("0", argv[argv.index("--max-redirs") + 1])

        with patch.object(replica, "curl", side_effect=replica.ReplicationError("peer down")):
            self.assertEqual(b"abc", replica.peer_first_block_fetcher(args_peer, public)(
                args_peer.block_url_base + CID_A, 3))
            args_peer.peer_only = True
            with self.assertRaisesRegex(replica.ReplicationError,
                                        "peer-only block source unavailable"):
                replica.peer_first_block_fetcher(args_peer, public)(
                    args_peer.block_url_base + CID_A, 3)
        args_peer.peer_wait_seconds = 30
        public_calls = len([entry for entry in urls if entry[0] == "public"])
        with (patch.object(replica, "curl", side_effect=[
                replica.ReplicationError("peer HTTP 404"), b"abc"]) as fetch,
              patch.object(replica.time, "monotonic", return_value=0),
              patch.object(replica.time, "sleep") as sleeper):
            self.assertEqual(b"abc", replica.peer_first_block_fetcher(args_peer, public)(
                args_peer.block_url_base + CID_A, 3))
        self.assertEqual(2, fetch.call_count)
        sleeper.assert_called_once_with(10)
        self.assertEqual(public_calls, len([entry for entry in urls if entry[0] == "public"]))
        with (patch.object(replica, "curl", side_effect=replica.ReplicationError("peer HTTP 404")),
              patch.object(replica.time, "monotonic", side_effect=[0, 31]),
              patch.object(replica.time, "sleep") as sleeper):
            with self.assertRaisesRegex(replica.ReplicationError, "peer HTTP 404"):
                replica.peer_first_block_fetcher(args_peer, public)(
                    args_peer.block_url_base + CID_A, 3)
        sleeper.assert_not_called()
        self.assertEqual(public_calls, len([entry for entry in urls if entry[0] == "public"]))

    def test_prefetch_overlaps_fetches_and_preserves_receipt_order(self):
        barrier = threading.Barrier(2, timeout=3)
        fetched = []

        def fetch(url, _limit):
            fetched.append(url.rsplit("/", 1)[-1])
            barrier.wait()
            return b"abc"

        with tempfile.TemporaryDirectory() as directory:
            node = FakeKubo()
            result = replica.run_batch(
                args(directory), node,
                lambda _url, _cursor: page([(CID_A, b"abc"), (CID_B, b"abc")]),
                fetch)
            self.assertEqual("cycle-complete", result["status"])
            self.assertEqual(2, result["new_blocks"])
            receipts = [json.loads(line) for line in
                        (Path(directory) / "receipts.jsonl").read_text().splitlines()]
            self.assertEqual([CID_A, CID_B], [r["cid"] for r in receipts])
            self.assertEqual({CID_A, CID_B}, set(fetched))

    def test_prefetch_respects_byte_budget_before_network(self):
        fetched = []
        with tempfile.TemporaryDirectory() as directory:
            node = FakeKubo()
            result = replica.run_batch(
                args(directory, budget=3), node,
                lambda _url, _cursor: page([(CID_A, b"abc"), (CID_B, b"abc")], "next"),
                lambda url, _limit: (fetched.append(url.rsplit("/", 1)[-1]) or b"abc"))
            self.assertEqual("byte-budget", result["status"])
            self.assertEqual([CID_A], fetched)
            self.assertIsNone(json.loads((Path(directory) / "checkpoint.json").read_text())["cursor"])

    def test_prefetch_memory_ceiling_refuses_before_network(self):
        fetched = []
        with self.assertRaisesRegex(replica.ReplicationError,
                                    "block exceeds prefetch memory budget"):
            list(replica.prefetch_ordered(
                [{"cid": CID_A, "size": 4}],
                lambda url, _limit: fetched.append(url), "https://example.test/",
                100, 2, 3))
        self.assertEqual([], fetched)

    def test_failed_prefetch_keeps_page_cursor(self):
        with tempfile.TemporaryDirectory() as directory:
            node = FakeKubo()

            def fetch(url, _limit):
                if url.endswith(CID_B):
                    raise replica.ReplicationError("source unavailable")
                return b"abc"

            with self.assertRaisesRegex(replica.ReplicationError, "source unavailable"):
                replica.run_batch(
                    args(directory), node,
                    lambda _url, _cursor: page([(CID_A, b"abc"), (CID_B, b"abc")], "next"),
                    fetch)
            self.assertIsNone(json.loads((Path(directory) / "checkpoint.json").read_text())["cursor"])

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
