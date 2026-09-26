#!/usr/bin/env python3
"""Move the shared public HTTPS lease to gad after xavier gateway loss.

The two nodes still share one router. This protects against a node/gateway
failure, not router, ISP, DNS, certificate, or site-update failure.
"""

import argparse
import fcntl
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
from pathlib import Path


PUBLIC_IP = "220.146.170.114"
XAVIER_IP = "192.168.1.28"
GAD_IP = "192.168.1.16"
SITES = {
    "yataverse-data.220-146-170-114.sslip.io":
        "2fd28ac84111bd181090f4ea712f8988c80dbad9162edf52c108865a3b636e97",
    "isekai-static.220-146-170-114.sslip.io":
        "5e3fc8703c96bbdb308974cb96559185bbd23748aa3a060ad973d5ca6385fe83",
    "itonami-static.220-146-170-114.sslip.io":
        "1c1232c376f79e7e53af01ec7d85cfbfb2f4b9ee0691d4b2c1e600fcfbaa4539",
}


class Refused(Exception):
    pass


def command(argv, timeout=20):
    try:
        result = subprocess.run(argv, capture_output=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Refused("command unavailable: " + argv[0]) from exc
    if result.returncode:
        raise Refused("command failed: " + argv[0])
    return result.stdout


def gateway_healthy(ip):
    for host, expected in SITES.items():
        try:
            body = command([
                "curl", "-fLsS", "--noproxy", "*", "--connect-timeout", "2",
                "--max-time", "7", "--proto", "=https", "--resolve",
                host + ":443:" + ip, "https://" + host + "/"], timeout=9)
        except Refused:
            return False
        if hashlib.sha256(body).hexdigest() != expected:
            return False
    return True


def tcp_accepts(ip):
    try:
        with socket.create_connection((ip, 443), timeout=2):
            return True
    except OSError:
        return False


def router_mapping(output):
    text = output.decode("utf-8", "replace")
    match = re.search(r"^ExternalIPAddress\s*=\s*(\S+)\s*$", text, re.MULTILINE)
    if not match or match.group(1) != PUBLIC_IP:
        raise Refused("router public IPv4 differs from configured address")
    matches = re.findall(r"^\s*\d+\s+TCP\s+8443->([0-9.]+):(\d+)\b", text, re.MULTILINE)
    if len(matches) > 1:
        raise Refused("router has multiple HTTPS mappings")
    if not matches:
        return None
    target, port = matches[0]
    if target not in (XAVIER_IP, GAD_IP) or port != "443":
        raise Refused("router HTTPS mapping has unexpected owner")
    return target


def decide(primary_healthy, standby_healthy, failures, mapping,
           primary_listener_active=False):
    if primary_healthy:
        return "restore-primary" if mapping != XAVIER_IP else "keep-primary"
    if failures < 3:
        return "wait-for-confirmation"
    if primary_listener_active:
        raise Refused("primary HTTPS listener is still active; renewal may contend")
    if not standby_healthy:
        raise Refused("standby gateway failed its own content check")
    return "take-over" if mapping != GAD_IP else "renew-standby"


def save_failures(path, count):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    with temporary.open("w") as handle:
        json.dump({"consecutive-primary-failures": count}, handle)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def load_failures(path):
    if not path.exists():
        return 0
    try:
        count = json.loads(path.read_text())["consecutive-primary-failures"]
    except (OSError, KeyError, ValueError, TypeError) as exc:
        raise Refused("standby state unreadable") from exc
    if type(count) is not int or count < 0:
        raise Refused("standby state has invalid failure count")
    return count


def set_mapping(target, current):
    if current and current != target:
        command(["upnpc", "-d", "8443", "TCP"])
        if router_mapping(command(["upnpc", "-l"])) is not None:
            raise Refused("router refused to remove old HTTPS mapping")
    try:
        command(["upnpc", "-a", target, "443", "8443", "TCP", "900"])
        if router_mapping(command(["upnpc", "-l"])) != target:
            raise Refused("router did not confirm new HTTPS mapping")
    except Refused:
        # A failed replacement must not strand a formerly healthy endpoint.
        if current and current != target:
            try:
                command(["upnpc", "-a", current, "443", "8443", "TCP", "900"])
            except Refused:
                pass
        raise


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--state-dir", type=Path,
                        default=Path.home() / ".local/state/yataverse-router-standby")
    args = parser.parse_args()
    args.state_dir.mkdir(parents=True, exist_ok=True)
    try:
        with (args.state_dir / "check.lock").open("w") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            mapping = router_mapping(command(["upnpc", "-l"]))
            primary = gateway_healthy(XAVIER_IP)
            failures = 0 if primary else load_failures(args.state_dir / "state.json") + 1
            standby = gateway_healthy(GAD_IP) if not primary and failures >= 3 else None
            # Xavier's existing timer checks nginx, not content. Takeover while
            # that listener still runs would contend with its own UPnP lease.
            listener = tcp_accepts(XAVIER_IP) if not primary and failures >= 3 else False
            action = decide(primary, standby, failures, mapping, listener)
            if args.apply:
                save_failures(args.state_dir / "state.json", failures)
                if action in ("restore-primary", "take-over", "renew-standby"):
                    target = XAVIER_IP if action == "restore-primary" else GAD_IP
                    set_mapping(target, mapping)
            print(json.dumps({"action": action, "primary_healthy": primary,
                              "standby_healthy": standby, "failures": failures,
                              "mapping": mapping, "applied": args.apply}, sort_keys=True))
    except (Refused, BlockingIOError) as exc:
        print("REFUSED: " + str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
