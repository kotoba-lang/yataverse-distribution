#!/bin/sh
# certbot deploy hook on Xavier: install as
# /etc/letsencrypt/renewal-hooks/deploy/yataverse-apps-to-gad.sh (root, 0755).
#
# certbot runs every deploy hook after any lineage renews, with
# RENEWED_LINEAGE set. Only `yataverse-apps` is sent: gad serves a copy of it
# for the standby app entrance and cannot renew it itself (gad-public-apps.conf).
# The dedicated key can only run gad's receive-yataverse-apps-cert, which
# validates the pair before installing it. A failure is logged and does not
# fail the renewal on Xavier.
set -u
case "${RENEWED_LINEAGE:-}" in
    */live/yataverse-apps) ;;
    *) exit 0 ;;
esac

KEY=/root/.ssh/yataverse-apps-cert-sync
# gad's LAN address, not its tailnet one: on the tailnet address port 22 is
# answered by Tailscale SSH, which authorizes by tailnet policy and ignores
# authorized_keys, so the forced command would not apply. On the LAN, sshd
# answers and the key is held to receive-yataverse-apps-cert (from=192.168.1.28).
GAD=192.168.1.16
out=$(tar -chf - -C "$RENEWED_LINEAGE" fullchain.pem privkey.pem |
      ssh -i "$KEY" -o BatchMode=yes -o IdentitiesOnly=yes -o ConnectTimeout=15 \
          -o StrictHostKeyChecking=yes "root@$GAD" 2>&1)
status=$?
logger -t yataverse-apps-cert "copy to gad exit=$status: $out"
exit 0
