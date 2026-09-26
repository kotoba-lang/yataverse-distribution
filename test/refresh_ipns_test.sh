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
case "$1" in
  pin) [ "$FAKE_PIN" = yes ] ;;
  name) printf 'Published to %s: %s\n' "$FAKE_NAME" "$6" ;;
  *) exit 1 ;;
esac
SH
chmod 700 "$tmp/ipfs"
printf '%s\n' "$cid" > "$tmp/current-cid"

export FAKE_LOG="$tmp/log" FAKE_PIN=yes FAKE_NAME="$name"
"$repo/deploy/refresh-ipns.sh" "$tmp/ipfs" "$tmp/current-cid" "$name" > "$tmp/out"
grep -Fx "Published to $name: /ipfs/$cid" "$tmp/out" >/dev/null
grep -F "pin ls --type=recursive $cid" "$tmp/log" >/dev/null
grep -F "name publish --key=yataverse-apex" "$tmp/log" >/dev/null

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
echo "refresh-ipns: valid publish and both refusal paths passed"
