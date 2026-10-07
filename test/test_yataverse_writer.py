import importlib.util
import sys
import unittest
from pathlib import Path

DEPLOY = Path(__file__).resolve().parents[1] / "deploy"
sys.path.insert(0, str(DEPLOY))
spec = importlib.util.spec_from_file_location("yataverse_writer", DEPLOY / "yataverse_writer.py")
yw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(yw)
gm = yw.gm

G = "bafyreiha3q2g6ghjzjtydkvsudnixlbpbf6dlw6b7etd2l3ch4mbruv54e"


def head(seq, value="bafyreiaaa"):
    return {"name": G, "value": value, "sequence": seq}


def doc(seq, h):
    return {"schema": gm.SCHEMA, "graph": G, "seq": seq, "prev": None, "head": h}


class Decide(unittest.TestCase):
    def test_first_write_creates_seq_0(self):
        self.assertEqual(yw.decide(G, None, head(0), None), ("submit", (0, None)))

    def test_next_write_advances_by_one(self):
        cur = doc(4, head(10, "bafyreiold"))
        self.assertEqual(yw.decide(G, {"sequence": 10, "value": "bafyreiold"}, head(11, "bafyreinew"), cur),
                         ("submit", (5, None)))

    def test_stale_expectation_is_a_conflict_naming_the_current_head(self):
        cur = doc(4, head(10, "bafyreiold"))
        with self.assertRaises(yw.Conflict) as c:
            yw.decide(G, {"sequence": 9, "value": "bafyreiolder"}, head(10, "bafyreinew"), cur)
        self.assertEqual(c.exception.current, {"sequence": 10, "value": "bafyreiold"})

    def test_create_when_a_head_exists_is_a_conflict(self):
        with self.assertRaises(yw.Conflict):
            yw.decide(G, None, head(0), doc(0, head(0, "bafyreiother")))

    def test_sequence_must_move_forward(self):
        cur = doc(4, head(10, "bafyreiold"))
        with self.assertRaisesRegex(gm.Refused, "does not advance"):
            yw.decide(G, {"sequence": 10, "value": "bafyreiold"}, head(10, "bafyreinew"), cur)
        self.assertEqual(yw.decide(G, {"sequence": 10, "value": "bafyreiold"}, head(13, "bafyreinew"), cur),
                         ("submit", (5, None)), "several R2 writes may collapse into one step")

    def test_expected_may_name_only_the_sequence(self):
        cur = doc(4, head(10, "bafyreiold"))
        self.assertEqual(yw.decide(G, {"sequence": 10}, head(11, "bafyreinew"), cur), ("submit", (5, None)))
        with self.assertRaises(yw.Conflict):
            yw.decide(G, {"sequence": 9}, head(11, "bafyreinew"), cur)
        with self.assertRaises(yw.Conflict):
            yw.decide(G, {"sequence": 10, "value": "bafyreiwrong"}, head(11, "bafyreinew"), cur)

    def test_retrying_the_committed_head_is_idempotent(self):
        h = head(11, "bafyreinew")
        self.assertEqual(yw.decide(G, {"sequence": 10, "value": "bafyreiold"}, h, doc(5, h)), ("commit", None))


if __name__ == "__main__":
    unittest.main()


class StaleCache(unittest.TestCase):
    def _writer(self, fresh, outcomes):
        w = yw.Writer.__new__(yw.Writer)
        w.locks, w.guard, w.cache = {}, yw.threading.Lock(), {}
        w.a = type("A", (), {"signer": []})()
        calls = []
        w.current = lambda graph: fresh

        def fake_write(graph, expected, head, cur):
            calls.append(cur)
            r = outcomes.pop(0)
            if isinstance(r, Exception):
                raise r
            return r
        w._write = fake_write
        return w, calls

    def test_a_stale_cache_is_re_read_once_before_answering_conflict(self):
        stale, fresh = (4, "c4", {}), (5, "c5", {})
        w, calls = self._writer(fresh, [yw.Conflict(None), {"seq": 6}])
        w.cache[G] = stale
        orig = gm.verify_head
        gm.verify_head = lambda *a: None
        try:
            self.assertEqual(w.write(G, {"sequence": 11}, {"sequence": 12}), {"seq": 6})
        finally:
            gm.verify_head = orig
        self.assertEqual(calls, [stale, fresh], "decided on the cache, then on a fresh read")

    def test_a_real_conflict_is_still_a_conflict(self):
        cur = (5, "c5", {})
        w, calls = self._writer(cur, [yw.Conflict({"sequence": 11, "value": "v"})])
        w.cache[G] = cur
        orig = gm.verify_head
        gm.verify_head = lambda *a: None
        try:
            with self.assertRaises(yw.Conflict):
                w.write(G, {"sequence": 9}, {"sequence": 12})
        finally:
            gm.verify_head = orig
        self.assertEqual(calls, [cur], "a fresh read equal to the cache is not retried")
