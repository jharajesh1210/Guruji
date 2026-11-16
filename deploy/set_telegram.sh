#!/usr/bin/env bash
# Add Telegram alerts on the server. Asks only for the bot token (hidden); finds the chat id by itself.
# Usage: cd ~/Guruji && git pull && bash deploy/set_telegram.sh
set -u
cd "$(dirname "$0")/.."
ENVF=/etc/rd_live.env
echo "Before this: open your Telegram bot (the one from BotFather) and send it any message, e.g. hi."
read -r -p "Done? Press Enter to continue " _ </dev/tty
read -r -s -p "Telegram BOT TOKEN (hidden) - paste, then press Enter: " T </dev/tty; echo
T=$(printf %s "$T" | sed 's/\x1b\[20[01]~//g' | tr -cd 'A-Za-z0-9:_-')
echo "  got ${#T} characters"
case "$T" in [0-9]*:*) ;; *) echo "  !! That does not look like a bot token (should look like 123456789:AAxxxx). Run again."; exit 1;; esac
R=$(curl -s "https://api.telegram.org/bot${T}/getUpdates")
CID=$(printf %s "$R" | python3 -c 'import sys,json
try:
    d=json.load(sys.stdin); r=[u for u in d.get("result",[]) if "message" in u]
    print(r[-1]["message"]["chat"]["id"] if r else "")
except Exception: print("")')
if [ -z "$CID" ]; then
  echo "  Could not find a chat. Make sure you sent a message to THIS bot, then run again."; exit 1
fi
echo "  chat id found: $CID"
sudo sed -i '/^TG_TOKEN=/d;/^TG_CHAT_ID=/d' "$ENVF"
printf 'TG_TOKEN=%s\nTG_CHAT_ID=%s\n' "$T" "$CID" | sudo tee -a "$ENVF" >/dev/null
sudo chmod 600 "$ENVF"
OUT=$(curl -s -o /dev/null -w '%{http_code}' "https://api.telegram.org/bot${T}/sendMessage" --data-urlencode "chat_id=${CID}" --data-urlencode "text=rd_live server connected (DRY RUN)")
echo "  test message HTTP: $OUT (200 = check your Telegram)"
sudo systemctl restart rd_live && echo "service restarted: $(systemctl is-active rd_live)"
