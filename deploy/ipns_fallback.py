#!/usr/bin/env python3
"""Keep IPNS names alive from a second custodian without ever publishing stale data.

The primary signer (xavier) re-signs each name on its own timer. gad, the
other signer, has been offline since 2026-10-02, and the public directory name
whose key lived only there stopped resolving. Holding the keys on a second
WAN is only half of the fix: if two hosts each publish what *they* believe the
value is, the one with the higher sequence wins even when its value is older.

So this custodian never chooses a value. For each name it:

1. fetches the current signed record from the routing system (`ipfs name get`)
   and re-puts it unchanged (`ipfs name put`), which keeps the primary's record
   available without any key;
2. only when that record's validity falls below --resign-below-hours
   (default 24; the primary renews daily, so this fires only after a
   multi-day primary outage), re-signs the
   SAME value with sequence + 1 (`ipfs name publish --sequence`), then checks
   that resolution now returns that value;
3. refuses and reports a name it cannot fetch at all, rather than inventing a
   value for it.

Exit 0 when every name was kept or re-signed; 1 when any name could not be
fetched or a re-sign did not take effect.
"""
import argparse
import datetime as dt
import json
import subprocess
import sys
import tempfile


def parse_validity(text):
    # Kubo prints RFC 3339 with nanoseconds; datetime takes microseconds.
    head, _, frac = text.rstrip("Z").partition(".")
    frac = (frac + "000000")[:6]
    return dt.datetime.fromisoformat(f"{head}.{frac}+00:00")


class Ipfs:
    def __init__(self, binary, repo):
        self.binary, self.repo = binary, repo

    def run(self, *args, data=None, timeout=180):
        return subprocess.run([self.binary, *args], input=data, capture_output=True,
                              timeout=timeout, env={"IPFS_PATH": self.repo, "PATH": "/usr/bin:/bin"})

    def get_record(self, name):
        r = self.run("name", "get", name)
        if r.returncode or not r.stdout:
            return None, None
        i = self.run("name", "inspect", "--enc=json", data=r.stdout)
        if i.returncode:
            return None, None
        return r.stdout, json.loads(i.stdout)["Entry"]

    def put_record(self, name, record):
        with tempfile.NamedTemporaryFile() as f:
            f.write(record)
            f.flush()
            return self.run("name", "put", "--force", name, f.name).returncode == 0

    def publish(self, key, value, sequence):
        return self.run("name", "publish", f"--key={key}", f"--sequence={sequence}",
                        "--lifetime=168h", "--ttl=5m", value).returncode == 0

    def resolve(self, name):
        r = self.run("name", "resolve", "--nocache", f"/ipns/{name}")
        return r.stdout.decode().strip() if r.returncode == 0 else None


def keep(ipfs, name, key, now, resign_below):
    record, entry = ipfs.get_record(name)
    if record is None:
        return False, f"REFUSE {name} ({key}): no record could be fetched; not inventing a value"
    value, seq = entry["Value"], int(entry["Sequence"])
    left = parse_validity(entry["Validity"]) - now
    if left > resign_below:
        put = ipfs.put_record(name, record)
        return True, (f"KEPT {name} ({key}) seq={seq} value={value} "
                      f"valid-for={int(left.total_seconds() // 3600)}h reput={'ok' if put else 'failed'}")
    if not ipfs.publish(key, value, seq + 1):
        return False, f"REFUSE {name} ({key}): re-sign of {value} at seq {seq + 1} failed"
    resolved = ipfs.resolve(name)
    if resolved != value:
        return False, f"REFUSE {name} ({key}): re-signed but resolves to {resolved!r}, not {value}"
    return True, f"RESIGNED {name} ({key}) seq={seq + 1} value={value} (was valid for {int(left.total_seconds() // 3600)}h)"


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--ipfs-bin", required=True)
    p.add_argument("--ipfs-path", required=True)
    p.add_argument("--name", action="append", required=True, help="k51...=key-alias")
    p.add_argument("--resign-below-hours", type=int, default=24)
    p.add_argument("--publish", help="primary mode: publish this /ipfs/ value for the single --name with sequence = network sequence + 1")
    a = p.parse_args(argv)
    ipfs = Ipfs(a.ipfs_bin, a.ipfs_path)
    if a.publish:
        # A primary signer must not trust its local sequence: a fallback
        # custodian may have re-signed while it was away, and a lower
        # sequence is silently ignored by everyone who saw the higher one.
        if len(a.name) != 1:
            print("REFUSE --publish takes exactly one --name")
            return 1
        name, _, key = a.name[0].partition("=")
        _, entry = ipfs.get_record(name)
        seq = int(entry["Sequence"]) + 1 if entry else 0
        if not ipfs.publish(key, a.publish, seq):
            print(f"REFUSE {name} ({key}): publish of {a.publish} at seq {seq} failed")
            return 1
        resolved = ipfs.resolve(name)
        if resolved != a.publish:
            print(f"REFUSE {name} ({key}): published but resolves to {resolved!r}")
            return 1
        print(f"PUBLISHED {name} ({key}) seq={seq} value={a.publish}")
        return 0
    now = dt.datetime.now(dt.timezone.utc)
    ok_all = True
    for spec in a.name:
        name, _, key = spec.partition("=")
        ok, line = keep(ipfs, name, key, now, dt.timedelta(hours=a.resign_below_hours))
        print(line, flush=True)
        ok_all &= ok
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
