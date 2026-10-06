import importlib.util
import json
import unittest
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

source = Path(__file__).resolve().parents[1] / "deploy" / "graph_head_mirror.py"
spec = importlib.util.spec_from_file_location("graph_head_mirror", source)
gm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gm)

GRAPH = "bafyreiha3q2g6ghjzjtydkvsudnixlbpbf6dlw6b7etd2l3ch4mbruv54e"
VALUE = "bafyreigchr5r5xm64kssxkkyrebtfxozp6lfmvyoimvighzir4bn3rhkn4"


def b58encode(raw):
    n = int.from_bytes(raw, "big")
    out = ""
    while n:
        n, r = divmod(n, 58)
        out = gm.B58[r] + out
    return "1" * (len(raw) - len(raw.lstrip(b"\0"))) + out


def did_of(key):
    pub = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return "did:key:z" + b58encode(b"\xed\x01" + pub)


def signed(key, sequence, value=VALUE, name=GRAPH):
    record = {"name": name, "value": value, "sequence": sequence, "valid_until": None}
    sig = key.sign(gm.dag_cbor(record))
    return dict(record, public_key_multibase=did_of(key), signature_multibase="z" + b58encode(sig))


class HeadSignature(unittest.TestCase):
    def setUp(self):
        self.key = Ed25519PrivateKey.generate()
        self.pins = {did_of(self.key)}

    def test_accepts_a_pinned_signed_head(self):
        r = signed(self.key, 7)
        self.assertEqual(gm.verify_head(r, GRAPH, self.pins), r)

    def test_refuses_an_altered_field(self):
        for field, value in (("sequence", 8), ("value", "bafyreigor2t5dsxobb4vwcqfhuvlwc5kl2fiebsc4zpekfcwo2ap544y3q")):
            r = dict(signed(self.key, 7), **{field: value})
            with self.assertRaisesRegex(gm.Refused, "does not verify"):
                gm.verify_head(r, GRAPH, self.pins)

    def test_refuses_an_unpinned_signer(self):
        other = Ed25519PrivateKey.generate()
        with self.assertRaisesRegex(gm.Refused, "not pinned"):
            gm.verify_head(signed(other, 7), GRAPH, self.pins)

    def test_refuses_a_head_for_another_graph(self):
        with self.assertRaisesRegex(gm.Refused, "names"):
            gm.verify_head(signed(self.key, 7), "bafyreig6tog2dqujgwgmmzu2gycl2m4nhetgwsbp3thlxk6lteqj5bdjr4", self.pins)

    def test_dag_cbor_sorts_keys_length_first(self):
        # name(4) < value(5) < sequence(8) < valid_until(11); a4 = map of 4.
        enc = gm.dag_cbor({"valid_until": None, "sequence": 1, "value": "v", "name": "n"})
        self.assertEqual(enc, bytes.fromhex("a4") + b"\x64name\x61n\x65value\x61v\x68sequence\x01\x6bvalid_until\xf6")


class NextStep(unittest.TestCase):
    def setUp(self):
        self.key = Ed25519PrivateKey.generate()

    def doc(self, seq, head, prev=None):
        return json.loads(gm.mirror_doc(GRAPH, seq, prev, head))

    def test_absent_ref_starts_at_zero(self):
        self.assertEqual(gm.next_step(GRAPH, signed(self.key, 3551), None, None), ("submit", (0, None)))

    def test_same_head_is_unchanged(self):
        h = signed(self.key, 3551)
        self.assertEqual(gm.next_step(GRAPH, h, (0, "c0"), self.doc(0, h)), ("unchanged", None))

    def test_advanced_head_chains_to_the_previous_document(self):
        old, new = signed(self.key, 3551), signed(self.key, 3552, value="bafyreigor2t5dsxobb4vwcqfhuvlwc5kl2fiebsc4zpekfcwo2ap544y3q")
        self.assertEqual(gm.next_step(GRAPH, new, (4, "c4"), self.doc(4, old)), ("submit", (5, "c4")))

    def test_rollback_is_refused(self):
        old, new = signed(self.key, 3551), signed(self.key, 3550, value="bafyreigor2t5dsxobb4vwcqfhuvlwc5kl2fiebsc4zpekfcwo2ap544y3q")
        with self.assertRaisesRegex(gm.Refused, "does not advance"):
            gm.next_step(GRAPH, new, (0, "c0"), self.doc(0, old))

    def test_unreadable_previous_document_is_unmeasured(self):
        with self.assertRaises(gm.Unmeasured):
            gm.next_step(GRAPH, signed(self.key, 1), (0, "c0"), None)

    def test_foreign_previous_document_is_refused(self):
        h = signed(self.key, 1)
        with self.assertRaisesRegex(gm.Refused, "not the seq"):
            gm.next_step(GRAPH, h, (1, "c1"), self.doc(0, h))


class Document(unittest.TestCase):
    def test_document_is_canonical_and_cid_is_raw_sha256(self):
        h = signed(Ed25519PrivateKey.generate(), 1)
        data = gm.mirror_doc(GRAPH, 0, None, h)
        self.assertEqual(data, json.dumps(json.loads(data), sort_keys=True, separators=(",", ":")).encode())
        # The CID of b"hello" from `ipfs add --cid-version=1 --raw-leaves`.
        self.assertEqual(gm.raw_cid(b"hello"), "bafkreibm6jg3ux5qumhcn2b3flc3tyu6dmlb4xa7u5bf44yegnrjhc4yeq")


if __name__ == "__main__":
    unittest.main()
