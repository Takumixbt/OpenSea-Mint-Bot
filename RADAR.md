# OpenSea Mint Bot Radar

The radar is an optional discovery component for Robinhood Chain (chain ID
`4663`). It finds candidate mints from public X activity, recent on-chain mints,
and OpenSea data, then writes them to either a local board or your own Notion
database.

`radar_scan.py` never mints. `radar_watch.py` only acts on rows that you
explicitly mark as **Armed**. The normal mint bot in `main.py` is separate.

## What it needs

- The Python dependencies in `requirements.txt`.
- The same `ALCHEMY_API_KEY` used by the main bot. It is needed for chain data.
- A separate wallet in `.env` if you will run `radar_watch.py` live.
- `twitter-cli`, installed and authenticated separately, for X-based signals.
  The radar still runs its on-chain and OpenSea checks if `twitter-cli` is not
  available.
- An optional Notion integration and database. Without Notion, the local board
  in `radar/state/board.json` is used.

## Setup

Run the main project setup first. From the repository folder:

```powershell
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Fill in `ALCHEMY_API_KEY`, `PRIVATE_KEY`, and `WALLET_ADDRESS` in `.env`. Use a
separate wallet and never commit `.env`.

For X signals, install and authenticate `twitter-cli` according to its own
instructions, then verify that a read-only command works:

```powershell
twitter user <public-handle>
```

Review `radar/smart_accounts.txt`. It is a starter list of public accounts;
replace it with accounts whose signals you trust. Use one X handle per line.

## Run a sweep

Run the first sweep from the repository folder:

```powershell
python radar_scan.py
```

The first run creates the follow-graph baseline. It can still find candidates
from the other feeds. Later runs compare the baseline and look for new signals.

To print results without writing to Notion or the local board:

```powershell
python radar_scan.py --no-notion
```

Without Notion, normal sweeps write candidates to
`radar/state/board.json`. The board is ignored by Git and stays on your PC.

## Optional Notion watchlist

Create your own Notion database, create an internal integration, and share that
database with the integration. Put the following in `.env`:

```text
NOTION_TOKEN=your_integration_token
NOTION_DATABASE_ID=your_database_id
NOTION_DATABASE_URL=https://www.notion.so/your-database
```

The URL is optional and is only printed as a convenient link. The token and
database ID must both be present. The database must contain these properties
with these names:

| Property | Notion type |
|---|---|
| Project | Title |
| X Handle | Rich text |
| Project URL | URL |
| Chain | Select |
| Status | Select |
| Score | Number |
| Smart Follows | Number |
| Smart Followers | Rich text |
| Mint Type | Select |
| Mint Contract | Rich text |
| Mint Open | Date |
| Deployer | Rich text |
| Risk Flags | Multi-select |
| OSINT Notes | Rich text |
| Source | Select |
| Armed | Checkbox |
| Result | Select |
| Tx Hash | Rich text |

The radar proposes rows. You authorize a row by checking **Armed** in Notion.
It never arms a row automatically.

## Rehearse and run the watcher

First run a one-time dry run:

```powershell
python radar_watch.py --dry-run --once
```

If you use the local board, open `radar/state/board.json` and change the
intended row's `"armed"` value to `true`. If you use Notion, check **Armed**
there instead.

Then start the watcher:

```powershell
python radar_watch.py
```

The watcher needs the PC awake, online, and running this command. It reads the
published schedule when it can, simulates the mint call, checks the configured
price cap, and sends only after a call succeeds and the row is armed.

The default `MAX_MINT_PRICE_NATIVE = "0"` is free-mint only. Gas is separate
and is still required. Raise the cap deliberately in `config.py` before using a
paid mint.

## Windows scheduling

To run a sweep every three hours, replace the example folder with the folder
where you saved this repository:

```powershell
schtasks /create /tn "OpenSeaMintRadar" /sc hourly /mo 3 /tr "cmd /c cd /d C:\path\to\OpenSea-Mint-Bot && python radar_scan.py >> radar\state\sweep.log 2>&1"
```

The scheduled account must have access to the repository, `.env`, and any
`twitter-cli` login it uses. Test the command manually before scheduling it.

## Safety and limits

- Run `python radar_watch.py --dry-run --once` before any live run.
- Use a wallet funded only with the amount you are willing to lose.
- A score is a ranking signal, not permission to spend.
- Paid mints above the configured cap are skipped.
- Server signatures, allowlists, and Merkle proofs may prevent replay; those
  drops are skipped rather than guessed at.
- X, OpenSea, and chain APIs can change or rate-limit requests. A quiet sweep
  does not prove that no mint exists.
- This radar is currently specialized for Robinhood Chain. The main bot and the
  radar do not have identical chain support.
