import base64
import hashlib
import io
import json
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import patch
from types import SimpleNamespace
import urllib.error
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "deploy"))
import r2_origin_bridge as bridge
import replicate_lake as replica


BODY = b"verified lake block"
CID = "b" + base64.b32encode(bytes((1, 0x55, 0x12, 0x20)) + hashlib.sha256(BODY).digest()).decode().lower().rstrip("=")


class Token:
    def __init__(self):
        self.refreshes = 0

    def get(self, refresh=False):
        self.refreshes += int(refresh)
        return "test-token"


class Response:
    status = 200

    def __init__(self, body):
        self.body = io.BytesIO(body)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def read(self, size):
        return self.body.read(size)


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.token = Token()
        self.origin = bridge.Origin({CID: len(BODY)}, "0" * 32, "example-bucket", self.token)

    def test_listed_cid_and_bytes_are_verified(self):
        with patch.object(bridge, "_open", return_value=Response(BODY)) as fetch:
            self.assertEqual(BODY, self.origin.fetch(CID))
        self.assertIn("/objects/ipld/" + CID, fetch.call_args.args[0].full_url)
        self.assertEqual(0, self.origin.budget.used)

    def test_unlisted_cid_is_not_sent_to_r2(self):
        with patch.object(bridge, "_open") as fetch:
            with self.assertRaises(KeyError):
                self.origin.fetch("b" + "a" * 60)
        fetch.assert_not_called()

    def test_corrupt_and_oversized_responses_fail_closed(self):
        for body in (b"x" * len(BODY), BODY + b"x"):
            with self.subTest(body=body), patch.object(bridge, "_open", return_value=Response(body)):
                with self.assertRaisesRegex(IOError, "CID|size"):
                    self.origin.fetch(CID)
                self.assertEqual(0, self.origin.budget.used)

    def test_expired_oauth_refreshes_once(self):
        error = urllib.error.HTTPError("https://example.invalid", 401, "unauthorized", {}, io.BytesIO())
        with patch.object(bridge, "_open", side_effect=[error, Response(BODY)]):
            self.assertEqual(BODY, self.origin.fetch(CID))
        self.assertEqual(1, self.token.refreshes)

    def test_memory_ceiling_refuses_large_claim(self):
        self.origin.budget.limit = len(BODY) - 1
        with self.assertRaisesRegex(ValueError, "memory budget"):
            self.origin.fetch(CID)
        self.assertEqual(0, self.origin.budget.used)

    def test_health_checks_a_real_origin_block_and_refuses_failure(self):
        server = bridge.ThreadingHTTPServer(("127.0.0.1", 0), bridge.handler_for(self.origin))
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = "http://127.0.0.1:{}/healthz".format(server.server_port)
        try:
            with patch.object(self.origin, "fetch", side_effect=IOError("source unavailable")):
                with self.assertRaises(urllib.error.HTTPError) as failure:
                    urllib.request.urlopen(url, timeout=2)
                self.assertEqual(503, failure.exception.code)
                failure.exception.close()
            with patch.object(self.origin, "fetch", return_value=BODY):
                with urllib.request.urlopen(url, timeout=2) as response:
                    payload = json.load(response)
                    self.assertEqual(200, response.status)
                self.assertEqual({"origin_verified": True, "inventory_blocks": 1}, payload)
        finally:
            server.shutdown()
            server.server_close()

    def test_replication_records_public_fallback_and_suppresses_dead_bridge(self):
        args = SimpleNamespace(block_url_base="http://127.0.0.1:18095/ipfs/",
                               fallback_block_url_base="https://ipfs.yataverse.com/ipfs/")
        calls = []

        def fetch(url, _limit):
            calls.append(url)
            if url.startswith(args.block_url_base):
                raise replica.ReplicationError("bridge unavailable")
            return BODY

        source = replica.origin_fallback_block_fetcher(args, fetch)
        self.assertEqual(BODY, source(args.block_url_base + CID, len(BODY)))
        self.assertEqual(BODY, source(args.block_url_base + CID, len(BODY)))
        self.assertEqual([args.block_url_base + CID,
                          args.fallback_block_url_base + CID,
                          args.fallback_block_url_base + CID], calls)
        self.assertEqual({"primary": 0, "fallback": 2}, source.source_counts)

    def test_replication_refuses_both_failed_origins(self):
        args = SimpleNamespace(block_url_base="http://127.0.0.1:18095/ipfs/",
                               fallback_block_url_base="https://ipfs.yataverse.com/ipfs/")
        def fail(_url, _limit):
            raise replica.ReplicationError("unavailable")
        source = replica.origin_fallback_block_fetcher(args, fail)
        with self.assertRaisesRegex(replica.ReplicationError, "both block origins failed"):
            source(args.block_url_base + CID, len(BODY))


if __name__ == "__main__":
    unittest.main()
