import hashlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

source = Path(__file__).resolve().parents[1] / "deploy" / "lake_epoch.py"
spec = importlib.util.spec_from_file_location("lake_epoch", source)
le = importlib.util.module_from_spec(spec)
spec.loader.exec_module(le)

REPO = Path(__file__).resolve().parents[1]


def write(path, rows):
    path.write_text("".join(json.dumps({"cid": c, "bytes": b}) + "\n" for c, b in rows))


CUSTODY = [{"node": "jacob", "wan": "219.104.136.140", "evidence": "x"},
           {"node": "xavier", "wan": "220.146.170.114", "evidence": "y"}]
AUTH = "inga ref yataverse/lake on chain isekai-score-20261006-v5 (ADR-2610062000 P2, shadow)"


class EpochTests(unittest.TestCase):
    def test_delta_keeps_only_rows_the_log_does_not_hold(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            write(d / "log.jsonl", [("bafa", 1), ("bafb", 2)])
            write(d / "diff.jsonl", [("bafb", 2), ("bafc", 3), ("bafd", 4)])
            r = le.delta(d / "log.jsonl", d / "diff.jsonl", d / "out.jsonl")
            self.assertEqual({"rows": 2, "bytes": 7}, {k: r[k] for k in ("rows", "bytes")})
            self.assertEqual(['{"cid":"bafc","bytes":3}', '{"cid":"bafd","bytes":4}'],
                             (d / "out.jsonl").read_text().splitlines())

    def test_delta_refuses_a_size_conflict_and_a_duplicate(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            write(d / "log.jsonl", [("bafa", 1)])
            write(d / "diff.jsonl", [("bafa", 9)])
            with self.assertRaisesRegex(ValueError, "log holds 1 bytes, capture says 9"):
                le.delta(d / "log.jsonl", d / "diff.jsonl", d / "out.jsonl")
            write(d / "diff.jsonl", [("bafz", 1), ("bafz", 1)])
            with self.assertRaisesRegex(ValueError, "duplicated in the capture diff"):
                le.delta(d / "log.jsonl", d / "diff.jsonl", d / "out.jsonl")

    def test_committed_manifests_are_in_the_canonical_form(self):
        # The tool writes sorted keys and no whitespace so the same inputs give
        # the same bytes, hence the same CID, on every custodian. The committed
        # manifests must already be in that form. (Rebuilding epoch-1 from the
        # real 977-row delta reproduced it byte for byte on 2026-10-06; that
        # inventory is not in the repository, so it is not repeated here.)
        for name in ("epoch-0.json", "epoch-1.json", "epoch-2.json", "epoch-3.json"):
            committed = (REPO / "deploy" / "lake-manifests" / name).read_text()
            self.assertEqual(committed, json.dumps(json.loads(committed), sort_keys=True, separators=(",", ":")))

    def test_manifest_computes_counts_and_requires_two_wans(self):
        with tempfile.TemporaryDirectory() as d:
            inv = Path(d) / "inv.jsonl"
            write(inv, [("bafc", 3), ("bafd", 4)])
            text = le.manifest(2, "bafprev", "bafinv", inv, "2026-10-06", "delta", CUSTODY, AUTH)
            m = json.loads(text)
            self.assertEqual((2, 7), (m["inventory"]["rows"], m["inventory"]["bytes"]))
            self.assertEqual(hashlib.sha256(inv.read_bytes()).hexdigest(), m["inventory"]["sha256"])
            self.assertEqual(text, json.dumps(m, sort_keys=True, separators=(",", ":")))
            with self.assertRaisesRegex(ValueError, "two nodes on at least two WANs"):
                le.manifest(2, "bafprev", "bafinv", inv, "d", "r", CUSTODY[:1], AUTH)
            same_wan = [dict(CUSTODY[0]), dict(CUSTODY[0], node="judah")]
            with self.assertRaisesRegex(ValueError, "two nodes on at least two WANs"):
                le.manifest(2, "bafprev", "bafinv", inv, "d", "r", same_wan, AUTH)
            with self.assertRaisesRegex(ValueError, "previous manifest"):
                le.manifest(2, "", "bafinv", inv, "d", "r", CUSTODY, AUTH)
            (Path(d) / "empty.jsonl").write_text("")
            with self.assertRaisesRegex(ValueError, "empty epoch"):
                le.manifest(2, "bafprev", "bafinv", Path(d) / "empty.jsonl", "d", "r", CUSTODY, AUTH)


if __name__ == "__main__":
    unittest.main()
