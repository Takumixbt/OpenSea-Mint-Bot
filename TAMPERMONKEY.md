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

Auto-click can be enabled only when OpenSea has authorized the automation. When
enabled, the helper clicks at most one visible page button and immediately
disarms. It never presses Confirm inside MetaMask or Rabby.

## How to verify it is working

The panel should appear on `https://opensea.io/*` pages and initially say
`Disarmed — watching only`. On a live page it will report when it sees a visible
Mint/Claim control. If the panel appears but cannot find the control, OpenSea's
current UI text or button structure may differ; use the page manually or use the
Python bot instead.

The browser must remain open for this route. The helper does not read your
private key, API key, wallet address, or local Python session.
