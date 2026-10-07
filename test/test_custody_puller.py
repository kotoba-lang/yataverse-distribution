import importlib.util
import json
import sys
import unittest
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

TEST = Path(__file__).resolve().parent
sys.path.insert(0, str(TEST))
sys.path.insert(0, str(TEST.parent / "deploy"))
import test_graph_head_mirror as tm  # noqa: E402

spec = importlib.util.spec_from_file_location("custody_puller", TEST.parent / "deploy" / "custody_puller.py")
cp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cp)
gm = cp.gm
G = tm.GRAPH


class CheckDoc(unittest.TestCase):
    def setUp(self):
        self.key = Ed25519PrivateKey.generate()
        self.pins = {tm.did_of(self.key)}

    def doc(self, seq, prev, key=None, graph=G):
        data = gm.mirror_doc(graph, seq, prev, tm.signed(key or self.key, 100 + seq))
        return data, gm.raw_cid(data)

    def test_a_chain_validates_back_to_seq_0(self):
        d0, c0 = self.doc(0, None)
        d1, c1 = self.doc(1, c0)
        self.assertEqual(cp.check_doc(G, 1, c1, d1, self.pins)["prev"], c0)
        self.assertIsNone(cp.check_doc(G, 0, c0, d0, self.pins)["prev"])

    def test_bytes_that_do_not_hash_to_the_cid(self):
        d0, _ = self.doc(0, None)
        _, other = self.doc(1, "bafkreiprev")
        with self.assertRaises(gm.Refused):
            cp.check_doc(G, 0, other, d0, self.pins)

    def test_wrong_seq_or_graph(self):
        d1, c1 = self.doc(1, "bafkreiprev")
        with self.assertRaises(gm.Refused):
            cp.check_doc(G, 2, c1, d1, self.pins)
        with self.assertRaises(gm.Refused):
            cp.check_doc("bafyreig6tog2dqujgwgmmzu2gycl2m4nhetgwsbp3thlxk6lteqj5bdjr4", 1, c1, d1, self.pins)

    def test_prev_must_match_seq_0(self):
        d, c = self.doc(0, "bafkreiprev")
        with self.assertRaises(gm.Refused):
            cp.check_doc(G, 0, c, d, self.pins)
        d, c = self.doc(3, None)
        with self.assertRaises(gm.Refused):
            cp.check_doc(G, 3, c, d, self.pins)

    def test_a_head_not_signed_by_the_namespace_is_a_warrant(self):
        d, c = self.doc(1, "bafkreiprev", key=Ed25519PrivateKey.generate())
        with self.assertRaises(gm.Refused):
            cp.check_doc(G, 1, c, d, self.pins)


if __name__ == "__main__":
    unittest.main()
