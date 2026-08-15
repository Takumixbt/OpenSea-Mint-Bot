# Security policy

## Keep secrets out of Git

Never commit or paste any of the following into an issue, pull request,
Telegram message, screenshot, log, or code file:

- wallet private keys or seed phrases;
- `TELEGRAM_BOT_TOKEN` or `TELEGRAM_ALLOWED_CHAT_ID`;
- Alchemy, OpenSea, or other API keys;
- VPS credentials or `.env` files.

The bot loads these values from a local `.env`, and `.env` is ignored by Git.

## Operating the bot safely

Use a separate low-value wallet. Start with `ENABLE_LIVE_MINTS=false`,
`MAX_MINT_PRICE_NATIVE=0`, and `MAX_BUY_PRICE_NATIVE=0`. Enable a paid route
only after checking the exact collection, chain, quantity, recipient, value,
and gas in the confirmation screen.

The Tampermonkey helper never handles keys or confirms wallet popups. Treat any
browser script that asks for a seed phrase or private key as malicious.

## Reporting a vulnerability

Do not publish an exploitable detail with a private key or live target. Open a
private security report through the repository's GitHub security contact, or
contact the repository maintainer privately before disclosure.
