#!/bin/sh
set -eu

if [ "$#" -ne 3 ]; then
  echo "REFUSED: expected IPFS binary, CID file, and IPNS name" >&2
  exit 2
fi

ipfs_bin=$1
cid_file=$2
expected_name=$3

if [ ! -x "$ipfs_bin" ] || [ ! -f "$cid_file" ]; then
  echo "REFUSED: IPFS binary or CID file missing" >&2
  exit 2
fi

cid=$(cat "$cid_file")
if ! printf '%s\n' "$cid" | grep -Eq '^b[a-z2-7]{30,}$'; then
  echo "REFUSED: CID file is not one CIDv1 base32 name" >&2
  exit 2
fi

if ! "$ipfs_bin" pin ls --type=recursive "$cid" >/dev/null; then
  echo "REFUSED: target CID is not pinned locally" >&2
  exit 2
fi

published=$("$ipfs_bin" name publish --key=yataverse-apex --lifetime=168h --ttl=5m "/ipfs/$cid")
if [ "$published" != "Published to $expected_name: /ipfs/$cid" ]; then
  echo "REFUSED: published name or target did not match" >&2
  exit 2
fi
printf '%s\n' "$published"
