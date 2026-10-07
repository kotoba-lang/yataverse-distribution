import importlib.util
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location("p2p_mount", Path(__file__).resolve().parents[1] / "deploy" / "p2p_mount.py")
pm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pm)

P = "/x/yataverse/graph-head/1"
ME = "12D3KooWH5CfezpDdMtYttLLMeib526f7f8QKCC6jCgiHDme4XSV"
M2 = "12D3KooWREkjSRXrLFe6HXzaP9LpRBGNfJV7YuEwLw5tK2VQH9hN"
LS = (f"{P} /p2p/{ME} /ip4/127.0.0.1/tcp/18130\n"
      f"{P} /ip4/127.0.0.1/tcp/18131                                  /p2p/{M2}\n")


class Mounts(unittest.TestCase):
    def test_listen(self):
        self.assertTrue(pm.mounted(LS, P, "/ip4/127.0.0.1/tcp/18130"))
        self.assertFalse(pm.mounted(LS, P, "/ip4/100.108.223.94/tcp/18130"))
        self.assertFalse(pm.mounted(LS, "/x/other/1", "/ip4/127.0.0.1/tcp/18130"))
        self.assertFalse(pm.mounted("", P, "/ip4/127.0.0.1/tcp/18130"))

    def test_forward(self):
        self.assertTrue(pm.forwarded(LS, P, "/ip4/127.0.0.1/tcp/18131", M2))
        self.assertFalse(pm.forwarded(LS, P, "/ip4/127.0.0.1/tcp/18132", M2))
        self.assertFalse(pm.forwarded(LS, P, "/ip4/127.0.0.1/tcp/18131", ME))

    def test_arguments(self):
        for bad in ([], ["--target", "t", "--forward-to", M2, "--listen", "l"], ["--forward-to", M2]):
            with self.assertRaises(SystemExit, msg=bad):
                pm.main(["--ipfs", "ipfs"] + bad)


if __name__ == "__main__":
    unittest.main()
