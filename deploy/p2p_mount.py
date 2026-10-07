#!/usr/bin/env python3
"""Keep yataverse-writer reachable as a libp2p protocol (ADR-2610062000).

After Holochain: no node needs an inbound address. Each writer host's Kubo
mounts the writer's HTTP port as the libp2p protocol
/x/yataverse/graph-head/1 (Kubo's Libp2pStreamMounting). A client dials the
writer's peer ID. Nothing here depends on DNS, Tailscale or Cloudflare.

A writer behind NAT is not reliably reachable by a stranger: a circuit-relay
connection is "limited" and Kubo opens no application stream over it, and
DCUtR hole punching between two NATed peers succeeded once in five drills
(2026-10-07). What is reliable is the Holochain relay shape: the NATed writer
keeps an outbound connection to a public peer (Kubo Peering), and that peer
opens the protocol over it (--forward-to). The public peer is replaceable;
any host the writer peers with can carry it.

`ipfs p2p listen` does not survive a Kubo restart, so this process
re-registers the mount whenever `ipfs p2p ls` no longer shows it.

    p2p_mount.py --ipfs "<ipfs>" --target /ip4/127.0.0.1/tcp/18130 [--loop 30]
    p2p_mount.py --ipfs "<ipfs>" --forward-to <peer id> --listen /ip4/127.0.0.1/tcp/18131 [--loop 30]

Exit codes (one-shot): 0 mounted; 3 Kubo did not answer.
"""

import argparse
import shlex
import subprocess
import sys
import time

PROTOCOL = "/x/yataverse/graph-head/1"


def mounted(ls_text, protocol, target):
    """Pure. `ipfs p2p ls` prints `protocol listen-address target-address`."""
    for line in ls_text.splitlines():
        parts = line.split()
        if len(parts) == 3 and parts[0] == protocol and parts[2] == target:
            return True
    return False


def forwarded(ls_text, protocol, listen, peer):
    """Pure. A forward shows as `protocol listen-address /p2p/<peer>`."""
    for line in ls_text.splitlines():
        parts = line.split()
        if len(parts) == 3 and parts[0] == protocol and parts[1] == listen and parts[2] == "/p2p/" + peer:
            return True
    return False


def ensure_forward(ipfs, protocol, listen, peer):
    """Keep a local port that opens `protocol` on a remote writer's Kubo.
    Used where a writer behind NAT keeps a connection to this (public) host:
    the stream rides that connection, so it needs no inbound address there."""
    r = subprocess.run(ipfs + ["p2p", "ls"], capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise RuntimeError("p2p ls: " + (r.stderr.strip() or r.stdout.strip())[-200:])
    if forwarded(r.stdout, protocol, listen, peer):
        return "kept"
    r = subprocess.run(ipfs + ["p2p", "forward", protocol, listen, "/p2p/" + peer],
                       capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise RuntimeError("p2p forward: " + (r.stderr.strip() or r.stdout.strip())[-200:])
    return "mounted"


def ensure(ipfs, protocol, target):
    """-> "kept" | "mounted"; raises RuntimeError when Kubo does not answer."""
    r = subprocess.run(ipfs + ["p2p", "ls"], capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise RuntimeError("p2p ls: " + (r.stderr.strip() or r.stdout.strip())[-200:])
    if mounted(r.stdout, protocol, target):
        return "kept"
    r = subprocess.run(ipfs + ["p2p", "listen", protocol, target], capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise RuntimeError("p2p listen: " + (r.stderr.strip() or r.stdout.strip())[-200:])
    return "mounted"


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--ipfs", required=True, help="ipfs command prefix for this host's Kubo")
    p.add_argument("--target", help="listen: the local writer's address, e.g. /ip4/127.0.0.1/tcp/18130")
    p.add_argument("--forward-to", help="forward instead: the remote writer's peer ID")
    p.add_argument("--listen", help="forward: the local address to open, e.g. /ip4/127.0.0.1/tcp/18131")
    p.add_argument("--protocol", default=PROTOCOL)
    p.add_argument("--loop", type=int, help="re-check every N seconds instead of exiting")
    a = p.parse_args(argv)
    if bool(a.target) == bool(a.forward_to) or bool(a.forward_to) != bool(a.listen):
        p.error("give --target, or --forward-to with --listen")
    ipfs = shlex.split(a.ipfs)
    what = a.target or f"{a.listen} -> /p2p/{a.forward_to}"
    last = None
    while True:
        try:
            state = (ensure(ipfs, a.protocol, a.target) if a.target
                     else ensure_forward(ipfs, a.protocol, a.listen, a.forward_to))
        except (RuntimeError, subprocess.TimeoutExpired, OSError) as e:
            state = f"UNMEASURED {e}"
            if not a.loop:
                print(state, flush=True)
                return 3
        if state != last or state == "mounted":
            print(f"{state} {a.protocol} {what}", flush=True)
            last = state
        if not a.loop:
            return 0
        time.sleep(a.loop)


if __name__ == "__main__":
    sys.exit(main())
