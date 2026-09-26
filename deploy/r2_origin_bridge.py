#!/usr/bin/env python3
"""Local, CID-checked R2 bootstrap source for the existing lake replicator.

Bind only to loopback. Reach it from a node with an SSH reverse tunnel; the
Cloudflare OAuth token never leaves the operator host. This is a temporary
copy source, not a replacement for the independent node gateway.
"""

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import re
import subprocess
import threading
import urllib.error
import urllib.request

from raw_block_store import RawBlockStoreError, verify_cid
from replicate_lake import inventory_sizes


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


_open = urllib.request.build_opener(NoRedirect).open


class ByteBudget:
    def __init__(self, limit):
        self.limit = limit
        self.used = 0
        self.condition = threading.Condition()

    def acquire(self, size):
        if size > self.limit:
            raise ValueError("block exceeds bridge memory budget")
        with self.condition:
            while self.used + size > self.limit:
                self.condition.wait()
            self.used += size

    def release(self, size):
        with self.condition:
            self.used -= size
            self.condition.notify_all()


class OAuthToken:
    def __init__(self):
        self.lock = threading.Lock()
        self.value = None

    def get(self, refresh=False):
        with self.lock:
            if self.value is None or refresh:
                process = subprocess.run(
                    ["npx", "--yes", "wrangler", "auth", "token", "--json"],
                    capture_output=True, text=True, timeout=30, check=True)
                candidate = json.loads(process.stdout)["token"]
                if not isinstance(candidate, str) or not candidate:
                    raise ValueError("wrangler returned no OAuth token")
                self.value = candidate
            return self.value


class Origin:
    def __init__(self, sizes, account_id, bucket, token=None, max_memory=512_000_000):
        self.sizes = sizes
        self.canary_cid = min(sizes, key=sizes.get)
        self.account_id = account_id
        self.bucket = bucket
        self.token = token or OAuthToken()
        self.budget = ByteBudget(max_memory)

    def fetch(self, cid):
        size = self.sizes.get(cid)
        if size is None:
            raise KeyError("CID is absent from pinned inventory")
        self.budget.acquire(size)
        try:
            url = ("https://api.cloudflare.com/client/v4/accounts/" + self.account_id +
                   "/r2/buckets/" + self.bucket + "/objects/ipld/" + cid)
            for attempt in range(2):
                request = urllib.request.Request(url, headers={
                    "Authorization": "Bearer " + self.token.get(refresh=bool(attempt)),
                })
                try:
                    with _open(request, timeout=60) as response:
                        if response.status != 200:
                            raise IOError("R2 did not return 200")
                        body = response.read(size + 1)
                    break
                except urllib.error.HTTPError as error:
                    code = error.code
                    error.close()
                    if code != 401 or attempt:
                        raise IOError("R2 HTTP " + str(code)) from None
            if len(body) != size:
                raise IOError("R2 size differs from pinned inventory")
            try:
                verify_cid(cid, body)
            except RawBlockStoreError as error:
                raise IOError("R2 bytes differ from CID") from error
            return body
        finally:
            self.budget.release(size)


def handler_for(origin):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/healthz":
                try:
                    origin.fetch(origin.canary_cid)
                except (IOError, OSError, subprocess.SubprocessError, ValueError):
                    self.send_error(503)
                    return
                body = json.dumps({"origin_verified": True, "inventory_blocks": len(origin.sizes)}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
            elif self.path.startswith("/ipfs/") and self.path.count("/") == 2:
                cid = self.path[len("/ipfs/"):]
                try:
                    body = origin.fetch(cid)
                except KeyError:
                    self.send_error(404)
                    return
                except (IOError, OSError, subprocess.SubprocessError, ValueError):
                    self.send_error(502)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
            else:
                self.send_error(404)
                return
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            pass

    return Handler


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--inventory", required=True)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--port", type=int, default=18095)
    parser.add_argument("--max-memory-bytes", type=int, default=512_000_000)
    args = parser.parse_args()
    if not re.fullmatch(r"[0-9a-f]{32}", args.account_id):
        parser.error("account id must be 32 hex characters")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{2,63}", args.bucket):
        parser.error("invalid bucket")
    if args.port < 1 or args.port > 65535 or args.max_memory_bytes < 256_000_000:
        parser.error("invalid port or memory bound")
    sizes = inventory_sizes(args.inventory, args.sha256)
    origin = Origin(sizes, args.account_id, args.bucket,
                    max_memory=args.max_memory_bytes)
    # Preflight the credential before reporting a ready health endpoint.
    origin.token.get()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), handler_for(origin))
    server.daemon_threads = True
    print("R2 bootstrap bridge ready; inventory_blocks=" + str(len(sizes)), flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
