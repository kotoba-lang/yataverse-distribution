import hashlib
import http.client
import importlib.util
import json
import shutil
import tempfile
import threading
import sys
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


source = Path(__file__).resolve().parents[1] / "deploy" / "serve_lake.py"
sys.path.insert(0, str(source.parent))
spec = importlib.util.spec_from_file_location("serve_lake", source)
serve = importlib.util.module_from_spec(spec)
spec.loader.exec_module(serve)

CID_A = "QmNLgJDagPKXUg4C1vKtvETQhTxEDScNYwEhYBke5uBan8"
CID_B = "QmNLhRArQBktFvoG2vgmoM3oMZC8vgWGhk9sM4Q4KkGUxh"


class ServingTests(unittest.TestCase):
    def inventory(self, directory):
        path = Path(directory) / "inventory.jsonl"
        rows = [{"cid": CID_A, "bytes": 3}, {"cid": CID_B, "bytes": 3}]
        payload = b"".join((json.dumps(row) + "\n").encode() for row in rows)
        path.write_bytes(payload)
        return serve.Inventory(path, hashlib.sha256(payload).hexdigest(), 2)

    def test_inventory_refuses_bad_digest_and_duplicate_cid(self):
        with tempfile.TemporaryDirectory() as directory:
            inventory = self.inventory(directory)
            with self.assertRaisesRegex(serve.InventoryError, "digest or row count"):
                serve.Inventory(inventory.path, "0" * 64, 2)
            inventory.path.write_text(json.dumps({"cid": CID_A, "bytes": 3}) + "\n" +
                                      json.dumps({"cid": CID_A, "bytes": 3}) + "\n")
            with self.assertRaisesRegex(serve.InventoryError, "duplicate CID"):
                serve.Inventory(inventory.path, hashlib.sha256(inventory.path.read_bytes()).hexdigest(), 2)

    def test_listing_refuses_inventory_file_replacement_after_startup(self):
        with tempfile.TemporaryDirectory() as directory:
            inventory = self.inventory(directory)
            replacement = Path(directory) / "replacement.jsonl"
            replacement.write_bytes(inventory.path.read_bytes())
            replacement.replace(inventory.path)
            with self.assertRaisesRegex(serve.InventoryError, "changed since startup"):
                inventory.page(0)

    def test_listing_has_bounded_cursor_and_original_size(self):
        with tempfile.TemporaryDirectory() as directory:
            inventory = self.inventory(directory)
            with patch.object(serve, "PAGE_SIZE", 1):
                first = inventory.page(0)
                last = inventory.page(int(first["cursor"]))
            self.assertEqual([{"cid": CID_A, "size": 3}], first["blocks"])
            self.assertEqual(inventory.sha256, first["inventory-sha256"])
            self.assertEqual("1", first["cursor"])
            self.assertTrue(first["truncated?"])
            self.assertEqual([{"cid": CID_B, "size": 3}], last["blocks"])
            self.assertFalse(last["truncated?"])
            with self.assertRaisesRegex(serve.InventoryError, "cursor outside"):
                inventory.page(2)

    def test_block_requires_exact_pin_and_measured_size(self):
        with tempfile.TemporaryDirectory() as directory:
            inventory = self.inventory(directory)
            with self.assertRaisesRegex(serve.InventoryError, "block byte limit"):
                serve.LocalBlocks(shutil.which("true"), directory, inventory, 256_000_001)
            blocks = serve.LocalBlocks(shutil.which("true"), directory, inventory, 8)
            direct = SimpleNamespace(returncode=0, stdout=(CID_A + " direct\n").encode())
            body = SimpleNamespace(returncode=0, stdout=b"abc")
            with patch.object(serve.subprocess, "run", side_effect=[direct, body]) as invoke:
                self.assertEqual(b"abc", blocks.read(CID_A))
            self.assertIn("--offline", invoke.call_args_list[1].args[0])
            indirect = SimpleNamespace(returncode=0, stdout=(CID_A + " indirect\n").encode())
            with patch.object(serve.subprocess, "run", return_value=indirect):
                with self.assertRaisesRegex(serve.InventoryError, "not directly or recursively pinned"):
                    blocks.read(CID_A)
            with patch.object(serve.subprocess, "run", side_effect=[direct, SimpleNamespace(returncode=0, stdout=b"ab")]):
                with self.assertRaisesRegex(serve.InventoryError, "size differs"):
                    blocks.read(CID_A)

    def test_http_serves_snapshot_and_refuses_bad_cursor(self):
        with tempfile.TemporaryDirectory() as directory:
            inventory = self.inventory(directory)
            blocks = SimpleNamespace(read=lambda _cid: b"abc")
            server = ThreadingHTTPServer(("127.0.0.1", 0), serve.handler_for(inventory, blocks))
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                connection = http.client.HTTPConnection("127.0.0.1", server.server_port)
                connection.request("GET", "/api/v1/lake/blocks")
                response = connection.getresponse()
                page = json.loads(response.read())
                self.assertEqual(200, response.status)
                self.assertEqual(2, len(page["blocks"]))
                self.assertEqual(inventory.sha256, page["inventory-sha256"])
                connection.request("GET", "/api/v1/lake/blocks?cursor=-1")
                response = connection.getresponse()
                response.read()
                self.assertEqual(404, response.status)
                connection.request("GET", "/ipfs/" + CID_A)
                response = connection.getresponse()
                self.assertEqual(b"abc", response.read())
                self.assertEqual(200, response.status)
                connection.close()
            finally:
                server.shutdown()
                server.server_close()
                worker.join(timeout=2)

    def test_second_large_read_is_refused_while_first_is_active(self):
        with tempfile.TemporaryDirectory() as directory:
            inventory = self.inventory(directory)
            inventory.sizes[CID_A] = serve.LARGE_BLOCK_THRESHOLD + 1
            entered = threading.Event()
            release = threading.Event()
            first = {}

            def read(_cid):
                entered.set()
                if not release.wait(5):
                    raise RuntimeError("test reader did not unblock")
                return b"abc"

            server = ThreadingHTTPServer(("127.0.0.1", 0), serve.handler_for(inventory, SimpleNamespace(read=read)))
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()

            def first_request():
                conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
                try:
                    conn.request("GET", "/ipfs/" + CID_A)
                    response = conn.getresponse()
                    first["status"] = response.status
                    first["body"] = response.read()
                finally:
                    conn.close()

            reader = threading.Thread(target=first_request, daemon=True)
            reader.start()
            try:
                self.assertTrue(entered.wait(2))
                conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
                try:
                    conn.request("GET", "/ipfs/" + CID_A)
                    response = conn.getresponse()
                    response.read()
                    self.assertEqual(503, response.status)
                    self.assertIn("large block reader busy", response.reason)
                finally:
                    conn.close()
                release.set()
                reader.join(timeout=5)
                self.assertEqual({"status": 200, "body": b"abc"}, first)
            finally:
                release.set()
                server.shutdown()
                server.server_close()
                worker.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
