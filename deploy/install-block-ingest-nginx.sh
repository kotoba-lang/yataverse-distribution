#!/bin/sh
# Owner step on xavier (needs sudo): publish block-ingest at
# https://yataverse-blocks.220-146-170-114.sslip.io. The service itself is a
# user unit (~/.config/systemd/user/block-ingest.service) and needs no root.
set -eu
D="${1:-$HOME/.local/share/block-ingest/nginx}"
sudo install -m 0644 "$D/xavier-block-ingest-limit.conf" /etc/nginx/conf.d/block-ingest-limit.conf
sudo install -m 0644 "$D/xavier-block-ingest-http.conf" /etc/nginx/sites-available/yataverse-blocks-http
sudo ln -sf /etc/nginx/sites-available/yataverse-blocks-http /etc/nginx/sites-enabled/yataverse-blocks-http
sudo nginx -t && sudo systemctl reload nginx
sudo certbot certonly --webroot -w /var/www/letsencrypt -d yataverse-blocks.220-146-170-114.sslip.io --non-interactive --agree-tos --keep-until-expiring
sudo install -m 0644 "$D/xavier-block-ingest.conf" /etc/nginx/sites-available/yataverse-blocks
sudo ln -sf /etc/nginx/sites-available/yataverse-blocks /etc/nginx/sites-enabled/yataverse-blocks
sudo nginx -t && sudo systemctl reload nginx
curl -fsS https://yataverse-blocks.220-146-170-114.sslip.io/health && echo
