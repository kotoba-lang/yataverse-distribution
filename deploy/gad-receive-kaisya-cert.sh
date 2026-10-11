#!/bin/sh
# Install on gad as /usr/local/sbin/receive-kaisya-domains-cert (root, 0755).
# It is the forced command of Xavier's cert-sync key in /root/.ssh/authorized_keys,
# so that key can do nothing but hand gad a renewed `kaisya-domains` certificate.
#
# stdin: a tar holding exactly fullchain.pem and privkey.pem.
# The pair is installed only if the key matches the certificate, the
# certificate is currently valid, and it covers both kaisya aliases.
# Then nginx -t and a reload; on
# any failure the previous pair is put back and nginx is left as it was.
set -u
DEST=/etc/nginx/tls/kaisya-domains
log() { logger -t kaisya-domains-cert "$*"; echo "$*" >&2; }
fail() { log "REFUSED: $*"; exit 1; }
[ -z "${SSH_ORIGINAL_COMMAND:-}" ] || fail "remote commands are forbidden"

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
for host in kaisya.itonami.cloud kaisya.itonami.app; do
    openssl x509 -in "$cert" -noout -checkhost "$host" | grep -q 'does match' \
        || fail "certificate does not cover $host"
done

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

if nginx -t 2>/dev/null && systemctl reload nginx; then
    log "installed $(openssl x509 -in "$DEST/fullchain.pem" -noout -enddate)"
else
    cp -p "$DEST/previous/fullchain.pem" "$DEST/previous/privkey.pem" "$DEST/" 2>/dev/null
    fail "nginx validation or reload failed; previous pair restored"
fi
