#!/usr/bin/env python3
"""Build a dated, content-addressed Yataverse page for the own-node gateway.

Input is the exact 2026-09-26 apex HTML snapshot. Refuse if that source
changes: edits to the transformation must then be reviewed against the new
source instead of silently publishing an incomplete mirror.
"""

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path


SOURCE_SHA256 = "d8c516b3e3dc08408f3dfd2d2d4d05fe86a8e7d3fb5e83b79ca0f145eec3abd0"
OLD_BLOCK_BASE = "https://ipfs.yataverse.com/ipfs/"
NEW_BLOCK_BASE = "/ipfs/"


class BuildError(Exception):
    pass


def replace_span(page, start, end, replacement):
    if page.count(start) != 1 or page.count(end) != 1:
        raise BuildError("source markup anchor count changed")
    i = page.index(start)
    j = page.index(end, i + len(start))
    return page[:i] + replacement + page[j:]


def build(source):
    if hashlib.sha256(source).hexdigest() != SOURCE_SHA256:
        raise BuildError("source HTML SHA-256 differs from dated snapshot")
    try:
        page = source.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BuildError("source HTML is not UTF-8") from exc
    if page.count(OLD_BLOCK_BASE) != 50:
        raise BuildError("source gateway link count changed")
    page = page.replace(OLD_BLOCK_BASE, NEW_BLOCK_BASE)
    page = replace_span(
        page, '<p class="muted">An IPLD/IPFS-centered mirror', '<div class="plan">',
        '<p class="muted">Dated 2026-09-26 read-only snapshot on an own-node IPNS '
        'gateway. The inventory covers 821,533 public CIDs. This gateway returns '
        'bytes only for blocks pinned on this node; unavailable blocks return 404. '
        'The original content CID remains available through IPFS.</p>\n',
    )
    page = replace_span(
        page, '<div class="plan">', '<h2>Machine access</h2>',
        '<div class="plan"><div><h3>Independent read path</h3><p>'
        'Browse the dated inventory, retrieve pinned bytes from this gateway, '
        'or use the native ipfs:// links. Updates and other lake APIs remain '
        'unavailable here.</p></div></div>\n',
    )
    page = replace_span(
        page, '<p class="muted">LLMs and agents call the lake through tools, not screens:',
        '<h2>Portable inventory snapshot</h2>',
        '<p class="muted">Read-only local endpoints: '
        '<code>GET /health</code>, <code>GET /api/v1/lake/blocks</code> '
        '(dated inventory with integer cursor), and <code>GET /ipfs/{cid}</code> '
        '(only locally pinned bytes). Other APIs are unavailable.</p>\n',
    )
    page, cursor_count = re.subn(
        r'More blocks available — next page: <code>/api/v1/lake/blocks\?cursor=[^<]+</code>',
        'More blocks available — browse the complete dated inventory at '
        '<code>/api/v1/lake/blocks</code>.',
        page,
    )
    if cursor_count != 1:
        raise BuildError("source next-page marker changed")
    page, footer_count = re.subn(
        r'identity: content-addressed · naming: DNSLink/IPNS · this page: generated at request time from the same bucket it serves\.',
        'identity: content-addressed · naming: signed IPNS · this page: dated, static, read-only own-node snapshot.',
        page,
    )
    if footer_count != 1 or OLD_BLOCK_BASE in page:
        raise BuildError("source footer or gateway links changed")
    result = page.encode("utf-8")
    return result, {"source_sha256": SOURCE_SHA256, "sha256": hashlib.sha256(result).hexdigest(),
                    "bytes": len(result), "rewritten_block_links": 50,
                    "scope": "dated-own-node-read-only-snapshot"}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        result, receipt = build(args.source.read_bytes())
        args.output.write_bytes(result)
    except (OSError, BuildError) as exc:
        print("REFUSED: " + str(exc), file=sys.stderr)
        return 2
    print(json.dumps(receipt, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
