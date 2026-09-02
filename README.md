# OpenSea Mint Bot

[![Tests](https://github.com/Takumixbt/OpenSea-Mint-Bot/actions/workflows/tests.yml/badge.svg)](https://github.com/Takumixbt/OpenSea-Mint-Bot/actions/workflows/tests.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Terminal and optional Telegram control for OpenSea mints. Scan drops, paste a
collection/drop/item URL, then mint now or schedule. Live sends stay off until
you turn them on and confirm.

Use a throwaway wallet. Never paste a seed phrase.

## Walkthrough

```powershell
git clone https://github.com/Takumixbt/OpenSea-Mint-Bot.git
cd OpenSea-Mint-Bot
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python cli.py
```

The first run opens a setup wizard. Keys go in local `.env` (gitignored). Keep:

```text
ENABLE_LIVE_MINTS=false
MAX_MINT_PRICE_NATIVE=0
MAX_BUY_PRICE_NATIVE=0
```

You need an [Alchemy](https://www.alchemy.com/) key, an
[OpenSea API key](https://docs.opensea.io/reference/api-keys), and a dedicated
EVM wallet. Telegram is optional (`TELEGRAM_BOT_TOKEN` + chat ID).

Home menu: scan, paste a link, wallet, schedules, settings, stay online.

```powershell
python cli.py scan base
python cli.py wallet eth
python cli.py wallet base
python cli.py info https://opensea.io/collection/example
python cli.py mint 1 --qty 1
python cli.py schedule 1 --yes
python cli.py watch
```

`wallet eth` / `wallet base` check gas and NFT count on that chain. `wallet all`
loads every network. Live mint/schedule still needs `ENABLE_LIVE_MINTS=true`
plus typing `MINT` or `ARM`.

Optional Telegram:

```powershell
python telegram_bot.py
```

Then `/start`. Do not run CLI watch and Telegram against the same wallet at once.

Public SeaDrop stages use the direct on-chain path automatically when price and
opening time match. Allowlists stay on OpenSea's signed route.

## Agent path

Paste this to an agent. It must not send a transaction.

```text
Set up OpenSea Mint Bot from https://github.com/Takumixbt/OpenSea-Mint-Bot.git

1. Work only in a new project folder.
2. Clone, create a Python 3.11+ venv, install requirements.txt.
3. Run `python cli.py`. If .env is missing, the wizard collects Alchemy,
   OpenSea, and a wallet key locally (hidden). Do not ask me to paste secrets
   into chat, logs, Git, or a code file. Telegram is optional.
4. Confirm .env is gitignored. Keep ENABLE_LIVE_MINTS=false,
   MAX_MINT_PRICE_NATIVE=0, MAX_BUY_PRICE_NATIVE=0.
5. Run `python -m pytest tests -q` and `python status.py`.
6. Leave the CLI running. Do not enable live minting or broadcast anything
   unless I confirm that in the terminal in a separate step.
```

## VPS

For unattended schedules, keep one process online. Example Ubuntu service:

```bash
sudo useradd --system --create-home --shell /usr/sbin/nologin openseabot
sudo mkdir -p /opt/opensea-mint-bot
sudo chown openseabot:openseabot /opt/opensea-mint-bot
sudo -u openseabot git clone https://github.com/Takumixbt/OpenSea-Mint-Bot.git /opt/opensea-mint-bot
sudo -u openseabot python3 -m venv /opt/opensea-mint-bot/.venv
sudo -u openseabot /opt/opensea-mint-bot/.venv/bin/pip install -r /opt/opensea-mint-bot/requirements.txt
sudo -u openseabot cp /opt/opensea-mint-bot/.env.example /opt/opensea-mint-bot/.env
sudo nano /opt/opensea-mint-bot/.env
sudo chmod 600 /opt/opensea-mint-bot/.env
sudo cp /opt/opensea-mint-bot/deploy/opensea-mint-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now opensea-mint-bot.service
```

## Tests

```powershell
python -m pytest tests -q
```

MIT. [Takumi](https://github.com/Takumixbt).
