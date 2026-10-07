import importlib.util
import ipaddress
import json
import sys
import tempfile
import unittest
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PrivateFormat, PublicFormat, NoEncryption

deploy = Path(__file__).resolve().parents[1] / "deploy"
sys.path.insert(0, str(deploy))
spec = importlib.util.spec_from_file_location("p6_drill", deploy / "p6_drill.py")
p6 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(p6)

CF = [ipaddress.ip_network(n) for n in ("104.16.0.0/13", "172.64.0.0/13", "2606:4700::/32")]


def keypair():
    sk = Ed25519PrivateKey.generate()
    seed = sk.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption()).hex()
    return seed, p6.did_of_pub(sk.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw))


def action(author):
    return {"schema": p6.ACTION_SCHEMA, "author": author, "chain": "yataverse/lake", "action_seq": 5,
            "prev_action": "bafkreibfc7kpfjwj2hki7nygkicymkjbk5rgh4mg35ntsf5lddzhuhwuiu",
            "timestamp": "2026-10-07T02:34:42Z", "entry_type": "p6-drill", "entry": {"phase": "P6"}}


class ActionTest(unittest.TestCase):
    def test_signed_action_verifies_from_the_block_alone(self):
        seed, did = keypair()
        block = p6.canonical(p6.sign_action(action(did), seed))
        self.assertEqual(p6.verify_action(block)["author"], did)

    def test_did_round_trip(self):
        _, did = keypair()
        self.assertEqual(p6.did_of_pub(p6.pub_of_did(did)), did)

    def test_seed_that_is_not_the_author_is_refused(self):
        seed, _ = keypair()
        _, other = keypair()
        with self.assertRaises(p6.Refused):
            p6.sign_action(action(other), seed)

    def test_every_field_is_signed(self):
        seed, did = keypair()
        signed = p6.sign_action(action(did), seed)
        for k, v in (("action_seq", 6), ("prev_action", "bafkreiother"), ("entry", {"phase": "P7"}), ("chain", "x")):
            with self.assertRaises(Exception, msg=k):
                p6.verify_action(p6.canonical(dict(signed, **{k: v})))

    def test_another_authors_did_does_not_verify(self):
        seed, did = keypair()
        _, other = keypair()
        signed = p6.sign_action(action(did), seed)
        with self.assertRaises(Exception):
            p6.verify_action(p6.canonical(dict(signed, author=other)))

    def test_non_canonical_block_is_refused(self):
        seed, did = keypair()
        signed = p6.sign_action(action(did), seed)
        with self.assertRaises(p6.Refused):
            p6.verify_action(json.dumps(signed, indent=1).encode())

    def test_raw_cid_digest(self):
        cid = "bafkreigonzau42ygpemkobt26r76kv7icwbyydhc6aeuzfxf4no53njoge"
        self.assertEqual(p6.raw_cid_digest(cid).hex(),
                         "ce6e414e6b067918a7067af47fe557e815838c0ce2f0094c96e5e35dddb52e31")
        with self.assertRaises(p6.Refused):
            p6.raw_cid_digest("bafyreiha3q2g6ghjzjtydkvsudnixlbpbf6dlw6b7etd2l3ch4mbruv54e")


class ObserveTest(unittest.TestCase):
    def test_classify(self):
        self.assertEqual(p6.classify("104.18.24.159", CF), "cloudflare")
        self.assertEqual(p6.classify("2606:4700::6812:189f", CF), "cloudflare")
        self.assertEqual(p6.classify("100.117.208.83", CF), "tailnet")
        self.assertEqual(p6.classify("127.0.0.1", CF), "loopback")
        self.assertEqual(p6.classify("192.168.1.3", CF), "private")
        self.assertEqual(p6.classify("220.146.170.114", CF), "public")
        self.assertEqual(p6.classify("xavier", CF), "name")

    def write(self, events):
        f = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False)
        for e in events:
            f.write(json.dumps(e) + "\n")
        f.close()
        return f.name

    def test_summarize_finds_cloudflare_in_both_sources(self):
        s = p6.summarize(self.write([
            {"ev": "connect", "lang": "node", "host": "100.75.169.8", "port": 12001},
            {"ev": "connect", "lang": "python", "host": "172.67.1.1", "port": 443},
            {"ev": "lsof", "cmd": "ipfs", "remote": "[2606:4700::1]:4001"},
            {"ev": "lsof", "cmd": "tor", "remote": "192.42.116.23:443"},
            {"ev": "dns", "lang": "node", "host": "ipfs.yataverse.com"},
            {"ev": "dns", "lang": "node", "host": "localhost"}]), CF)
        self.assertEqual([c["host"] for c in s["cloudflare"]], ["172.67.1.1", "2606:4700::1"])
        self.assertEqual(s["name_lookups"], ["ipfs.yataverse.com"])
        self.assertEqual(s["connections_by_class"], {"tailnet": 1, "cloudflare": 2, "public": 1})

    def test_summarize_clean_run(self):
        s = p6.summarize(self.write([{"ev": "lsof", "cmd": "ssh", "remote": "100.117.208.83:22"},
                                     {"ev": "exec", "lang": "python", "argv0": "ssh"}]), CF)
        self.assertEqual((s["cloudflare"], s["name_lookups"]), ([], []))


if __name__ == "__main__":
    unittest.main()
