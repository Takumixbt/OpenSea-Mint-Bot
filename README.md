# Free-Mint Bot (OpenSea, personal use)

This is a small program that tries to win a free ("gas-only") OpenSea NFT mint
for you by doing, automatically and fast, the same steps a person would do by
hand: log in, watch the clock, and send the mint transaction the instant it
opens. It uses one dedicated wallet that you set up just for this.

You do not need to know how to code to use it. Follow the numbered steps below
exactly, copying and pasting the commands. Every command goes into a program
called PowerShell (it comes with Windows).

---

## Important things to understand first

- **This does not hack or bypass anything.** It just does the normal mint steps
  faster than a human can. But automating OpenSea like this very likely breaks
  OpenSea's Terms of Service around automated access. Keep it to personal, single
  wallet use. Do not scale it, sell access, or run many wallets.
- **Use a fresh wallet with only a little money in it.** Your secret key sits in
  a file on your computer. If your computer is ever compromised, that wallet
  could be drained. Never put your main wallet's key here, and never fund the
  bot wallet with more than you would be okay losing.
- **Winning is never guaranteed.** Even perfect tooling can lose a close race to
  someone bidding higher gas or sitting closer to OpenSea's servers.
- **OpenSea can change their website at any time.** When they do, the bot can
  break. That is why there is a `recon_check.py` you run before each new drop.
  This is not "set up once and forget."
- **The bot refuses to overpay by design.** `MAX_MINT_PRICE_NATIVE` in
  `config.py` (default `"0"`) is a hard cap on the mint price. Free mints still
  work, and a paid mint is accepted only when its price is at or below the cap.
  For example, setting it to `"0.02"` permits a mint price up to 0.02 of the
  chain's native coin. `MAX_FEE_CAP_GWEI` and `GAS_LIMIT_MAX` separately cap
  the network fee. You only raise these on purpose.
- **A dry run costs nothing and needs no funds in the wallet.** It logs in,
  reads the schedule, and even builds + signs a transaction - signing is
  free, it never touches the network fee - but stops before broadcasting. You
  can fully rehearse Steps 6-7 below with an empty wallet.

---

## Step 1 - Install Python

1. Go to https://www.python.org/downloads/ and download Python (version 3.11 or
   newer; 3.12 is a safe choice).
2. Run the installer. **On the first screen, tick the box that says "Add
   python.exe to PATH"** before clicking Install. This matters.
3. Open PowerShell: press the Windows key, type `PowerShell`, press Enter.
4. Check it worked by typing this and pressing Enter:

   ```
   python --version
   ```

   You should see something like `Python 3.12.x`. If you instead see an error,
   restart your computer and try again (the PATH change needs a restart).

---

## Step 2 - Install the bot's helper packages

1. In PowerShell, move into the bot's folder by pasting this and pressing Enter:

   ```
   cd C:\Users\Takum\free-mint-bot
   ```

2. Install the four small packages the bot needs:

   ```
   pip install -r requirements.txt
   ```

   Wait for it to finish (it prints a lot; that is normal). When you get your
   prompt back with no red "ERROR" at the very end, it worked.

---

## Step 3 - Make a fresh wallet just for the bot

Your bot needs its own wallet, separate from your main one, because its secret
key will live in a file. The easiest way:

**Option A - MetaMask (recommended if you already use it):**
1. Open MetaMask, click your account icon at the top, choose "Add account or
   hardware wallet" then "Add a new account". Name it something like "bot".
2. Copy that new account's **public address** (starts with `0x`). You will paste
   it as `WALLET_ADDRESS` later.
3. Reveal that account's **private key**: click the three dots next to the
   account, "Account details", "Show private key", enter your MetaMask password.
   Copy it (starts with `0x`). You will paste it as `PRIVATE_KEY` later. Never
   share this with anyone, ever.

**Option B - Generate one with a script the bot includes:**
1. Paste this into PowerShell and press Enter:

   ```
   python -c "from eth_account import Account; a=Account.create(); print('ADDRESS:', a.address); print('PRIVATE KEY:', a.key.hex())"
   ```

2. It prints an `ADDRESS:` and a `PRIVATE KEY:`. Save both somewhere safe and
   private. The address is `WALLET_ADDRESS`, the private key is `PRIVATE_KEY`.

Then **fund it** with a small amount of the chain's coin (e.g. a little ETH on
Ethereum or Base) so it can pay the small network fee for the mint. Only send
what you are okay losing.

---

## Step 4 - Get one free API key from Alchemy (works for every chain)

An "RPC url" is just the bot's internet connection to the blockchain. Alchemy
gives one out free, and one key covers ALL chains it supports - you set up
Alchemy once, ever, no matter how many different chains your drops end up on.

1. Go to https://www.alchemy.com/ and sign up (free).
2. Click **"Create new app"** (or "Apps" then "Create app"). Give it any name;
   the chain you pick here doesn't limit it - the key works on every chain.
3. Once created, open the app and find its **API key** - it's the part of any
   of its URLs after `/v2/`, e.g. in `https://eth-mainnet.g.alchemy.com/v2/xxxxxxxxxxxx`
   the key is `xxxxxxxxxxxx`. Copy just that part.

That's it - this key never changes even if next month's drop is on a chain you
haven't used yet.

---

## Step 5 - Fill in your secrets file (.env)

1. If `.env` does not already exist, make a copy of `.env.example` named
   exactly `.env` by pasting this into PowerShell. Do not overwrite an existing
   `.env` because it may contain your real secrets:

   ```
   Copy-Item C:\Users\Takum\free-mint-bot\.env.example C:\Users\Takum\free-mint-bot\.env
   ```

2. Open the new `.env` file in Notepad:

   ```
   notepad C:\Users\Takum\free-mint-bot\.env
   ```

3. Fill in the three values, then save and close:
   - `ALCHEMY_API_KEY` = the key from step 4
   - `PRIVATE_KEY` = your wallet's private key
   - `WALLET_ADDRESS` = your wallet's public address

This `.env` file is automatically kept private (it is never uploaded anywhere).

4. Tell the bot which drop to target AND which chain it's on. Open the
   settings file:

   ```
   notepad C:\Users\Takum\free-mint-bot\config.py
   ```

   Paste the complete OpenSea collection/drop URL into `TARGET_COLLECTION_URL`,
   for example `https://opensea.io/collection/cool-cats`. The bot extracts
   `cool-cats` automatically. Advanced users can leave the URL blank and set
   `TARGET_COLLECTION_SLUG` directly.

   Then find `TARGET_CHAIN_ID` just below it and set it to match the chain
   shown on that same OpenSea page (a list of common ones is right there in
   the comment - Ethereum `1`, Base `8453`, Polygon `137`, Optimism `10`,
   Arbitrum `42161`, Robinhood Chain `4663`). This is the only thing you
   change when your next drop is on a different chain - the Alchemy key from
   step 4 stays the same either way. Save and close.

   If a future drop is on a chain not in that list, `main.py` will tell you
   exactly what to do: look up "Alchemy `<chain name>` RPC", and add one line
   to the `CHAIN_RPC_SUBDOMAINS` dictionary in `config.py`.

---

## Step 6 - Check OpenSea hasn't changed anything

Before trusting the bot, run the drift checker:

```
cd C:\Users\Takum\free-mint-bot
python recon_check.py
```

It downloads OpenSea's current site and checks each thing the bot depends on,
with an honest confidence level per item:

- `PASS` - confirmed in the right context. Strong signal.
- `PASS-WEAK` - the text exists somewhere in the site's code, just not in a
  spot this script can be certain matters. Probably fine.
- `CHANGED` / `FAIL` - not found; something likely moved.
- `UNKNOWN` / `INCONCLUSIVE` - can't be checked from outside a real browser,
  or the download itself was too small to judge fairly.

If everything is PASS or PASS-WEAK, continue. If anything says CHANGED or
FAIL, see "If something in the drift check fails" near the bottom.

---

## Step 7 - Test safely with a dry run

A **dry run** does the whole thing - logs in, waits, asks OpenSea for the real
mint instructions, builds and signs the transaction - but stops right before
actually sending it, and prints exactly what it would have sent. It risks
nothing.

Pick a real, currently-live free drop, set its slug in `config.py` (step 5.4),
then run:

```
python main.py --dry-run
```

Watch the messages. You want to see it log in, find the drop's schedule, warm
up, get valid mint instructions, and print a "Transaction ready" summary ending
with "DRY RUN: stopping here." If it gets that far, the pipeline works.

If it fails at "asking OpenSea for the mint instructions", that is the one part
that most often needs a small manual update - see "Capturing the real OpenSea
queries" below. It is a two-minute, one-time fix.

---

## Step 8 - Run it for real

Only once a dry run looks correct:

```
python main.py
```

It will actually send the transaction when the mint opens. **Rehearse on a low
stakes drop you do not care about first**, so the first real send is never on
the drop you actually want.

For a paid drop, set `MAX_MINT_PRICE_NATIVE` in `config.py`, or override it for
one run without editing the file:

```
python main.py --max-mint-price 0.02
```

The value is in the chain's native coin: ETH on Ethereum/Base/Optimism/Arbitrum,
MATIC on Polygon, and the native gas coin on Robinhood Chain. A free mint has a
mint price of zero but still requires enough native coin to pay gas.

---

## Capturing the real OpenSea queries (do this once if the dry run fails at the mint step)

OpenSea only loads its exact mint instructions code when you are on a live drop
page, so the bot ships with a best-guess version of that one query. If a dry run
fails specifically at "asking OpenSea for the mint instructions", you can copy
the real one out of your browser in about two minutes:

1. In Chrome, open a **live, currently-minting** OpenSea drop page while logged
   in with any wallet.
2. Press `F12` to open Developer Tools, click the **Network** tab, and in its
   filter box type `graphql`.
3. Click the drop's **Mint** button (you do not have to complete it). Watch the
   Network list fill with `graphql` rows.
4. Click the row that appears right when you press Mint. In the panel that opens,
   look at **Payload** (or "Request"). You will see an `operationName` (a word
   like `MintActions...`) and a `query` (a block of text), and `variables`.
5. Open `opensea_client.py` in Notepad and replace the value of
   `MINT_CALLDATA_QUERY_NAME` with that `operationName`, and the text between the
   triple quotes of `MINT_CALLDATA_QUERY` with that `query`. If the `variables`
   names differ from the ones in `get_mint_calldata`, match them up. Save.

Then re-run the dry run. This same trick works for the schedule query
(`dropBySlug`) if that ever fails too.

---

## If login stops working

If the bot fails at the login step, OpenSea likely changed its login web
addresses or message wording. To read the real ones:

1. Open OpenSea in Chrome, press `F12`, go to the **Network** tab, filter for
   `auth` or `siwe`.
2. Disconnect and reconnect your wallet / sign in. Watch for two requests: one
   fetching a **nonce**, and one **verify** (or "login") request.
3. Their web addresses go into `config.py` as `AUTH_NONCE_URL` and
   `AUTH_VERIFY_URL`. Click the verify request's **Payload** to see the exact
   field names and the `statement` text; update `SIWE_STATEMENT` in
   `opensea_auth.py` to match if it differs.

---

## If something in the drift check fails

`recon_check.py` tells you which item changed and which file/line backs it:

- **Endpoint FAIL** - OpenSea's internal address moved. Find the new one via the
  Network tab (see "If login stops working") and update `GQL_ENDPOINT` in
  `config.py`.
- **Header CHANGED** - update `APP_ID_HEADER` in `config.py` to the new value you
  see on `graphql` requests in the Network tab (header named `x-app-id`).
- **A field CHANGED** (dropBySlug / transactionSubmissionData / chainArch /
  connectorId / login wording) - OpenSea renamed something. Capture the current
  query/payload from the Network tab and update the matching file it points to.

When in doubt, the safe move is to not run the bot for real until the drift
check is all PASS again.

## Current status and browser helper

The quickest read-only readiness report is:

```
python status.py
```

For the slower website-bundle check as well:

```
python status.py --full-recon
```

The report never signs or broadcasts a transaction and never prints secrets.
It checks the target slug, required `.env` values, wallet/key matching, Python
dependencies, the configured chain/RPC, the OpenSea endpoint, the saved session,
and the local radar board.

There is also a conservative browser companion in
`opensea_mint_assist.user.js`. The beginner installation and usage steps are in
`TAMPERMONKEY.md`. It only observes one page and can optionally click one
visible Mint/Claim button; it never handles a private key, calls an API, or
confirms MetaMask/Rabby. The default protections require visible free/0-value
evidence and leave wallet confirmation to you. OpenSea's Terms restrict
automated tools unless authorized, so only enable auto-click if you have
permission to automate that page. For unattended minting, use the Python bot
only after a dry run and the drift check.
