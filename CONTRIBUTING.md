# Contributing

This is a local minting tool. Keys never leave the machine that runs it.
Do not paste private keys, seed phrases, Alchemy keys, OpenSea keys, or
Telegram tokens into issues, PRs, logs, or chat.

## Layout

| Path | What it is |
| --- | --- |
| `cli.py` | Terminal UI. This is the program. |
| `telegram_bot.py` | Optional Telegram control. Same mint engine. |
| `daily_runner.py` | Scan, research, schedule, mint, buy. |
| `discovery.py` | OpenSea drop calendar. |
| `config.py` | Non-secret settings and chain registry. |
| `assets/chains/` | Network logos. Regenerate with `python scripts/fetch_chain_logos.py`. |
| `main.py` | Shim that calls `cli.py`. `--confirm-live` is the old one-drop path. |

## Dev loop

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m pytest tests -q
python cli.py help
```

Keep `ENABLE_LIVE_MINTS=false` while you work. Tests must not send a
transaction.

## Rules

- Live sends need the env switch **and** an explicit confirm.
- Discovery uses OpenSea drop feeds only. Do not mix ranked/trending
  marketplace collections back into `/scan`.
- Telegram buttons: one verb, no synonyms. Numbers on the chain picker
  image must match the tap buttons 1:1.
- Chain logos must be the real mark with alpha. Do not flatten to RGB.
