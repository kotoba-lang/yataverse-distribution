import importlib.util
import sys
import unittest
from pathlib import Path

DEPLOY = Path(__file__).resolve().parents[1] / "deploy"
sys.path.insert(0, str(DEPLOY))
spec = importlib.util.spec_from_file_location("r", DEPLOY / "recover_graph_heads.py")
r = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r)


class Agreed(unittest.TestCase):
    def test_quorum_of_answers(self):
        a = (5, "c5")
        self.assertEqual(r.agreed([a] * 5 + [None, (4, "c4")], 5), a)
        self.assertIsNone(r.agreed([a] * 4 + [(4, "c4")] * 3, 5), "four is not five")
        self.assertIsNone(r.agreed([None] * 7, 5), "nobody answered")


if __name__ == "__main__":
    unittest.main()
