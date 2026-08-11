# OpenSea Mint Bot — Tampermonkey helper

This is an optional browser helper. It is not the Python mint bot.

## Install

1. Install Tampermonkey in Chrome or another supported browser.
2. Open the Tampermonkey dashboard.
3. Choose **Create a new script**.
4. Delete the starter code.
5. Copy all of `opensea_mint_assist.user.js` into the editor.
6. Save with `Ctrl+S`.
7. Make sure the script is enabled.

## Use

1. Open one OpenSea collection/drop page.
2. Refresh the page.
3. Look for the **OpenSea Mint Assist** panel in the bottom-right corner.
4. Leave **Require visible free/0-value evidence** enabled unless you fully
   understand the page price.
5. Leave **Auto-click one page button** disabled for the safest route.
6. Click **Arm once** when you are ready.
7. If a visible Mint/Claim button appears, click it yourself.
8. Review the wallet popup carefully and confirm it manually.

## Fast mode

For an opt-in fast route:

1. Enable **Auto-arm on page load**.
2. Enable **Auto-click one page button**.
3. Keep **Require visible free/0-value evidence** enabled for free mints.
4. Reload the OpenSea page before the drop.

The helper watches the page continuously and clicks at most one matching button
as soon as it becomes visible. It immediately disarms after the click. For a
paid mint, disable the free-only checkbox only after verifying the price yourself.

The wallet popup is still manual. Tampermonkey cannot safely sign or press
Confirm inside MetaMask or Rabby, so the final approval remains yours.

## How to verify it is working

The panel should appear on `https://opensea.io/*` pages and initially say
`Disarmed — watching only`. On a live page it will report when it sees a visible
Mint/Claim control. If the panel appears but cannot find the control, OpenSea's
current UI text or button structure may differ; use the page manually or use the
Python bot instead.

The browser must remain open for this route. The helper does not read your
private key, API key, wallet address, or local Python session.
