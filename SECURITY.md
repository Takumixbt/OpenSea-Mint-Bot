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
and gas in the confirmation screen (Telegram tap or CLI `MINT`/`--yes`).

The direct SeaDrop path is limited to public stages whose on-chain price and
opening time match the saved preview. It does not bypass allowlists or create
calldata for arbitrary contracts. If you configure `MINT_RPC_URLS_*`, use only
RPC endpoints you trust: the same signed transaction is sent to each one at
launch.

The direct executor only prepares the narrow public SeaDrop transaction after
on-chain checks; it does not bypass allowlists, guess arbitrary contract calls,
or accept private keys through Telegram or the CLI. Keep the signing wallet and RPC
configuration on the machine running the bot.

## Reporting a vulnerability

Do not publish an exploitable detail with a private key or live target. Use
[GitHub private vulnerability reporting](https://github.com/Takumixbt/OpenSea-Mint-Bot/security/advisories/new)
so the maintainer can fix it before public disclosure.
