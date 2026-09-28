import importlib.util
import http.server
import json
import tempfile
import threading
import unittest
from pathlib import Path


source = Path(__file__).resolve().parents[1] / "deploy" / "export_lake_car_from_kubo.py"
spec = importlib.util.spec_from_file_location("export_lake_car_from_kubo", source)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class LocalCarExporterTests(unittest.TestCase):
    def test_dated_tail_dag_json_cid_is_supported(self):
        cid = "baguqeera35dhgx6zqxhqvmrlelvonxh3uclsntqinvpbxpaddngwv25omgga"
        binary, digest = module.cid_bytes(cid)
        version, offset = module.read_varint(binary, 0)
        codec, offset = module.read_varint(binary, offset)
        self.assertEqual((1, 0x129, 32), (version, codec, len(digest)))

    def test_loopback_rpc_requires_matching_repository_and_offline_block(self):
        with tempfile.TemporaryDirectory() as directory:
            state = {"repo": directory, "body": b"block-data", "paths": []}

            class Handler(http.server.BaseHTTPRequestHandler):
                protocol_version = "HTTP/1.1"

                def do_POST(self):
                    state["paths"].append(self.path)
                    if self.path == "/api/v0/repo/stat":
                        body = json.dumps({"RepoPath": state["repo"]}).encode()
                    elif self.path.startswith("/api/v0/block/get?"):
                        body = state["body"]
                    else:
                        self.send_error(404)
                        return
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)

                def log_message(self, *_args):
                    pass

            server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                url = "http://127.0.0.1:{}".format(server.server_port)
                with self.assertRaises(module.ExportError):
                    module.local_rpc(url.replace("127.0.0.1", "example.com"), Path(directory))
                with self.assertRaises(module.ExportError):
                    module.local_rpc(url, Path(directory) / "wrong")
                connection = module.local_rpc(url, Path(directory))
                try:
                    self.assertEqual(module.rpc_block(connection, "bafytest", 10), b"block-data")
                    self.assertIn("offline=true", state["paths"][-1])
                    state["body"] = b"block-data-extra"
                    with self.assertRaises(module.ExportError):
                        module.rpc_block(connection, "bafytest", 10)
                finally:
                    connection.close()
            finally:
                server.shutdown()
                server.server_close()
                worker.join()


if __name__ == "__main__":
    unittest.main()
