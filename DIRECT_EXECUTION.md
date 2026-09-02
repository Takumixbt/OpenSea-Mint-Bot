# Direct execution guide

`opensea_direct_executor.py` is the project-owned low-latency executor for
compatible public SeaDrop stages. The Telegram bot and the terminal CLI call it
automatically; it is not a browser extension and it does not need an OpenSea
tab, Chrome, or a wallet extension.

## What it supports

The direct path is used only when all of these are true:

- the selected OpenSea stage is public;
- OpenSea supplied a valid NFT contract address;
- the contract uses the public SeaDrop route;
- the on-chain price and opening time match the saved preview;
- the requested quantity is within the on-chain wallet limit.

The scheduler accepts an OpenSea collection, drop, item, or NFT asset URL. An
asset URL is resolved through OpenSea metadata first. If it is not a hosted
calendar drop, the bot may use this direct route only when the NFT's collection
contract exposes a live public SeaDrop stage and the on-chain checks pass.
Marketplace listings and unsupported/custom contract routes are still rejected.

Allowlist/signature stages and stages whose data changed remain on their
required OpenSea calldata route. The bot does not bypass an allowlist or guess
arbitrary calldata.

## Install on a personal computer

Run these commands from the repository folder. Replace the path with the folder
where you cloned the project:

```powershell
cd path\to\OpenSea-Mint-Bot
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
notepad .env
```

Fill in `.env` locally with your own values. At minimum, the bot needs an
Alchemy key, OpenSea API key, wallet private key, and matching wallet address.
Add Telegram settings only if you want Telegram control. Never paste those
values into Telegram, a terminal chat, GitHub, or an issue.

Keep the safe defaults while testing:

```dotenv
DIRECT_PUBLIC_SEADROP=true
ENABLE_LIVE_MINTS=false
MAX_MINT_PRICE_NATIVE=0
MAX_BUY_PRICE_NATIVE=0
MAX_DAILY_MINTS=5
MAX_DAILY_GAS_NATIVE=0.05
```

Then verify the installation:

```powershell
python -m pytest tests -q
python status.py
```

`status.py` reports missing configuration without sending a transaction.

## Start and use it

Start the terminal controller:

```powershell
python cli.py
```

Or start exactly one Telegram controller:

```powershell
python telegram_bot.py
```

Do not run two Telegram processes with the same token. The CLI and Telegram
share the same wallet if both are live at once, so use one controller for live
sends.

In the CLI, `scan`, `info <OpenSea URL>`, `mint`, and `schedule` cover the same
flow. In Telegram:

1. Send `/start`.
2. Use `/scan` to choose a network, `/scan all` to scan every supported network
   with a chain-grouped summary, or `/schedule` and paste any OpenSea
   collection, drop, item, or asset URL.
3. Open a project, choose **Mint now** or **Schedule**, then review the
   price, gas estimate, eligibility, links, and route.
4. Enable live mode in `.env` only after the review is correct. Set a deliberate
   price cap, restart the bot, and use the separate live confirmation control.

The bot will show the transaction hash and explorer link after a broadcast. A
hash means the network received the transaction; it does not guarantee that the
transaction will be included or that the mint will succeed.

## How the fast path works

For a scheduled public stage, the service warms the RPC connection about ten
seconds before launch. During warm-up it reads the SeaDrop price, start/end
window, wallet quantity limit, and fee recipient, then builds and signs the
transaction only if those values match the Telegram preview. At the opening
time, the already-signed raw transaction is broadcast to the configured RPC
endpoints. The same signed transaction is sent to each endpoint, so this is
endpoint fan-out—not multiple mints.

Optional endpoints can be configured per chain:

```dotenv
MINT_RPC_URLS_BASE=https://your-primary-rpc.example,https://your-backup-rpc.example
```

Use only endpoints you trust. An endpoint does not make blockchain inclusion
instant or guaranteed. The process must remain online until the transaction is
sent; a VPS is useful for unattended schedules.

## VPS operation

Use the included service after copying the repository to a non-root service
directory. The VPS does not need Chrome or MetaMask:

```bash
sudo cp deploy/opensea-mint-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now opensea-mint-bot.service
sudo systemctl status opensea-mint-bot.service --no-pager
```

Run only one Telegram polling process for the bot token. If another instance is
already running, Telegram returns a `Conflict: terminated by other getUpdates
request` error.

## Upgrades included around the direct executor

Compared with a minimal page-clicking mint script, this project adds:

- Telegram-first all-network discovery and hosted-drop scheduling;
- free, paid, public, and restricted OpenSea stage handling;
- quantity selection, multiple configured wallets, and per-wallet checks;
- hard price, daily mint, daily gas, balance, nonce, and simulation guards;
- on-chain price/window/quantity validation immediately before signing;
- optional verified RPC fan-out using one identical signed transaction;
- receipt links, wallet snapshots, mint history, and configurable receipt cards;
- persisted schedules and duplicate-attempt protection;
- tests and documentation that keep private configuration out of the repository.

These features improve control and observability; they cannot override a
project's allowlist, CAPTCHA, signature requirement, sold-out state, or chain
rules.

## Troubleshooting

- **No direct execution shown:** the stage is probably restricted, custom, not
  SeaDrop-compatible, or its on-chain values no longer match OpenSea. The bot
  will use OpenSea's signed calldata route when available or stop safely.
- **The bot does not start:** run `python status.py`, fix the first blocked item,
  and restart the process.
- **The schedule did not fire:** confirm the PC/VPS stayed online, the process
  was running, the wallet had native gas, and the price/gas caps were high
  enough for the chosen stage.
- **A transaction is pending:** do not click mint again. Open the explorer link
  and wait for a final state before deciding what to do.
