#!/bin/sh
set -eu

repo=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
tmp=$(mktemp -d)
trap 'rm -r "$tmp"' EXIT
cid=bafkreigfslgs56tqnlo2unf2e73gbr77nnf5ismhi3yhkhg2w3gps52n7e
name=k51qzi5uqu5dlrtqhevrfpyrdr2c1thxug3iqys76xxbi7bgzm16mjti2bwrf2

cat > "$tmp/ipfs" <<'SH'
#!/bin/sh
printf '%s\n' "$*" >> "$FAKE_LOG"
case "$1 $2" in
  "pin ls") [ "$FAKE_PIN" = yes ] ;;
  "name get") [ "$FAKE_GET" = yes ] && printf 'signed-record' ;;
  "name inspect") printf '{"Entry":{"Value":"/ipfs/x","Sequence":%s}}\n' "$FAKE_NETWORK_SEQ" ;;
  "name publish") printf 'Published to %s: %s\n' "$FAKE_NAME" "$7" ;;
  "name resolve") printf '%s\n' "$FAKE_RESOLVED" ;;
  *) exit 1 ;;
esac
SH
chmod 700 "$tmp/ipfs"
printf '%s\n' "$cid" > "$tmp/current-cid"

export FAKE_LOG="$tmp/log" FAKE_PIN=yes FAKE_NAME="$name" FAKE_GET=yes
export FAKE_NETWORK_SEQ=41 FAKE_RESOLVED="/ipfs/$cid"
"$repo/deploy/refresh-ipns.sh" "$tmp/ipfs" "$tmp/current-cid" "$name" > "$tmp/out"
grep -Fx "Published to $name: /ipfs/$cid (sequence 42, network had 41)" "$tmp/out" >/dev/null
grep -F "pin ls --type=recursive $cid" "$tmp/log" >/dev/null
grep -F "name get $name" "$tmp/log" >/dev/null
grep -Fx "name publish --key=yataverse-apex --sequence=42 --lifetime=168h --ttl=5m /ipfs/$cid" "$tmp/log" >/dev/null
grep -Fx "name resolve --nocache /ipns/$name" "$tmp/log" >/dev/null

: > "$tmp/log"
"$repo/deploy/refresh-ipns.sh" "$tmp/ipfs" "$tmp/current-cid" "$name" itonami-static > "$tmp/out"
grep -F "name publish --key=itonami-static" "$tmp/log" >/dev/null

: > "$tmp/log"
if "$repo/deploy/refresh-ipns.sh" "$tmp/ipfs" "$tmp/current-cid" "$name" 'bad/key' > "$tmp/out" 2>&1; then
  echo "invalid key name was accepted" >&2
  exit 1
fi
grep -F "REFUSED: invalid IPNS key name" "$tmp/out" >/dev/null
[ ! -s "$tmp/log" ]

: > "$tmp/log"
printf 'not-a-cid\n' > "$tmp/current-cid"
if "$repo/deploy/refresh-ipns.sh" "$tmp/ipfs" "$tmp/current-cid" "$name" > "$tmp/out" 2>&1; then
  echo "invalid CID was accepted" >&2
  exit 1
fi
grep -F "REFUSED: CID file" "$tmp/out" >/dev/null
[ ! -s "$tmp/log" ]

printf '%s\n' "$cid" > "$tmp/current-cid"
export FAKE_PIN=no
if "$repo/deploy/refresh-ipns.sh" "$tmp/ipfs" "$tmp/current-cid" "$name" > "$tmp/out" 2>&1; then
  echo "unpinned CID was accepted" >&2
  exit 1
fi
grep -F "REFUSED: target CID" "$tmp/out" >/dev/null
if grep -F "name publish" "$tmp/log" >/dev/null; then
  echo "unpinned CID was published" >&2
  exit 1
fi

# A primary must not fall back to its local sequence when the network record
# cannot be read: that is exactly the publish a fallback re-sign would bury.
export FAKE_PIN=yes FAKE_GET=no
: > "$tmp/log"
if "$repo/deploy/refresh-ipns.sh" "$tmp/ipfs" "$tmp/current-cid" "$name" > "$tmp/out" 2>&1; then
  echo "unreadable network record was accepted" >&2
  exit 1
fi
grep -F "REFUSED: current IPNS record could not be fetched" "$tmp/out" >/dev/null
if grep -F "name publish" "$tmp/log" >/dev/null; then
  echo "published without the network sequence" >&2
  exit 1
fi

export FAKE_GET=yes FAKE_NETWORK_SEQ='"x"'
if "$repo/deploy/refresh-ipns.sh" "$tmp/ipfs" "$tmp/current-cid" "$name" > "$tmp/out" 2>&1; then
  echo "unreadable network sequence was accepted" >&2
  exit 1
fi
grep -F "REFUSED: current IPNS record sequence" "$tmp/out" >/dev/null

export FAKE_NETWORK_SEQ=7 FAKE_RESOLVED=/ipfs/bafkreiolder
if "$repo/deploy/refresh-ipns.sh" "$tmp/ipfs" "$tmp/current-cid" "$name" > "$tmp/out" 2>&1; then
  echo "publish that does not resolve was accepted" >&2
  exit 1
fi
grep -F "REFUSED: published at sequence 8 but resolves to /ipfs/bafkreiolder" "$tmp/out" >/dev/null
echo "refresh-ipns: network-sequence publish and all refusal paths passed"
