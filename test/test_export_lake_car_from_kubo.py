import importlib.util
import unittest
from pathlib import Path


source = Path(__file__).resolve().parents[1] / "deploy" / "export_lake_car_from_kubo.py"
spec = importlib.util.spec_from_file_location("export_lake_car_from_kubo", source)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class LocalCarExporterTests(unittest.TestCase):
    def test_dated_tail_dag_json_cid_is_supported(self):
        cid = "baguqeera35dhgx6zqxhqvmrlelvonxh3uclsntqinvpbxpaddngwv25omgga"
        binary, digest = module.cid_bytes(cid)
        version, offset = module.read_varint(binary, 0)
        codec, offset = module.read_varint(binary, offset)
        self.assertEqual((1, 0x129, 32), (version, codec, len(digest)))


if __name__ == "__main__":
    unittest.main()
