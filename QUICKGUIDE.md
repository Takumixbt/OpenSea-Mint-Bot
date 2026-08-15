# Quick guide

## Windows

```powershell
git clone https://github.com/Takumixbt/OpenSea-Mint-Bot.git
cd OpenSea-Mint-Bot
python -m pip install -r requirements.txt
Copy-Item .env.example .env
notepad .env
python -m unittest discover -s tests -v
python status.py
python telegram_bot.py
```

Send `/start` in Telegram. Paste an OpenSea collection link into **Schedule
from link** or **Look up an NFT**. Pick quantity, wallets, and time/stage, then
review the exact live confirmation.

Optional extra wallets:

```text
MINT_WALLETS=Backup:0xPRIVATE_KEY;Third:0xPRIVATE_KEY
```

`MAX_MINT_PRICE_NATIVE` limits each mint transaction. `MAX_BUY_PRICE_NATIVE`
separately limits one OpenSea purchase. Gas is additional.

## Ubuntu VPS

```bash
sudo apt update
sudo apt install -y git python3 python3-venv
sudo useradd --system --create-home --shell /usr/sbin/nologin openseabot
sudo git clone https://github.com/Takumixbt/OpenSea-Mint-Bot.git /opt/opensea-mint-bot
sudo python3 -m venv /opt/opensea-mint-bot/.venv
sudo /opt/opensea-mint-bot/.venv/bin/pip install -r /opt/opensea-mint-bot/requirements.txt
sudo cp /opt/opensea-mint-bot/.env.example /opt/opensea-mint-bot/.env
sudo nano /opt/opensea-mint-bot/.env
sudo chown -R openseabot:openseabot /opt/opensea-mint-bot
sudo chmod 600 /opt/opensea-mint-bot/.env
sudo cp /opt/opensea-mint-bot/deploy/opensea-mint-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now opensea-mint-bot
sudo systemctl status opensea-mint-bot --no-pager
```

Stop the Windows bot first. Telegram allows only one polling instance per bot
token. Never upload `.env` or send private keys through Telegram.
