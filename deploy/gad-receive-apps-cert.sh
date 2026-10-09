#!/bin/sh
# Install on gad as /usr/local/sbin/receive-yataverse-apps-cert (root, 0755).
# It is the forced command of Xavier's cert-sync key in /root/.ssh/authorized_keys,
# so that key can do nothing but hand gad a renewed `yataverse-apps` certificate.
#
# stdin: a tar holding exactly fullchain.pem and privkey.pem.
# The pair is installed only if the key matches the certificate, the
# certificate is currently valid, and it covers every host in the allow-list
# gad serves (/etc/nginx/yataverse-apps.map). Then nginx -t and a reload; on
# any failure the previous pair is put back and nginx is left as it was.
set -u
DEST=/etc/nginx/tls/yataverse-apps
MAP=/etc/nginx/yataverse-apps.map
SUFFIX=.ipns.220-146-170-114.sslip.io
log() { logger -t yataverse-apps-cert "$*"; echo "$*" >&2; }
fail() { log "REFUSED: $*"; exit 1; }

umask 077
work=$(mktemp -d /etc/nginx/tls/.incoming.XXXXXX) || fail "no temp dir"
trap 'rm -rf "$work"' EXIT

# 64 KiB is far above a chain and key; a larger stream is not a certificate.
head -c 65536 > "$work/in.tar"
names=$(tar -tf "$work/in.tar" 2>/dev/null | sort | tr '\n' ' ')
[ "$names" = "fullchain.pem privkey.pem " ] || fail "unexpected archive members: $names"
mkdir "$work/x" && tar -xf "$work/in.tar" -C "$work/x" --no-same-owner --no-same-permissions || fail "tar"
for f in fullchain.pem privkey.pem; do
    [ -f "$work/x/$f" ] && [ ! -L "$work/x/$f" ] || fail "$f is not a regular file"
done

cert=$work/x/fullchain.pem key=$work/x/privkey.pem
[ "$(openssl x509 -in "$cert" -noout -pubkey 2>/dev/null | sha256sum)" = \
  "$(openssl pkey -in "$key" -pubout 2>/dev/null | sha256sum)" ] || fail "key does not match certificate"
openssl x509 -in "$cert" -noout -checkend 0 >/dev/null || fail "certificate expired"
now=$(date -u +%s)
start=$(date -u -d "$(openssl x509 -in "$cert" -noout -startdate | cut -d= -f2)" +%s) || fail "notBefore"
[ "$start" -le "$now" ] || fail "certificate not yet valid"
grep -oE '^k51[a-z0-9]{59}' "$MAP" | while read -r k51; do
    openssl x509 -in "$cert" -noout -checkhost "$k51$SUFFIX" | grep -q 'does match' \
        || { echo "$k51" > "$work/missing"; break; }
done
[ ! -e "$work/missing" ] || fail "certificate does not cover $(cat "$work/missing")$SUFFIX"

if cmp -s "$cert" "$DEST/fullchain.pem" && cmp -s "$key" "$DEST/privkey.pem"; then
    log "unchanged; nothing to do"
    exit 0
fi

mkdir -p "$DEST/previous"
cp -p "$DEST/fullchain.pem" "$DEST/privkey.pem" "$DEST/previous/" 2>/dev/null
install -m 0644 -o root -g root "$cert" "$DEST/fullchain.pem.new"
install -m 0600 -o root -g root "$key" "$DEST/privkey.pem.new"
mv "$DEST/fullchain.pem.new" "$DEST/fullchain.pem"
mv "$DEST/privkey.pem.new" "$DEST/privkey.pem"

if nginx -t 2>/dev/null; then
    systemctl reload nginx || fail "reload failed"
    log "installed $(openssl x509 -in "$DEST/fullchain.pem" -noout -enddate)"
else
    cp -p "$DEST/previous/fullchain.pem" "$DEST/previous/privkey.pem" "$DEST/" 2>/dev/null
    fail "nginx -t failed with the new pair; previous pair restored, nginx not reloaded"
fi
