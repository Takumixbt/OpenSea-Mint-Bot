# Tampermonkey browser companion

`opensea_mint_assist.user.js` is an optional browser helper for one OpenSea
collection/drop page. It is not a second wallet and it is not a replacement
for the Python Telegram bot.

## What it can do

- show a small status panel on OpenSea pages;
- watch for a visible Mint, Claim, or Collect button;
- check nearby page text for free/zero-value evidence;
- optionally allow a paid click only when you explicitly enable it and set a
  visible price cap;
- wait until a browser-local date and time;
- optionally set a visible numeric quantity input;
- click at most one page control, then disarm immediately.

## What it cannot do

- scan every OpenSea chain or collection;
- call the Python bot or Telegram;
- read a private key, seed phrase, API key, or wallet balance;
- sign or confirm a MetaMask/Rabby transaction;
- guarantee that a button is the project's real mint control;
- bypass allowlists, signatures, puzzles, CAPTCHAs, or project limits.

The wallet popup and final **Confirm** action are always yours. The browser
must remain open, logged in, connected to the intended wallet, and on the
correct collection page.

## Install Tampermonkey

1. Go to the official site: <https://www.tampermonkey.net/>.
2. Choose your browser and install the extension from its official store.
3. Open the Tampermonkey extension menu and choose **Dashboard**.
4. Select **Create a new script**.
5. Delete the starter template.
6. Copy the complete contents of `opensea_mint_assist.user.js` into the editor.
7. Save with `Ctrl+S`.
8. Confirm the script is enabled and its match rule is `https://opensea.io/*`.

Review the script before installing it. Do not install a modified copy from an
unknown source.

## Safe first use

1. Open the intended OpenSea collection or drop page.
2. Connect the intended wallet manually.
3. Check the chain, collection, quantity, and price in OpenSea itself.
4. Leave **Free/zero-value only** enabled.
5. Leave **Auto-click** disabled.
6. Click **Arm once**.
7. When the panel reports a matching control, click the page button yourself.
8. Read the wallet popup and reject it if the recipient, value, gas, or chain
   is not exactly what you expect.

## Faster opt-in use

Only after the safe flow works:

1. Set the browser-local launch time, or leave it blank for immediate watching.
2. Enable **Auto-arm on page load** if you understand that refreshing the page
   arms the helper again.
3. Enable **Auto-click one page button**.
4. Keep **Free/zero-value only** enabled for free drops.
5. Reload the page before the target time.

The helper clicks once at most and then disarms. It does not press the wallet
confirmation button.

## Paid page clicks

Paid browser clicks are deliberately opt-in. Disable **Free/zero-value only**,
enable **Allow paid click**, and enter a maximum native-coin amount. The helper
only proceeds when it can find a nearby visible price and the amount is at or
below your cap. Page text can be ambiguous, so the Python bot is the safer
route for paid execution. Always verify the wallet popup manually.

## Troubleshooting

- If the panel does not appear, make sure the URL starts with
  `https://opensea.io/`, the script is enabled, and the page is refreshed.
- If it says no button was found, OpenSea may have changed its UI or the drop
  may not be live. Use the page manually.
- If the browser page changes collection through client-side navigation,
  refresh before arming again.
- If the wallet popup shows a different chain or value, reject it.
