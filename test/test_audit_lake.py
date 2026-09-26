import hashlib
import base64
import importlib.util
import json
import tempfile
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


source = Path(__file__).resolve().parents[1] / "deploy" / "audit_lake.py"
sys.path.insert(0, str(source.parent))
spec = importlib.util.spec_from_file_location("audit_lake", source)
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)

CID_A = "QmNLgJDagPKXUg4C1vKtvETQhTxEDScNYwEhYBke5uBan8"
CID_B = "QmNLhRArQBktFvoG2vgmoM3oMZC8vgWGhk9sM4Q4KkGUxh"


class AuditTests(unittest.TestCase):
    def inventory(self, directory, rows):
        path = Path(directory) / "inventory.jsonl"
        data = b"".join(json.dumps(row).encode() + b"\n" for row in rows)
        path.write_bytes(data)
        return path, hashlib.sha256(data).hexdigest()

    def test_complete_and_partial_have_distinct_counts(self):
        with tempfile.TemporaryDirectory() as directory:
            path, digest = self.inventory(directory, [
                {"cid": CID_A, "bytes": 3}, {"cid": CID_B, "bytes": 5}])
            complete = audit.audit_inventory(path, digest, 2, {CID_A, CID_B})
            self.assertEqual(("complete", 2, 8, 0, 0),
                             (complete["status"], complete["pinned_rows"],
                              complete["pinned_bytes"], complete["missing_rows"],
                              complete["missing_bytes"]))
            partial = audit.audit_inventory(path, digest, 2, {CID_A})
            self.assertEqual(("partial", 1, 3, 1, 5, [CID_B]),
                             (partial["status"], partial["pinned_rows"],
                              partial["pinned_bytes"], partial["missing_rows"],
                              partial["missing_bytes"], partial["missing_sample"]))

    def test_empty_bad_digest_and_duplicate_refuse(self):
        with tempfile.TemporaryDirectory() as directory:
            path, digest = self.inventory(directory, [])
            with self.assertRaisesRegex(audit.AuditError, "count or SHA-256"):
                audit.audit_inventory(path, digest, 1, set())
            path, digest = self.inventory(directory, [{"cid": CID_A, "bytes": 3}])
            with self.assertRaisesRegex(audit.AuditError, "count or SHA-256"):
                audit.audit_inventory(path, "0" * 64, 1, {CID_A})
            path, digest = self.inventory(directory, [
                {"cid": CID_A, "bytes": 3}, {"cid": CID_A, "bytes": 3}])
            with self.assertRaisesRegex(audit.AuditError, "duplicate inventory CID"):
                audit.audit_inventory(path, digest, 2, {CID_A})

    def test_failed_pin_listing_cannot_be_clean(self):
        with patch.object(audit.Path, "is_file", return_value=True), patch.object(
                audit.subprocess, "run", return_value=SimpleNamespace(
                    returncode=1, stderr=b"daemon unavailable", stdout=b"")):
            with self.assertRaisesRegex(audit.AuditError, "daemon unavailable"):
                audit.durable_pins("/bin/ipfs")

    def test_cid_verified_raw_block_covers_only_its_inventory_row(self):
        data = b"\x0a\x03not-valid-dag-pb"
        cid = "b" + base64.b32encode(b"\x01\x70\x12\x20" + hashlib.sha256(data).digest()).decode().lower().rstrip("=")
        with tempfile.TemporaryDirectory() as directory:
            store = audit.RawBlockStore(Path(directory) / "raw")
            store.put(cid, data)
            path, digest = self.inventory(directory, [{"cid": cid, "bytes": len(data)}])
            report = audit.audit_inventory(path, digest, 1, set(), store)
            self.assertEqual(("complete", 1, len(data), 0),
                             (report["status"], report["raw_rows"],
                              report["raw_bytes"], report["missing_rows"]))
            store._paths(cid)[0].write_bytes(data + b"x")
            with self.assertRaisesRegex(audit.RawBlockStoreError, "data differs"):
                audit.audit_inventory(path, digest, 1, set(), store)

    def test_require_complete_exit_distinguishes_partial_from_refusal(self):
        report = {"status": "partial"}
        with patch.object(audit, "durable_pins", return_value=set()), patch.object(
                audit, "audit_inventory", return_value=report), patch(
                "sys.argv", ["audit_lake", "--inventory", "x", "--sha256", "0" * 64,
                             "--count", "1", "--ipfs-bin", "ipfs", "--require-complete"]):
            self.assertEqual(1, audit.main())


if __name__ == "__main__":
    unittest.main()
