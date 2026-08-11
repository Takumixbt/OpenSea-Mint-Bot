# OpenSea Mint Bot — beginner quick guide

This project has two different routes. Pick one:

| Route | Needs OpenSea browser tab? | Signs the blockchain transaction? | Best for |
|---|---:|---:|---|
| Python bot | No | Yes, in live mode | Scheduled or unattended minting |
| Tampermonkey helper | Yes | No | Watching the page and clicking once |

The Python bot is the real minting route. The browser helper is only a page
assistant and does not replace it.

## First-time setup

Open PowerShell and run:

```powershell
cd C:\Users\Takum\free-mint-bot
python --version
python -m pip install -r requirements.txt
```

If `.env` does not exist, create it once:

```powershell
Copy-Item .env.example .env
notepad .env
```

Fill in `ALCHEMY_API_KEY`, `PRIVATE_KEY`, and `WALLET_ADDRESS`. Never paste the
private key into GitHub, Discord, or a chat. Use a separate wallet containing
only the amount you are willing to risk.

## Configure an upcoming drop

Open the settings file:

```powershell
notepad config.py
```

Paste the full OpenSea URL into:

```python
TARGET_COLLECTION_URL = "https://opensea.io/collection/example"
```

Then set the chain shown on the drop page:

```python
TARGET_CHAIN_ID = 8453  # Base example
```

Use the correct stage index. Stage `0` is only a default; an allowlist and
public mint may have different stage indexes. Keep `MINT_QUANTITY = 1` until
you have verified the wallet limit.

Paid-mint safety is explicit:

```python
MAX_MINT_PRICE_NATIVE = "0"     # free-only
MAX_MINT_PRICE_NATIVE = "0.02"  # free plus paid up to 0.02 native coin
```

The bot still needs native coin for gas even when the mint price is zero.

## Check before running

```powershell
python status.py
python recon_check.py
```

`status.py` is read-only. It never signs or broadcasts. Do not continue until
the target collection, wallet identity, RPC, and dependencies are healthy.

## Safe rehearsal

```powershell
python main.py --dry-run
```

For an upcoming drop, this process waits until the selected stage opens. A
successful rehearsal ends with `Transaction ready` and `DRY RUN: stopping here`.
Nothing is broadcast.

## Live run

```powershell
python main.py
```

Keep the PowerShell window and PC awake, connected to the internet, and running
until the drop is finished. The main Python bot does not need Chrome, OpenSea,
or MetaMask to be open.

For a one-run paid cap without editing `config.py`:

```powershell
python main.py --max-mint-price 0.02
```

## If something fails

- `TARGET_COLLECTION_URL` error: use a URL shaped like
  `https://opensea.io/collection/<slug>` or `https://opensea.io/drops/<slug>`.
- No stage time: OpenSea has not exposed a usable schedule; check the page and
  stage index.
- No mint instructions: the drop may not be open, may be sold out, may require
  eligibility, or OpenSea may have changed its internal query.
- Transaction refused for value: the mint price is above your cap.
- Transaction reverted: somebody else may have taken the final supply; gas can
  still be charged.

The bot does not guarantee a win. It only makes the configured wallet's normal
mint process faster.
