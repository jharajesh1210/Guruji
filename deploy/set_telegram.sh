#!/usr/bin/env bash
# Add Telegram alerts on the server. Asks for the bot token and chat id (both hidden).
# Usage: cd ~/Guruji && git pull && bash deploy/set_telegram.sh
set -u
cd "$(dirname "$0")/.."
ENVF=/etc/rd_live.env
rd() { local v; read -r -s -p "$1 (hidden) - paste, then press Enter: " v </dev/tty; echo >&2; printf %s "$v" | sed 's/\x1b\[20[01]~//g' | tr -cd "$2"; }
T=$(rd "Telegram BOT TOKEN" 'A-Za-z0-9:_-'); echo "  got ${#T} characters"
case "$T" in [0-9]*:*) ;; *) echo "  !! That does not look like a bot token (123456789:AAxxxx). Run again."; exit 1;; esac
CID=$(rd "Telegram CHAT ID (numbers only)" '0-9-'); echo "  got ${#CID} characters"
[ "${#CID}" -ge 5 ] || { echo "  !! Chat id too short. Run again."; exit 1; }
sudo sed -i '/^TG_TOKEN=/d;/^TG_CHAT_ID=/d' "$ENVF"
printf 'TG_TOKEN=%s\nTG_CHAT_ID=%s\n' "$T" "$CID" | sudo tee -a "$ENVF" >/dev/null
sudo chmod 600 "$ENVF"
OUT=$(curl -s -o /dev/null -w '%{http_code}' "https://api.telegram.org/bot${T}/sendMessage" --data-urlencode "chat_id=${CID}" --data-urlencode "text=rd_live server connected (DRY RUN)")
echo "  test message HTTP: $OUT (200 = message reached your Telegram)"
sudo systemctl restart rd_live && echo "service restarted: $(systemctl is-active rd_live)"
