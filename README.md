# OpenSea Mint Bot

[![Tests](https://github.com/Takumixbt/OpenSea-Mint-Bot/actions/workflows/tests.yml/badge.svg)](https://github.com/Takumixbt/OpenSea-Mint-Bot/actions/workflows/tests.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

This is a small Python bot for finding OpenSea mints and sending them from a
wallet you control. You can run it as a numbered terminal menu, as one-shot
commands, or as an optional Telegram bot. The same mint engine sits behind all
three. It is meant to live on your own computer, or on a cheap VPS if a
schedule has to fire while you are away.

It is not a browser extension, not a clicker, and not a custodial service.
Nothing is hosted for you. Keys never leave the machine that runs the process.

Maintained by [Takumi](https://github.com/Takumixbt).

## What it actually does

The bot reads OpenSea's public Drops feeds, groups mint windows by network, and
lets you inspect a collection, drop, item, or asset URL. From there you can
mint immediately or arm a one-time schedule for the opening second.

Two execution routes exist, and you do not pick them by hand:

- **Public SeaDrop stages** whose on-chain price and opening time match the
  preview are signed during warm-up and broadcast as a raw transaction. That is
  the fast path.
- **Hosted allowlist / signature stages** stay on OpenSea's own calldata route,
  because those mints need OpenSea eligibility. The bot will not invent calldata
  or skip an allowlist.

A collection that merely trades on OpenSea is not a mint. If the bot cannot
resolve a safe route, it stops. Marketplace listings, custom proof flows, and
guessed contract calls are refused.

Scanning and research work with live mode off. Signing and broadcasting do not.

## Safety, before anything else

This program can spend gas and mint NFTs when you enable live mode. Use a
separate, low-value wallet. Never import a seed phrase. Never paste a private
key into Telegram, a chat, GitHub, or an issue.

The repository ships with live execution **off**. Paid mints and secondary buys
are blocked by price caps of `0`. Quantity defaults to one. Those defaults are
the point. Change them only after you have watched a dry run and you understand
the exact collection, chain, quantity, and value.

Secrets belong in a local `.env` file, which Git ignores. The setup wizard can
write that file for you and will hide the private key while you type it. The
public address is derived from the key, so you do not have to copy it by hand.

Do not run the terminal watcher and the Telegram bot against the same wallet at
the same time. Both would try to fire the same schedules.

## Walkthrough (human)

You need Python 3.11 or newer, an [Alchemy](https://www.alchemy.com/) API key
for RPC, an [OpenSea API key](https://docs.opensea.io/reference/api-keys), and
a throwaway EVM wallet with a little native gas on the chain you care about.
Telegram is optional.

On Windows, from a folder you own:

```powershell
git clone https://github.com/Takumixbt/OpenSea-Mint-Bot.git
cd OpenSea-Mint-Bot
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python cli.py
```

On macOS or Linux the same idea applies: clone, make a venv, install
`requirements.txt`, then `python cli.py`.

The first launch walks you through Alchemy, the wallet key (hidden), and
OpenSea. Leave live minting off:

```text
ENABLE_LIVE_MINTS=false
MAX_MINT_PRICE_NATIVE=0
MAX_BUY_PRICE_NATIVE=0
```

You should see an ASCII **OPENSEA MINT BOT** banner and a home menu:

1. Scan for mints — pick a busy network, then a window
2. Paste an OpenSea link — collection, drop, item, or asset
3. My wallet — gas and NFT count
4. Schedules
5. History
6. Settings / setup
7. Stay online so armed schedules can fire

Scan Base (or Ethereum), open a window, and use **Mint now** or **Schedule**.
While live mode is off, that is a preview. Nothing is signed.

Useful one-shots if you would rather not sit in the menu:

```powershell
python cli.py scan base
python cli.py wallet eth
python cli.py wallet base
python cli.py info https://opensea.io/collection/example
python cli.py mint 1 --qty 1
python cli.py schedule 1 --yes
python cli.py watch
```

`wallet eth` and `wallet base` are the fast checks: native balance plus NFT
count on that one chain. `wallet all` walks every configured network and is
slower; some chains have no OpenSea NFT index or a flaky RPC, and those rows
show a short note instead of a stack trace.

`python main.py` is the same CLI. `python status.py` is a read-only health
report. It never signs.

### Telegram, if you want it

Create a bot with `@BotFather`, put `TELEGRAM_BOT_TOKEN` and
`TELEGRAM_ALLOWED_CHAT_ID` in `.env`, then:

```powershell
python telegram_bot.py
```

Send `/start`. Scan, paste a link, schedule, and confirm the same way. Cards
show artwork when OpenSea has it. Only one process may poll a given bot token.

### Turning live mode on

Do this only after the dry run looks right.

```text
ENABLE_LIVE_MINTS=true
MAX_MINT_PRICE_NATIVE=your_explicit_cap
```

Restart the process. The CLI still asks you to type `MINT` or `ARM`. Telegram
still asks for a confirmation tap. `--yes` on a one-shot command is that same
confirmation. Before a send, the bot checks chain, balance, value, gas
envelope, quantity limits, and a simulation where the route supports it.

A transaction hash means the network accepted the broadcast. It does not mean
the mint landed, and it does not mean the drop still has supply.

Schedules only fire while the Python process is alive. For a one-off drop,
leave the window open (menu **7**, or `python cli.py watch`). For unattended
schedules, use a VPS and the systemd unit in `deploy/`.

## Agent path

Paste this to a coding agent. It must not send a transaction or ask you to
paste secrets into chat.

```text
Set up OpenSea Mint Bot from https://github.com/Takumixbt/OpenSea-Mint-Bot.git

Work only inside a new project folder. Do not touch unrelated repos.

Clone the repository. Create a Python 3.11+ virtual environment and install
requirements.txt.

Run `python cli.py`. If .env is missing, the first-run wizard collects an
Alchemy key, a wallet private key (hidden in the local terminal), and an
OpenSea API key. Derive the public address from the key. Telegram is optional.
Never ask me to paste private keys, seed phrases, bot tokens, or API keys into
chat, logs, Git, or a source file.

Confirm .env is gitignored. Keep ENABLE_LIVE_MINTS=false,
MAX_MINT_PRICE_NATIVE=0, and MAX_BUY_PRICE_NATIVE=0.

Run `python -m pytest tests -q` and `python status.py`. Start the CLI and leave
it running. Explain anything status.py marks as blocked.

Do not enable live minting, sign, or broadcast anything unless I confirm that
in a separate terminal message. Do not run telegram_bot.py and cli.py watch
against the same wallet at the same time.
```

## VPS (Ubuntu)

A VPS does not make inclusion guaranteed. It only keeps the process online.

```bash
sudo apt update
sudo apt install -y git python3 python3-venv
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

```bash
sudo systemctl status opensea-mint-bot.service --no-pager
sudo journalctl -u opensea-mint-bot.service -f
```

Enter `.env` on the server. Prefer not to scp a file that contains a private
key.


## Tests

```powershell
python -m pytest tests -q
```

Do not commit `.env`, `state/`, or wallet files.

MIT License.
