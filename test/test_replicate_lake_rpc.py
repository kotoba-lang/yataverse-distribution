"""Kubo loopback RPC must verify bytes, CID, and durable pin before a receipt."""

import importlib.util
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import shutil
import tempfile
import threading
import unittest
from urllib.parse import parse_qs, urlsplit


source = Path(__file__).resolve().parents[1] / "deploy" / "replicate_lake.py"
spec = importlib.util.spec_from_file_location("replicate_lake", source)
replica = importlib.util.module_from_spec(spec)
spec.loader.exec_module(replica)

CID = "bafkreihg6pmtrfuwpybthr6nrhrqkxke3tosiecp2rhosu7psuzaagpsyy"
CID_V0 = "QmNPTH5gH4g6EhXxkeu2vYzM83xdiLDgc5kFMZcdMzSCTG"


class RPCHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):
        pass

    def do_POST(self):
        parsed = urlsplit(self.path)
        name = parsed.path.removeprefix("/api/v0/")
        params = parse_qs(parsed.query)
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        self.server.calls.append((name, params, body))
        if name == "repo/stat":
            answer = {"RepoPath": self.server.repo_path}
        elif name == "cid/format":
            answer = {"Formatted": self.server.cid_format}
        elif name == "block/put":
            if (self.server.data not in body or
                    (self.server.cid != CID_V0 and params.get("mhlen") != ["32"])):
                self.send_error(400)
                return
            answer = {"Key": "wrong-cid" if self.server.fault == "cid" else self.server.cid}
        elif name == "block/get":
            return self.respond(b"xyz" if self.server.fault == "bytes" else self.server.data,
                                "application/vnd.ipld.raw")
        elif name == "block/stat":
            answer = {"Size": len(self.server.data)}
        elif name == "pin/add":
            answer = {"Pins": [] if self.server.fault == "pin" else [self.server.cid]}
        elif name == "pin/ls":
            answer = {"Keys": {} if self.server.fault == "pin" else {self.server.cid: {"Type": "direct"}}}
        else:
            self.send_error(404)
            return
        return self.respond(json.dumps(answer).encode(), "application/json")

    def respond(self, body, content_type):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class RPCReplicationTests(unittest.TestCase):
    def make_node(self, directory, fault=None):
        server = ThreadingHTTPServer(("127.0.0.1", 0), RPCHandler)
        server.repo_path = directory
        server.data = b"abc"
        server.cid = CID
        server.cid_format = "cidv1 raw sha2-256 32"
        server.fault = fault
        server.calls = []
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        node = replica.KuboRPC(shutil.which("true"),
                               "http://127.0.0.1:" + str(server.server_port))

        def cli(*argv, data=None):
            if argv[:2] == ("pin", "ls"):
                return b""
            if argv == ("repo", "stat"):
                return ("RepoSize: 10\nStorageMax: 1000\nRepoPath: " + directory + "\n").encode()
            self.fail("unexpected CLI call: " + repr(argv))

        node.run = cli
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.addCleanup(lambda: node.connection.close() if node.connection else None)
        return node, server

    def test_loopback_rpc_checks_same_repository_cid_bytes_and_pin(self):
        with tempfile.TemporaryDirectory() as directory:
            node, server = self.make_node(directory)
            node.preflight()
            node.put_verified(CID, b"abc")
            self.assertEqual(3, node.block_size(CID))
            self.assertIn(CID, node.pins)
            self.assertEqual(["repo/stat", "cid/format", "block/put", "block/get",
                              "pin/add", "pin/ls", "block/stat"],
                             [name for name, _params, _body in server.calls])
            self.assertEqual(["false"], server.calls[4][1]["recursive"])

    def test_wrong_cid_bytes_or_pin_refuses_without_a_successful_pin(self):
        for fault, reason in (("cid", "different CID"), ("bytes", "readback differs"),
                              ("pin", "pin receipt differs")):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                node, _server = self.make_node(directory, fault)
                node.preflight()
                with self.assertRaisesRegex(replica.ReplicationError, reason):
                    node.put_verified(CID, b"abc")
                self.assertNotIn(CID, node.pins)

    def test_cidv0_and_large_block_options_are_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            node, server = self.make_node(directory)
            server.cid = CID_V0
            server.cid_format = "cidv0 dag-pb sha2-256 32"
            node.preflight()
            node.put_verified(CID_V0, b"abc")
            put = next(params for name, params, _body in server.calls if name == "block/put")
            self.assertEqual(["v0"], put["format"])
        with tempfile.TemporaryDirectory() as directory:
            node, server = self.make_node(directory)
            server.data = b"x" * (2 * 1024 * 1024 + 1)
            node.preflight()
            node.put_verified(CID, server.data)
            put = next(params for name, params, _body in server.calls if name == "block/put")
            self.assertEqual(["true"], put["allow-big-block"])

    def test_remote_rpc_and_different_repository_refuse(self):
        for url in ("https://127.0.0.1:5001", "http://192.168.1.16:5001",
                    "http://127.0.0.1:5001/api/v0", "http://127.0.0.1:5001?q=1"):
            with self.subTest(url=url), self.assertRaisesRegex(
                    replica.ReplicationError, "loopback HTTP origin"):
                replica.KuboRPC(shutil.which("true"), url)
        with tempfile.TemporaryDirectory() as directory:
            node, server = self.make_node(directory)
            server.repo_path = "/another-repository"
            with self.assertRaisesRegex(replica.ReplicationError, "repository paths differ"):
                node.preflight()


if __name__ == "__main__":
    unittest.main()
