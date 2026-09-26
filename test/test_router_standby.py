import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


source = Path(__file__).resolve().parents[1] / "deploy" / "router_standby.py"
spec = importlib.util.spec_from_file_location("router_standby", source)
standby = importlib.util.module_from_spec(spec)
spec.loader.exec_module(standby)


def listing(mapping):
    lines = ["ExternalIPAddress = " + standby.PUBLIC_IP]
    if mapping:
        lines.append("15 TCP  8443->" + mapping + ":443   'libminiupnpc' '' 615")
    return ("\n".join(lines) + "\n").encode()


class RouterStandbyTests(unittest.TestCase):
    def test_only_expected_mapping_is_accepted(self):
        self.assertEqual(standby.XAVIER_IP, standby.router_mapping(listing(standby.XAVIER_IP)))
        self.assertEqual(standby.GAD_IP, standby.router_mapping(listing(standby.GAD_IP)))
        self.assertIsNone(standby.router_mapping(listing(None)))
        with self.assertRaisesRegex(standby.Refused, "unexpected owner"):
            standby.router_mapping(listing("192.168.1.99"))
        with self.assertRaisesRegex(standby.Refused, "public IPv4 differs"):
            standby.router_mapping(b"ExternalIPAddress = 203.0.113.1\n")

    def test_three_failed_checks_required_to_take_over(self):
        for failures in (1, 2):
            self.assertEqual("wait-for-confirmation",
                             standby.decide(False, None, failures, standby.XAVIER_IP))
        self.assertEqual("take-over",
                         standby.decide(False, True, 3, standby.XAVIER_IP))
        self.assertEqual("renew-standby",
                         standby.decide(False, True, 3, standby.GAD_IP))
        with self.assertRaisesRegex(standby.Refused, "standby gateway failed"):
            standby.decide(False, False, 3, standby.XAVIER_IP)
        with self.assertRaisesRegex(standby.Refused, "renewal may contend"):
            standby.decide(False, True, 3, standby.XAVIER_IP, True)

    def test_primary_recovery_restores_its_mapping(self):
        self.assertEqual("restore-primary",
                         standby.decide(True, None, 0, standby.GAD_IP))
        self.assertEqual("keep-primary",
                         standby.decide(True, None, 0, standby.XAVIER_IP))

    def test_timer_cannot_delete_a_mapping_during_unsupervised_cutover(self):
        for action in ("take-over", "restore-primary"):
            with self.assertRaisesRegex(standby.Refused, "cutover disabled"):
                standby.require_cutover_opt_in(action, False)
            standby.require_cutover_opt_in(action, True)
        standby.require_cutover_opt_in("keep-primary", False)
        standby.require_cutover_opt_in("renew-standby", False)

    def test_failed_switch_attempts_rollback(self):
        mapping = [standby.XAVIER_IP]
        calls = []

        def command(argv, timeout=20):
            calls.append(argv)
            if argv[:2] == ["upnpc", "-l"]:
                return listing(mapping[0])
            if argv[:2] == ["upnpc", "-d"]:
                mapping[0] = None
                return b""
            if argv[:2] == ["upnpc", "-a"]:
                if argv[2] == standby.GAD_IP:
                    raise standby.Refused("new mapping failed")
                mapping[0] = argv[2]
                return b""
            self.fail("unexpected command")

        with patch.object(standby, "command", command):
            with self.assertRaisesRegex(standby.Refused, "new mapping failed"):
                standby.set_mapping(standby.GAD_IP, standby.XAVIER_IP)
        self.assertEqual(standby.XAVIER_IP, mapping[0])
        self.assertEqual(2, len([c for c in calls if c[:2] == ["upnpc", "-a"]]))

    def test_failure_counter_is_checked_and_persisted(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            self.assertEqual(0, standby.load_failures(path))
            standby.save_failures(path, 2)
            self.assertEqual(2, standby.load_failures(path))
            path.write_text('{"consecutive-primary-failures":-1}')
            with self.assertRaisesRegex(standby.Refused, "invalid failure count"):
                standby.load_failures(path)


if __name__ == "__main__":
    unittest.main()
