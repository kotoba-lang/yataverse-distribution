import datetime as dt
import importlib.util
import unittest
from pathlib import Path

source = Path(__file__).resolve().parents[1] / "deploy" / "ipns_fallback.py"
spec = importlib.util.spec_from_file_location("ipns_fallback", source)
fb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fb)

NOW = dt.datetime(2026, 10, 6, 12, 0, tzinfo=dt.timezone.utc)
NAME = "k51qzi5uqu5dexample"


class FakeIpfs:
    def __init__(self, entry, resolve_to=None, publish_ok=True):
        self.entry, self.resolve_to, self.publish_ok = entry, resolve_to, publish_ok
        self.calls = []

    def get_record(self, name):
        self.calls.append(("get", name))
        return (None, None) if self.entry is None else (b"signed-bytes", self.entry)

    def put_record(self, name, record):
        self.calls.append(("put", name, record))
        return True

    def publish(self, key, value, sequence):
        self.calls.append(("publish", key, value, sequence))
        return self.publish_ok

    def resolve(self, name):
        return self.resolve_to


def entry(hours_left, seq=4, value="/ipfs/bafkcurrent"):
    v = (NOW + dt.timedelta(hours=hours_left)).strftime("%Y-%m-%dT%H:%M:%S.000000000Z")
    return {"Value": value, "Sequence": seq, "Validity": v}


class FallbackTests(unittest.TestCase):
    def test_fresh_record_is_reput_unchanged_and_never_resigned(self):
        ipfs = FakeIpfs(entry(120))
        ok, line = fb.keep(ipfs, NAME, "k", NOW, dt.timedelta(hours=48))
        self.assertTrue(ok)
        self.assertTrue(line.startswith("KEPT"))
        self.assertIn(("put", NAME, b"signed-bytes"), ipfs.calls)
        self.assertFalse([c for c in ipfs.calls if c[0] == "publish"])

    def test_expiring_record_is_resigned_with_the_same_value_and_next_sequence(self):
        ipfs = FakeIpfs(entry(10, seq=7), resolve_to="/ipfs/bafkcurrent")
        ok, line = fb.keep(ipfs, NAME, "k", NOW, dt.timedelta(hours=48))
        self.assertTrue(ok)
        self.assertIn(("publish", "k", "/ipfs/bafkcurrent", 8), ipfs.calls)
        self.assertTrue(line.startswith("RESIGNED"))

    def test_boundary_exactly_at_threshold_resigns(self):
        ipfs = FakeIpfs(entry(48), resolve_to="/ipfs/bafkcurrent")
        ok, line = fb.keep(ipfs, NAME, "k", NOW, dt.timedelta(hours=48))
        self.assertTrue(line.startswith("RESIGNED"))

    def test_unfetchable_name_is_refused_not_invented(self):
        ipfs = FakeIpfs(None)
        ok, line = fb.keep(ipfs, NAME, "k", NOW, dt.timedelta(hours=48))
        self.assertFalse(ok)
        self.assertIn("not inventing a value", line)
        self.assertEqual([("get", NAME)], ipfs.calls)

    def test_resign_that_does_not_take_effect_is_refused(self):
        ipfs = FakeIpfs(entry(10), resolve_to="/ipfs/bafksomethingelse")
        ok, line = fb.keep(ipfs, NAME, "k", NOW, dt.timedelta(hours=48))
        self.assertFalse(ok)
        self.assertIn("resolves to", line)

    def test_failed_publish_is_refused(self):
        ipfs = FakeIpfs(entry(10), publish_ok=False)
        ok, line = fb.keep(ipfs, NAME, "k", NOW, dt.timedelta(hours=48))
        self.assertFalse(ok)
        self.assertIn("re-sign", line)

    def test_validity_with_nanoseconds_parses(self):
        self.assertEqual(dt.datetime(2026, 10, 13, 6, 11, 48, 46042, tzinfo=dt.timezone.utc),
                         fb.parse_validity("2026-10-13T06:11:48.046042652Z"))


if __name__ == "__main__":
    unittest.main()
