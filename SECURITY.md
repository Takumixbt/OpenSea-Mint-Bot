# Security

This software can spend gas and mint NFTs from a wallet you control.

## If you run it

- Use a throwaway wallet. Never import a seed phrase.
- Keep `ENABLE_LIVE_MINTS=false` until a dry run looks right.
- Never paste a private key into Telegram, GitHub, Discord, or an issue.
- Do not run `python cli.py watch` and `python telegram_bot.py` against the
  same wallet at the same time.

## Reporting a vulnerability

Open a **private** GitHub security advisory on
[Takumixbt/OpenSea-Mint-Bot](https://github.com/Takumixbt/OpenSea-Mint-Bot),
or email the maintainer through GitHub. Do not file a public issue that
includes a working exploit against live wallets.

Please include:

- what the bot does that it should not (sign, broadcast, leak a key, mint
  the wrong stage)
- the file and a minimal reproduction
- whether a key or funds were at risk

We will not ask you to send a private key.
