# OpenSea Mint Bot

An open-source, Telegram-controlled bot for discovering OpenSea Drops,
researching collections, scheduling supported mints, and buying OpenSea
listings with explicit limits and confirmation steps.

The normal setup runs on your own Windows, macOS, or Linux computer. A VPS is
optional when the computer must stay online for scheduled mints.

## Important safety facts

This project can sign and broadcast real blockchain transactions when you
enable live mode. Use a separate, low-value wallet. Never use a wallet that
holds funds you cannot afford to lose, and never paste a seed phrase into this
project or into Telegram.

The repository contains no wallet keys, API keys, Telegram tokens, chat IDs,
VPS addresses, or personal filesystem paths. Secrets belong only in your local
`.env` file, which is ignored by Git.

The safe defaults are:

- live transaction execution is disabled;
- the mint price cap is `0`, so paid execution is blocked;
- the buy price cap is `0`, so purchases are blocked;
- one wallet and quantity one are used unless you configure more.

Scanning and research can run with live mode disabled.

## What the Python bot does

From Telegram, you can:

- scan one configured EVM network at a time for OpenSea live mints and mints
  opening today;
- see free, paid, public, and restricted stages;
- paste an OpenSea collection or drop link for research;
- schedule a supported OpenSea Drop mint;
- use a safe generic route for some verified external contracts;
- choose quantity and one or more configured wallets;
- check wallet balances, NFT counts, and mint transaction receipts;
- preview the cheapest OpenSea listing and buy it only after confirmation;
- configure the NFT receipt-card background and accent color.

The scanner uses OpenSea's official Drops feeds. A collection that is merely
indexed or traded on OpenSea is not automatically an OpenSea Drop. Custom
puzzles, CAPTCHAs, backend signatures, unknown Merkle proofs, and ambiguous
contract arguments are refused instead of guessed.

## Requirements

- Python 3.11 or newer
- An EVM wallet created specifically for this bot
- Native gas on the network you will use
- An Alchemy API key for RPC access
- An OpenSea API key for discovery and OpenSea Drop transaction data
- A Telegram bot token if you want Telegram control

OpenSea API documentation: <https://docs.opensea.io/reference/api-overview>

## Windows setup: recommended first run

Open PowerShell and choose a folder for the project. The path below is only an
example; use any folder you own.

```powershell
git clone https://github.com/YOUR_GITHUB_USERNAME/OpenSea-Mint-Bot.git
cd OpenSea-Mint-Bot
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
Copy-Item .env.example .env
notepad .env
```

Fill in `.env` as described below. Do not rename it and do not commit it.

Then validate the setup:

```powershell
python -m unittest discover -s tests -v
python status.py
```

Start the Telegram controller:

```powershell
python telegram_bot.py
```

Keep that PowerShell window open while you use the bot. Only one process may
poll a Telegram token at a time.

## Configure `.env`

Copy `.env.example` and replace the placeholders. The minimum values are:

```text
ALCHEMY_API_KEY=your_alchemy_key
PRIVATE_KEY=0xyour_bot_wallet_private_key
WALLET_ADDRESS=0xyour_bot_wallet_address
OPENSEA_API_KEY=your_opensea_key
TELEGRAM_BOT_TOKEN=your_botfather_token
TELEGRAM_ALLOWED_CHAT_ID=your_private_chat_id
```

Get keys from the providers directly. Do not use a key copied from a public
example:

- Alchemy: <https://www.alchemy.com/>
- OpenSea API: <https://docs.opensea.io/reference/api-keys>
- Telegram bot token: message `@BotFather` and use `/newbot`

The private key must belong to the same wallet as `WALLET_ADDRESS`. The bot
checks this relationship during startup. The bot never needs a seed phrase.

For local testing, leave these safety settings unchanged:

```text
ENABLE_LIVE_MINTS=false
MAX_MINT_PRICE_NATIVE=0
MAX_BUY_PRICE_NATIVE=0
```

To permit paid mint execution, set a deliberate hard cap in the native coin
of the target chain. For example, `0.02` permits up to `0.02` ETH on an ETH
chain. It does not instruct the bot to spend that amount. Gas is separate and
must also be available.

To permit OpenSea purchases, set `MAX_BUY_PRICE_NATIVE` separately. Mint and
buy limits are never interchangeable.

To use several wallets, keep the keys local and use semicolon-separated
entries:

```text
MINT_WALLETS=Backup:0xBACKUP_PRIVATE_KEY;Second:0xSECOND_PRIVATE_KEY
```

The bot derives and displays public addresses only. Never put these values in
Telegram messages, screenshots, issues, or pull requests.

Set `DISCOVERY_UTC_OFFSET_HOURS` to the fixed offset you want for the
midnight-to-midnight scan. For example, West Africa Time is `1`; UTC is `0`.

## Telegram first use

1. Start the process with `python telegram_bot.py`.
2. Send `/start` to your bot.
3. Use **My wallet** to confirm the displayed public address and gas balance.
4. Use **Find OpenSea mints**, select one network, and open a result.
5. Use **Schedule from link** when you already know the collection URL.
6. Choose the stage, quantity, wallets, and price cap.
7. Review the confirmation screen before enabling any live action.

The scanner is chain-specific. It shows OpenSea Drops, not every collection
that happens to have a secondary-market page. Paid mints are included when
OpenSea exposes them; a price cap only controls whether execution is allowed.

## Enabling a real mint

Do this only after the read-only checks work:

```text
ENABLE_LIVE_MINTS=true
MAX_MINT_PRICE_NATIVE=your_explicit_cap
```

Restart the bot after changing `.env`. The Telegram flow still requires an
explicit confirmation. Before signing, the Python bot checks the chain,
wallet balance, exact value, gas envelope, stage/quantity limits, and a
transaction simulation where the route supports it.

A successful broadcast is not a guarantee of inclusion. The network can still
reject, replace, or reorder transactions, and a collection can sell out.

## Local computer versus VPS

For a one-off mint, your computer must stay on, connected, and running the
Python process until the transaction is sent. OpenSea, Chrome, and MetaMask do
not need to remain open for the Python route.

Use a VPS when you want schedules to continue while your computer is off. A
VPS does not make a transaction guaranteed or change OpenSea's rate limits. It
only keeps the process online.

The included systemd unit is hardened with a non-root service account,
`NoNewPrivileges`, `ProtectHome`, `PrivateTmp`, and a restricted writable
state directory. See [QUICKGUIDE.md](QUICKGUIDE.md) for the complete VPS
procedure.

## VPS setup: Ubuntu

Use a fresh non-root service account on your own server. Replace the example
repository URL and server hostname with your values.

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
sudo systemctl status opensea-mint-bot.service --no-pager
```

From Windows, copy a completed `.env` over SSH only if you understand the
risk and have verified the destination. Prefer entering it directly on the
server so it does not remain in shell history or transfer logs.

Useful VPS commands:

```bash
sudo systemctl restart opensea-mint-bot.service
sudo journalctl -u opensea-mint-bot.service -f
sudo systemctl stop opensea-mint-bot.service
```

Never run two copies with the same Telegram token. If Telegram reports a
`getUpdates` conflict, stop the duplicate process first.

## Agent setup prompt

You can give the following prompt to a coding agent in a terminal. Replace
`YOUR_REPOSITORY_URL` with the repository URL you trust. The prompt tells the
agent to leave live execution off until you personally approve it.

```text
Set up OpenSea Mint Bot from YOUR_REPOSITORY_URL on this computer.

Rules:
1. Work only inside a new project folder and never modify unrelated projects.
2. Clone the repository, create a Python 3.11+ virtual environment, and install
   requirements.txt.
3. Copy .env.example to .env.
4. Ask me to enter API keys, wallet values, and the Telegram token locally. Do
   not ask me to paste private keys, seed phrases, bot tokens, or API keys into
   chat, logs, Git, or a code file.
5. Confirm .env is ignored by Git and run a secret scan before any commit.
6. Keep ENABLE_LIVE_MINTS=false, MAX_MINT_PRICE_NATIVE=0, and
   MAX_BUY_PRICE_NATIVE=0. Do not enable live execution or send a transaction.
7. Run `python -m unittest discover -s tests -v` and `python status.py`.
8. Start `python telegram_bot.py` only after the checks pass and tell me to
   send /start to the bot.
9. Explain what is still missing if status.py reports a blocked item.
10. Do not create, modify, or publish any wallet transaction without a separate
    confirmation from me in the terminal session.
```

## Tampermonkey browser companion

The optional `opensea_mint_assist.user.js` is a browser helper for one visible
OpenSea page. It can watch a page, wait for a chosen time, identify a visible
Mint/Claim control, and optionally click it once. It never reads private keys,
uses the OpenSea API, controls MetaMask/Rabby, or presses the final wallet
confirmation.

It therefore cannot replace the Python bot's Telegram control, VPS schedules,
multi-wallet execution, cross-chain scanner, balance checks, or transaction
receipt tracking. The browser must remain open. Read
[TAMPERMONKEY.md](TAMPERMONKEY.md) before enabling its optional auto-click
features.

## Troubleshooting

### `Telegram polling error: Conflict`

Stop every other process using the same bot token, then start exactly one
controller.

### `OPENSEA_API_KEY is missing`

Confirm that the file is named `.env`, it is in the project root, and the key
is not still a placeholder. Restart the process after editing it.

### A collection is visible on OpenSea but not in `/scan`

The collection may be secondary-market-only, externally hosted, sold out, or
not published in OpenSea's Drops feeds. Paste its collection URL into the
research route. The bot will show whether an OpenSea Drop route is available;
it will not pretend that a secondary listing is a mint route.

### The bot says the wallet is not eligible

The stage may require an allowlist, holder status, signature, or another
project-specific proof. Do not bypass the check by guessing calldata.

### The computer was turned off

Schedules run only while the Python process is alive. Use the VPS systemd
option for continuous availability.

## Development checks

Run the full local suite before submitting changes:

```powershell
python -m compileall -q .
python -m unittest discover -s tests -v
```

Do not include `.env`, `state/`, logs, browser sessions, wallet files, or
generated deployment output in a pull request.

## License and contributions

This project is released under the MIT License. Contributions should preserve
the confirmation gates, price caps, secret handling, and refusal of unknown
mint routes.
