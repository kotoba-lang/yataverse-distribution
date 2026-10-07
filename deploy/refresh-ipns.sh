#!/bin/sh
set -eu

if [ "$#" -ne 3 ] && [ "$#" -ne 4 ]; then
  echo "REFUSED: expected IPFS binary, CID file, IPNS name, and optional key name" >&2
  exit 2
fi

ipfs_bin=$1
cid_file=$2
expected_name=$3
key_name=${4:-yataverse-apex}

case "$key_name" in
  ''|*[!a-z0-9-]*)
    echo "REFUSED: invalid IPNS key name" >&2
    exit 2
    ;;
esac

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

# Kubo takes a publish's sequence from the local datastore. A fallback
# custodian (jacob, deploy/ipns_fallback.py) may have re-signed this name at a
# higher sequence while this node was away, and a lower sequence is ignored
# everywhere. So publish one above the sequence the network holds, and refuse
# when that sequence cannot be read rather than trusting the local one.
record=$(mktemp)
trap 'rm -f "$record"' EXIT
if ! "$ipfs_bin" name get "$expected_name" > "$record" || [ ! -s "$record" ]; then
  echo "REFUSED: current IPNS record could not be fetched" >&2
  exit 2
fi
network_seq=$("$ipfs_bin" name inspect --enc=json < "$record" |
  python3 -c 'import json, sys; print(int(json.load(sys.stdin)["Entry"]["Sequence"]))') || network_seq=
case "$network_seq" in
  ''|*[!0-9]*)
    echo "REFUSED: current IPNS record sequence could not be read" >&2
    exit 2
    ;;
esac
sequence=$((network_seq + 1))

published=$("$ipfs_bin" name publish --key="$key_name" --sequence="$sequence" --lifetime=168h --ttl=5m "/ipfs/$cid")
if [ "$published" != "Published to $expected_name: /ipfs/$cid" ]; then
  echo "REFUSED: published name or target did not match" >&2
  exit 2
fi
resolved=$("$ipfs_bin" name resolve --nocache "/ipns/$expected_name")
if [ "$resolved" != "/ipfs/$cid" ]; then
  echo "REFUSED: published at sequence $sequence but resolves to $resolved" >&2
  exit 2
fi
printf '%s (sequence %s, network had %s)\n' "$published" "$sequence" "$network_seq"
