# OpenSea Mint Bot

A Telegram-controlled mint bot for OpenSea EVM drops. It can scan one network,
research a collection, select quantity, mint immediately, or arm a one-time
schedule. It supports free, paid, public, and restricted stages; OpenSea makes
the final wallet-eligibility decision.

This is a mint bot, not a secondary-market buying bot. Use a separate wallet
and never commit `.env`.

## Install

Install Python 3.11 or newer, then:

```powershell
git clone https://github.com/Takumixbt/OpenSea-Mint-Bot.git
cd OpenSea-Mint-Bot
python -m pip install -r requirements.txt
Copy-Item .env.example .env
notepad .env
```

Add your Alchemy key, OpenSea key, wallet private key/address, Telegram token,
and allowed chat ID to `.env`. Keep `MAX_MINT_PRICE_NATIVE=0` for free-only, or
set your maximum transaction mint value in Telegram under **Settings**.

Run the checks and start the bot:

```powershell
python -m unittest discover -s tests -v
python status.py
python recon_check.py
python telegram_bot.py
```

Only one PC or VPS may run `telegram_bot.py` for the same Telegram token.

## Telegram

Send `/start` for the button dashboard. Main commands:

```text
/scan                 choose one network
/scan base            scan one network directly
/info                 research an OpenSea URL
/schedule             paste a URL and arm one stage
/schedules            inspect or cancel schedules
/mint 1               review candidate 1 and confirm it
/settings             price cap and mint-card background
/status               runtime status
```

Every mint requires a confirmation screen. Before signing, the bot checks the
exact selected price × quantity, configured price cap, gas cap, wallet balance,
chain ID, daily attempt limit, and OpenSea eligibility. If OpenSea returns a
different transaction value from the Telegram preview, it refuses to sign.

One-time schedules warm the RPC, nonce, fee data, balance, and OpenSea HTTPS
connection 10 seconds before launch. The first mint-data request starts at the
scheduled second, followed by short bounded retries if OpenSea activates late.
Completed schedule notifications include the measured broadcast delay.

The scanner is chain-by-chain in Telegram and covers midnight to midnight at
`DISCOVERY_UTC_OFFSET_HOURS`. OpenSea's public feeds are incomplete, so a scan
cannot guarantee it finds every mint. Paste a known collection URL into
`/schedule` when timing matters.

### Mint-card background

Open **Settings → Set card background**, then send a JPG/PNG/WEBP or a direct
image URL. The bot crops it to 1200×675 and stores it locally. **Reset card
background** restores the built-in design.

## VPS

The VPS needs Ubuntu, Python, internet access, and the bot running as one
service. Chrome, OpenSea, and MetaMask are not required. A ready systemd unit
is included in `deploy/opensea-mint-bot.service`; see [QUICKGUIDE.md](QUICKGUIDE.md).

Schedules and scans are local to the machine in `state/`. Copy that folder when
moving an armed bot, and stop the old instance before starting the VPS.

## Optional one-drop CLI

Set the target URL, chain, stage, quantity, and cap in `config.py`. Then run:

```powershell
python status.py
python recon_check.py
python main.py --confirm-live
```

The CLI is live-only and also requires `ENABLE_LIVE_MINTS=true` in `.env`.

## Browser helper

`opensea_mint_assist.user.js` is an optional Tampermonkey page helper. It can
click one visible Mint/Claim button, but wallet approval remains manual. See
[TAMPERMONKEY.md](TAMPERMONKEY.md).

No bot can guarantee a mint: supply, allowlists, wallet limits, API uptime,
network gas, and OpenSea timing can change.
