import importlib.util
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location("f", Path(__file__).resolve().parents[1] / "deploy" / "r2_head_follower.py")
f = importlib.util.module_from_spec(spec)
spec.loader.exec_module(f)


def h(seq, value="v"):
    return {"sequence": seq, "value": value}


class Classify(unittest.TestCase):
    def test_pairs(self):
        self.assertEqual(f.classify(h(5), h(5)), "agree")
        self.assertEqual(f.classify(h(5, "a"), h(5, "b")), "diverged")
        self.assertEqual(f.classify(h(6), h(5)), "inga-ahead")
        self.assertEqual(f.classify(h(5), h(6)), "r2-ahead")
        self.assertEqual(f.classify(None, h(5)), "absent")
        self.assertEqual(f.classify(h(5), None), "absent")

    def test_project_needs_cutover_and_a_put_command(self):
        with self.assertRaises(SystemExit):
            f.main(["--graph", "g", "--writer-url", "http://x", "--r2-get-cmd", "true", "--project"])


if __name__ == "__main__":
    unittest.main()
