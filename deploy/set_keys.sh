#!/usr/bin/env bash
# Re-enter the CoinDCX API key + secret on the server (hidden input), then re-test. No orders are placed.
# Usage: cd ~/Guruji && git pull && bash deploy/set_keys.sh
set -u
cd "$(dirname "$0")/.."
ENVF=/etc/rd_live.env
clean() { printf %s "$1" | sed 's/\x1b\[20[01]~//g' | tr -cd 'A-Za-z0-9'; }
ask() {
  local v="" yn=""
  while true; do
    read -r -s -p "$1 (hidden) - paste, then press Enter: " v </dev/tty; echo
    v=$(clean "$v"); echo "  got ${#v} characters"
    if [ "${#v}" -ge 16 ]; then ASKED="$v"; return; fi
    read -r -p "  Too short. Type y to type it VISIBLY (do not screenshot), or Enter to paste again: " yn </dev/tty
    if [ "$yn" = "y" ]; then read -r -p "$1 (VISIBLE): " v </dev/tty; v=$(clean "$v"); echo "  got ${#v} characters"; [ "${#v}" -ge 16 ] && { ASKED="$v"; return; }; fi
  done
}
echo "Step 1: copy ONLY the API KEY from CoinDCX, then paste below."
ask "CoinDCX API KEY"; KEY="$ASKED"
echo
echo "Step 2: now go back to CoinDCX / your notes and copy the SECRET KEY (a different value), then paste below."
while true; do
  ask "CoinDCX SECRET KEY"; SECRET="$ASKED"
  if [ "$SECRET" = "$KEY" ]; then
    echo "  !! The secret is identical to the API key. That is wrong - the secret is a different value. Copy the SECRET KEY and try again."
  else
    break
  fi
done
sudo tee "$ENVF" >/dev/null <<ENV
COINDCX_API_KEY=$KEY
COINDCX_API_SECRET=$SECRET
TG_TOKEN=
TG_CHAT_ID=
LIVE_ENABLED=NO
LIVE_MARGIN_USDT=10
MAX_LIVE_OPEN=1
MIN_24H_VOLUME_USDT=10000000
ENV
sudo chmod 600 "$ENVF"; sudo chown root:root "$ENVF"
echo "Saved. Testing..."
set -a; eval "$(sudo cat "$ENVF")"; set +a
./venv/bin/python rd_live.py --probe
sudo systemctl restart rd_live && echo "service restarted: $(systemctl is-active rd_live)"
