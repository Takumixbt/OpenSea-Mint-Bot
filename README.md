# OpenSea Mint Bot

A local Python bot for one configured OpenSea collection or drop.

It supports:

- Free mints
- Paid mints up to an explicit price cap
- Dry-run signing without broadcasting
- Read-only readiness and OpenSea drift checks
- An optional Tampermonkey page helper

Use a separate wallet. Never commit or share `.env`, `PRIVATE_KEY`, or
`session.json`.

## Quick start

From PowerShell:

```powershell
cd C:\Users\Takum\free-mint-bot
python -m pip install -r requirements.txt
```

If `.env` does not exist, create it from `.env.example` and fill in:

```text
ALCHEMY_API_KEY=...
PRIVATE_KEY=...
WALLET_ADDRESS=...
```

Open `config.py` and set the drop:

```python
TARGET_COLLECTION_URL = "https://opensea.io/collection/example"
TARGET_CHAIN_ID = 8453
TARGET_STAGE_INDEX = 0
MINT_QUANTITY = 1
MAX_MINT_PRICE_NATIVE = "0"
```

`MAX_MINT_PRICE_NATIVE = "0"` allows free mints only. Set a value such as
`"0.02"` to allow paid mints up to that amount. Gas is separate and is still
required for free mints.

Supported chain IDs are Ethereum `1`, Base `8453`, Polygon `137`, Optimism
`10`, Arbitrum `42161`, and Robinhood Chain `4663`.

## Check the setup

```powershell
python status.py
python recon_check.py
```

Fix any `BLOCKED`, `CHANGED`, or `FAIL` result before continuing.

## Run safely

Dry run; waits for the selected stage but never broadcasts:

```powershell
python main.py --dry-run
```

Live run:

```powershell
python main.py
```

For a one-run paid cap:

```powershell
python main.py --max-mint-price 0.02
```

The live bot needs the PC awake, internet-connected, and running the command.
Chrome, OpenSea, and MetaMask do not need to be open for the Python bot.

## Tampermonkey helper

The browser helper is separate from the Python bot. It watches one OpenSea page,
can click one visible Mint/Claim control when armed, and leaves wallet approval
manual. It does not sign transactions or use your private key.

See [TAMPERMONKEY.md](TAMPERMONKEY.md).

## Main files

- `config.py` — drop, chain, stage, quantity, price, and gas settings
- `main.py` — direct mint bot
- `status.py` — read-only readiness report
- `recon_check.py` — checks for OpenSea website changes
- `opensea_mint_assist.user.js` — optional browser helper
- `QUICKGUIDE.md` — beginner setup
- `RADAR.md` — optional discovery/radar component

OpenSea may change its website or restrict automation. This bot does not
guarantee a mint and should only be used where automation is permitted.
