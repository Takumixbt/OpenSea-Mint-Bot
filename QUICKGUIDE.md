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
python recon_check.py
python telegram_bot.py
```

In Telegram, send `/start`. Use **Find today's mints** to choose one network,
or **Schedule a mint** and paste a known OpenSea collection/drop URL. Pick the
stage and quantity, review the exact total mint value, then confirm.

Use **Settings** to upload the NFT mint-card background and set the maximum mint
price. `0` means free-only. Gas is separate.

## Ubuntu VPS

Run these as a sudo user:

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
```

Verify it:

```bash
sudo systemctl status opensea-mint-bot --no-pager
sudo journalctl -u opensea-mint-bot -n 50 --no-pager
```

Stop the Windows copy before starting the VPS copy. Telegram permits only one
polling instance per bot token.

## Required `.env` values

```text
ALCHEMY_API_KEY=...
PRIVATE_KEY=0x...
WALLET_ADDRESS=0x...
OPENSEA_API_KEY=...
TELEGRAM_BOT_TOKEN=...
TELEGRAM_ALLOWED_CHAT_ID=...
ENABLE_LIVE_MINTS=true
MAX_MINT_PRICE_NATIVE=0
```

Never upload `.env` to GitHub or send the private key to Telegram.
