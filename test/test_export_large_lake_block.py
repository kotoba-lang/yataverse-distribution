from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "deploy"))
import export_large_lake_block as exporter


CID = "bafkreids5efpt5x7ltboi3ya5mqeidf3hanukssa5ohfonps3jwzg3t3aa"


class SourceTests(unittest.TestCase):
    def test_loopback_http_uses_only_http_without_proxy(self):
        url = exporter.checked_source_url("http://127.0.0.1:8091/ipfs/", CID)
        argv = exporter.source_curl_argv(url, 202382755, "/tmp/source.block")
        self.assertEqual("=http", argv[argv.index("--proto") + 1])
        self.assertEqual("*", argv[argv.index("--noproxy") + 1])
        self.assertEqual("0", argv[argv.index("--max-redirs") + 1])

    def test_https_gateway_remains_allowed(self):
        url = exporter.checked_source_url("https://example.org/ipfs/", CID)
        argv = exporter.source_curl_argv(url, 202382755, "/tmp/source.block")
        self.assertEqual("=https", argv[argv.index("--proto") + 1])
        self.assertNotIn("--noproxy", argv)

    def test_plain_http_requires_literal_loopback_and_port(self):
        for base in ("http://localhost:8091/ipfs/", "http://192.168.1.1:8091/ipfs/",
                     "http://example.org/ipfs/", "http://127.0.0.1/ipfs/",
                     "http://127.0.0.1:8091/other/",
                     "http://user@127.0.0.1:8091/ipfs/",
                     "http://127.0.0.1:8091/ipfs/?q=1"):
            with self.subTest(base=base):
                with self.assertRaises(exporter.ExportError):
                    exporter.checked_source_url(base, CID)


if __name__ == "__main__":
    unittest.main()
