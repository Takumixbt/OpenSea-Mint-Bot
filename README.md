# OpenSea Mint Bot

An open-source, Telegram-controlled bot for discovering OpenSea mints, showing
concise mint information, and scheduling EVM mints from collection, drop, item,
or NFT asset links with explicit limits and confirmation steps.

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

- open a network picker that shows how many drops each network actually has
  right now, busiest first, so you never scan an empty chain by accident;
- scan one network, or all of them into a chain-grouped summary;
- see free, paid, public, and restricted stages;
- paste an OpenSea collection, drop, item, or NFT asset link (or collection
  slug) for mint-route resolution;
- schedule any active/upcoming hosted stage, or a compatible public SeaDrop /
  verified-contract route resolved from that OpenSea link;
- use direct SeaDrop for compatible public stages and OpenSea mint calldata for
  hosted stages that require OpenSea eligibility/signatures;
- choose quantity and one or more configured wallets;
- check wallet balances, NFT counts, and mint transaction receipts;
- configure the mint-card accent color, brand text, and fallback background;
  cards show the real NFT artwork as the main image whenever OpenSea has it.

For compatible public SeaDrop stages, the bot uses a direct on-chain fast path:
it reads the public price/window from SeaDrop, prepares calldata, signs during
the warm-up window, and broadcasts the same signed transaction to optional RPC
endpoints at launch. Other hosted stages keep the OpenSea calldata route
required for allowlists/signatures.

The scanner walks every cursor in all three official Drops feeds once, merges
the results across every supported EVM network, and expands relevant drops via
OpenSea's per-drop endpoint. That merged calendar is cached briefly
(`DISCOVERY_CALENDAR_TTL_SECONDS`), so switching networks is local work rather
than a second full cursor walk, and the per-network counts on the picker cost
no extra API request. A collection that is merely indexed or traded on
OpenSea is not automatically a mint. A pasted asset link is first resolved
through OpenSea NFT metadata; the bot then prefers the hosted Drops route and
falls back only to the existing direct SeaDrop or verified simple-contract
resolver. Marketplace listings, custom proof flows, and guessed calldata are
still rejected.

## Requirements

- Python 3.11 or newer
- An EVM wallet created specifically for this bot
- Native gas on the network you will use
- An Alchemy API key for most RPC networks (the few unsupported networks use
  their official public RPC by default)
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

The direct SeaDrop path is enabled by default. You can add extra broadcast
endpoints per chain if you have them:

```text
DIRECT_PUBLIC_SEADROP=true
MINT_RPC_URLS_BASE=https://rpc-one.example,https://rpc-two.example
```

The configured chain endpoint remains primary for reads and preparation.
At launch, each extra endpoint receives the same signed transaction; this does
not create multiple mints because the raw transaction and hash are identical.

Set `DISCOVERY_UTC_OFFSET_HOURS` to the fixed offset you want for the
midnight-to-midnight scan. For example, West Africa Time is `1`; UTC is `0`.

## Telegram first use

1. Start the process with `python telegram_bot.py`.
2. Send `/start` to your bot.
3. Use **My wallet** to confirm the displayed public address and gas balance.
4. Use **Scan for mints**. The picker lists only networks that have drops,
   with a count on each. Pick one, then open a project. **All networks** gives
   a chain-grouped summary; **Other networks** reveals the quiet chains.
5. Use **Schedule from link** with any OpenSea collection, drop, item, or asset
   URL. Asset links are resolved back to their collection before mint routing.
6. Choose **Mint now** or **Schedule** for the mint window, then adjust
   quantity/wallets if needed and review the price cap.
7. Review the confirmation screen before enabling any live action.

The default scan covers what is live now plus what opens through the rest of
the day, and never looks less than `DISCOVERY_MIN_WINDOW_HOURS` ahead. That
floor matters: anchoring the horizon strictly to midnight made an evening scan
return almost nothing, which looks like a broken scan rather than a narrow
window. `/scan all` shows every usable stage OpenSea exposes in its hosted
Drops feeds, not collections that merely have secondary-market pages. Link scheduling is broader: it can
also resolve a live public SeaDrop or verified simple mint contract that is not
listed in the calendar. Paid and gated stages are included; the explicit price
and eligibility checks control whether execution can occur.

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

For a compatible public SeaDrop stage, the direct path signs before the
opening second so the launch path is only the raw-transaction broadcast. It is
used only when the on-chain price and opening time match the Telegram preview.
If the collection is not SeaDrop-compatible, the bot keeps the normal OpenSea
hosted-mint route. Allowlists and signature stages cannot use the
direct path because they require project-specific authorization.

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

## Direct execution guide

The project-owned `opensea_direct_executor.py` is the low-latency on-chain
executor for compatible public SeaDrop stages. It is integrated into the
Telegram scheduler and signs a checked transaction during warm-up, so no
browser, Chrome session, or wallet extension is required. It has its own
project-owned implementation and naming.

Read [DIRECT_EXECUTION.md](DIRECT_EXECUTION.md) for the exact setup, Telegram
flow, VPS service, timing model, supported routes, and the upgrades included
around the direct executor.

## Troubleshooting

### `Telegram polling error: Conflict`

Stop every other process using the same bot token, then start exactly one
controller.

### `OPENSEA_API_KEY is missing`

Confirm that the file is named `.env`, it is in the project root, and the key
is not still a placeholder. Restart the process after editing it.

### `/scan` came back empty

First check the network picker. It reports drop counts straight from OpenSea's
calendar, and on most days the large majority of supported networks genuinely
have nothing scheduled. If the picker says "OpenSea has no drops scheduled on
any network", that is OpenSea's own answer, not a failed scan. Tap **Refresh**
to bypass the cached calendar.

If the picker shows a count but the scan for that network is empty, the stages
are outside the scan window. Raise `DISCOVERY_WINDOW_HOURS` in `config.py`.

### A collection is visible on OpenSea but not in `/scan`

The collection may be secondary-market-only, externally hosted, sold out, or
not published in OpenSea's Drops feeds. `/scan` remains calendar-only, while
`/info` and `/schedule` accept any supported OpenSea collection, drop, item, or
asset URL. If OpenSea metadata exposes no hosted stage and the contract is not
verified/simulatable through a supported route, the bot refuses to invent a
mint transaction.

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
python -m pytest tests -q
```

Do not include `.env`, `state/`, logs, browser sessions, wallet files, or
generated deployment output in a pull request.

## License and contributions

This project is released under the MIT License. Contributions should preserve
the confirmation gates, price caps, secret handling, and refusal of unknown
mint routes.
