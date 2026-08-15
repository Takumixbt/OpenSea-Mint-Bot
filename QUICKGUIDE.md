# Quick guide

This is the short version. For explanations and the terminal-agent prompt,
read [README.md](README.md).

## Run locally on Windows

```powershell
git clone https://github.com/YOUR_GITHUB_USERNAME/OpenSea-Mint-Bot.git
cd OpenSea-Mint-Bot
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
notepad .env
python -m unittest discover -s tests -v
python status.py
python telegram_bot.py
```

Start with these safe values:

```text
ENABLE_LIVE_MINTS=false
MAX_MINT_PRICE_NATIVE=0
MAX_BUY_PRICE_NATIVE=0
```

Use `/start` in Telegram. The bot scans one network at a time. It includes
paid and restricted OpenSea stages; the price cap only controls execution.

Public SeaDrop stages use the faster direct on-chain path automatically when
their on-chain price and opening time match the scan. Allowlists and custom
routes keep their required OpenSea/contract checks.

## Run on Ubuntu as a VPS service

```bash
sudo apt update
sudo apt install -y git python3 python3-venv
sudo useradd --system --create-home --shell /usr/sbin/nologin openseabot
sudo mkdir -p /opt/opensea-mint-bot
sudo chown openseabot:openseabot /opt/opensea-mint-bot
sudo -u openseabot git clone https://github.com/YOUR_GITHUB_USERNAME/OpenSea-Mint-Bot.git /opt/opensea-mint-bot
sudo -u openseabot python3 -m venv /opt/opensea-mint-bot/.venv
sudo -u openseabot /opt/opensea-mint-bot/.venv/bin/pip install -r /opt/opensea-mint-bot/requirements.txt
sudo -u openseabot cp /opt/opensea-mint-bot/.env.example /opt/opensea-mint-bot/.env
sudo nano /opt/opensea-mint-bot/.env
sudo chmod 600 /opt/opensea-mint-bot/.env
sudo cp /opt/opensea-mint-bot/deploy/opensea-mint-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now opensea-mint-bot.service
```

Check it with:

```bash
sudo systemctl status opensea-mint-bot.service --no-pager
sudo journalctl -u opensea-mint-bot.service -f
```

Only one process may use a Telegram token. Chrome and MetaMask are not needed
for the Python VPS route.

## Fast direct route

The bot automatically uses `opensea_direct_executor.py` for compatible public
SeaDrop stages. It runs from Python and Telegram, so Chrome, OpenSea tabs, and
wallet extensions are not needed. See [DIRECT_EXECUTION.md](DIRECT_EXECUTION.md)
for the setup and timing details.
