# rd_live.py on a VPS (fixed IP)

1. Get a small Linux (Ubuntu 22.04/24.04) VPS with a STATIC public IP, ideally Mumbai/Bangalore.
2. SSH in, then:
   sudo adduser --disabled-password --gecos "" rdlive
   sudo apt update && sudo apt install -y python3-venv git
   sudo -u rdlive -H bash -c 'cd ~ && git clone https://github.com/jharajesh1210/Guruji.git && cd Guruji && python3 -m venv venv && ./venv/bin/pip install requests pandas numpy'
3. Create the env file (secrets live only here):
   sudo cp /home/rdlive/Guruji/deploy/rd_live.env.example /etc/rd_live.env
   sudo nano /etc/rd_live.env        # fill key/secret/Telegram; keep LIVE_ENABLED=NO first
   sudo chmod 600 /etc/rd_live.env
4. On CoinDCX create the API key with THIS server's public IP in the IP field (curl ifconfig.me).
5. Read-only check:
   sudo -u rdlive -H bash -c 'cd ~/Guruji && set -a && . /etc/rd_live.env && set +a && ./venv/bin/python rd_live.py --probe'
6. Start (dry run first):
   sudo cp /home/rdlive/Guruji/deploy/rd_live.service /etc/systemd/system/
   sudo systemctl daemon-reload && sudo systemctl enable --now rd_live
   journalctl -u rd_live -f
7. Go live only after dry-run alerts look right: set LIVE_ENABLED=YES in /etc/rd_live.env, then
   sudo systemctl restart rd_live
   Stop anytime: sudo systemctl stop rd_live   (open trades stay protected only by the exchange emergency stop)
8. Update code: cd /home/rdlive/Guruji && sudo -u rdlive git pull && sudo systemctl restart rd_live
