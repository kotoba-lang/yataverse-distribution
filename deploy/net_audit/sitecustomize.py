"""Record every outbound socket connect and name lookup this Python process
makes, one JSON line each, to $NET_AUDIT_LOG (ADR-2610062000 P6).

Loaded through PYTHONPATH, so every child python3 of the drill inherits it.
sys.addaudithook sees socket.connect and socket.getaddrinfo for every socket
the interpreter opens, urllib and http.client included; it is not sampled."""
import json
import os
import sys
import time

_out = os.environ.get("NET_AUDIT_LOG")


def _rec(o):
    try:
        with open(_out, "a") as f:
            f.write(json.dumps({"t": int(time.time() * 1000), "pid": os.getpid(), "lang": "python", **o}) + "\n")
    except Exception:
        pass


def _hook(event, args):
    if event == "socket.connect":
        addr = args[1]
        if isinstance(addr, tuple):
            _rec({"ev": "connect", "host": str(addr[0]), "port": addr[1] if len(addr) > 1 else None})
        else:
            _rec({"ev": "connect", "path": str(addr)})
    elif event == "socket.getaddrinfo":
        _rec({"ev": "dns", "host": str(args[0])})
    elif event == "subprocess.Popen":
        _rec({"ev": "exec", "argv0": os.path.basename(str(args[0]))})


if _out:
    sys.addaudithook(_hook)
