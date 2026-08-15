# NFT Mint Bot

A Telegram-controlled EVM NFT bot. Paste an OpenSea collection link to:

- mint an OpenSea Drop;
- mint through a simple verified external collection contract;
- choose quantity and one or many wallets;
- schedule a launch while the bot runs on a PC or VPS;
- research the collection; or
- buy its cheapest active OpenSea listing at an exact confirmed price.

Custom puzzles, CAPTCHAs, backend signatures, and unknown allowlist proofs need
a dedicated adapter. The bot reports those routes as unsupported instead of
guessing a transaction.

## Install

Install Python 3.11 or newer, then run:

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

Only one machine may run the same Telegram token.

## Setup

Required `.env` values:

```text
ALCHEMY_API_KEY=...
PRIVATE_KEY=0x...
WALLET_ADDRESS=0x...
OPENSEA_API_KEY=...
TELEGRAM_BOT_TOKEN=...
TELEGRAM_ALLOWED_CHAT_ID=...
ENABLE_LIVE_MINTS=true
MAX_MINT_PRICE_NATIVE=0
MAX_BUY_PRICE_NATIVE=0
```

Add extra wallets with private keys separated by semicolons:

```text
MINT_WALLETS=Backup:0xPRIVATE_KEY;Third:0xPRIVATE_KEY
```

The bot derives their addresses. Keys stay in `.env`, never in Telegram or
schedule files. Use separate low-value wallets.

## Telegram

Send `/start`, then:

- **Find today’s mints** scans one OpenSea network calendar.
- **Schedule from link** accepts an OpenSea collection/drop link and resolves
  OpenSea-hosted or safe verified-contract mint routes.
- **Quantity** is the number minted by each selected wallet.
- **Wallets** chooses one or all configured wallets for parallel transactions.
- **Look up an NFT** shows research, mint routes, and **Buy now** when an active
  listing exists.
- **Settings** controls mint and purchase price caps plus mint-card appearance.
- **My wallet** lets you inspect each configured wallet.

Every live action requires a confirmation screen. Before signing, the bot
checks the exact value, chain, balance, gas envelope, price cap, wallet limit,
and transaction simulation. A changed listing price or mint value is refused.

The `/scan` calendar is not an exhaustive index of externally hosted launches.
If you already know a project, paste its OpenSea collection link directly.

## VPS

Chrome, MetaMask, and OpenSea do not need to stay open. The Python service must
stay online for schedules, so use the included systemd unit on a VPS. See
[QUICKGUIDE.md](QUICKGUIDE.md).

Never commit `.env`. No bot can guarantee supply, eligibility, API uptime, gas,
or inclusion in the first block.
