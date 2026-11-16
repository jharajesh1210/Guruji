#!/usr/bin/env bash
# One-shot setup of rd_live.py on the Ubuntu VPS (run as the normal 'ubuntu' user; uses sudo).
# Starts in DRY RUN mode (no real orders). Secrets are typed hidden and stored only in /etc/rd_live.env.
set -euo pipefail
REPO="https://github.com/jharajesh1210/Guruji.git"
DIR="$HOME/Guruji"
ENVF="/etc/rd_live.env"

echo "== 1/7 packages"
sudo apt-get update -y
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y python3-venv git

echo "== 2/7 swap (safety for 1 GB RAM)"
if ! swapon --show | grep -q .; then
  sudo fallocate -l 1G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile >/dev/null && sudo swapon /swapfile
  grep -q '/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
fi

echo "== 3/7 code"
if [ -d "$DIR/.git" ]; then git -C "$DIR" pull --ff-only; else git clone --depth 1 "$REPO" "$DIR"; fi

echo "== 4/7 python packages (takes a minute)"
python3 -m venv "$DIR/venv"
"$DIR/venv/bin/pip" install -q requests pandas numpy

echo "== 5/7 secrets (typing is hidden; paste and press Enter)"
read -r -s -p "CoinDCX API key: " KEY </dev/tty; echo " (got ${#KEY} characters)"
read -r -s -p "CoinDCX API secret: " SECRET </dev/tty; echo " (got ${#SECRET} characters)"
read -r -p "Telegram bot token (Enter to skip): " TGT </dev/tty
read -r -p "Telegram chat id (Enter to skip): " TGC </dev/tty
if [ -z "$KEY" ] || [ -z "$SECRET" ]; then echo "API key/secret missing - stopping."; exit 1; fi

sudo tee "$ENVF" >/dev/null <<ENV
COINDCX_API_KEY=$KEY
COINDCX_API_SECRET=$SECRET
TG_TOKEN=$TGT
TG_CHAT_ID=$TGC
LIVE_ENABLED=NO
LIVE_MARGIN_USDT=10
MAX_LIVE_OPEN=1
MIN_24H_VOLUME_USDT=10000000
ENV
sudo chmod 600 "$ENVF"; sudo chown root:root "$ENVF"

echo "== 6/7 read-only checks"
echo -n "server public IP (IPv4 default route): "; curl -s https://ifconfig.me; echo
curl -s -o /dev/null -w "public.coindcx.com futures prices: HTTP %{http_code}\n" https://public.coindcx.com/market_data/v3/current_prices/futures/rt || true
COINDCX_API_KEY="$KEY" COINDCX_API_SECRET="$SECRET" "$DIR/venv/bin/python" "$DIR/rd_live.py" --probe || echo "PROBE FAILED - tell Claude the lines above (hide key/secret)."

echo "== 7/7 service (dry run)"
sudo tee /etc/systemd/system/rd_live.service >/dev/null <<UNIT
[Unit]
Description=rd_live.py CoinDCX live runner
After=network-online.target
Wants=network-online.target

[Service]
User=$USER
WorkingDirectory=$DIR
EnvironmentFile=$ENVF
ExecStart=/bin/bash $DIR/deploy/rd_live_run.sh
Restart=always
RestartSec=30

[Install]
WantedBy=multi-user.target
UNIT
sudo systemctl daemon-reload
sudo systemctl enable --now rd_live
sleep 5
echo "service: $(systemctl is-active rd_live)"
echo "DONE. Mode is DRY RUN (LIVE_ENABLED=NO): no real orders. See logs with: sudo journalctl -u rd_live -n 30 --no-pager"
