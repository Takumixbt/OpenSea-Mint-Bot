"""
The executor. Watches your Notion watchlist and mints the rows you armed.

    python radar_watch.py --dry-run     full rehearsal, never broadcasts
    python radar_watch.py               live, sends real transactions

It only ever acts on rows where you ticked Armed and Result is still Pending.
A high score does not authorize anything. That tick is the entire safety model:
the radar proposes, you authorize, this fires.

How it fires: it probes the contract for a callable mint function in a tight
loop as the open time arrives. Before a mint opens every call reverts, which is
exactly the signal that it has not opened yet. The first call that simulates
successfully is the moment the mint is live, and the transaction goes out on
that same iteration.
"""

import argparse
import os
import sys
import time
from datetime import datetime, timezone

from dotenv import load_dotenv

import config
from minter import Minter
from radar import chain, mint_direct, notion_log, seadrop, settings

# How often to re-read the watchlist while nothing is close to opening.
WATCHLIST_POLL_SECONDS = 60

# Start hammering the contract this many seconds before the listed open time.
FIRE_LEAD_SECONDS = 3.0

# Gap between probe attempts inside the fire window.
FIRE_RETRY_SECONDS = 0.2

# Give up this long after the listed open time.
FIRE_TIMEOUT_SECONDS = 90


def log(msg=""):
    if msg:
        print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)
    else:
        print(flush=True)


def parse_open_time(value):
    """Notion hands back an ISO date or datetime. Return unix seconds or None."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def fire(row, w3, minter, wallet, dry_run):
    """
    Race a single armed drop. Returns (result_string, tx_hash_or_None).
    """
    contract = row.get("mint_contract")
    if not contract:
        log(f"  {row['name']}: armed but no mint contract on the row. Skipping.")
        return "Skipped", None

    # SeaDrop's own schedule beats whatever the row says. The row was written at
    # sweep time and the team can move the stage afterwards; this is read now.
    drop = seadrop.public_drop(w3, contract)
    open_time = parse_open_time(row.get("mint_open"))
    if drop:
        open_time = drop["start"]
        if drop["price_wei"] > config.MAX_MINT_VALUE_WEI:
            log(f"  {row['name']}: SeaDrop stage costs {drop['price_eth']:.6f} ETH, "
                f"over the {config.MAX_MINT_PRICE_NATIVE} native-coin cap. Not minting.")
            return "Skipped", None
        if drop["state"] == "ended":
            log(f"  {row['name']}: the public stage already closed. Nothing to race.")
            return "Skipped", None

    now = time.time()
    if open_time and now < open_time - FIRE_LEAD_SECONDS:
        return None, None  # not our turn yet

    log(f"FIRING on {row['name']} ({contract})")
    if drop:
        log(f"  {seadrop.describe(drop)}")
    if row.get("flags"):
        log(f"  risk flags on this row: {', '.join(row['flags'])}")

    # Warm up before the race so no slow lookup happens inside it.
    try:
        live_chain, nonce = minter.warm_up()
        log(f"  warm-up ok. chain {live_chain}, nonce {nonce}")
    except Exception as e:
        log(f"  warm-up failed: {type(e).__name__}: {e}")
        return "Skipped", None

    deadline = max(open_time or now, now) + FIRE_TIMEOUT_SECONDS
    attempts = 0
    target = contract
    calldata = None
    value = 0

    quiet = lambda *_: None
    while time.time() < deadline:
        attempts += 1

        # Replay first. A survey of real mints on this chain found none of the
        # guessable signatures in use, so copying a call the chain has already
        # accepted beats guessing at one. It also finds mints routed through
        # SeaDrop, where the right destination is not the collection at all.
        to, data, replay_value, _ = mint_direct.replay_probe(
            w3, contract, wallet, log=quiet,
            max_value_wei=config.MAX_MINT_VALUE_WEI)
        if data:
            target, calldata, value = to, data, replay_value
            log(f"  mint is OPEN. Replayed a live mint after {attempts} probe(s).")
            break

        # Fall back to guessing, which still wins on plain public mints and
        # costs nothing once the replay path has already failed.
        data, signature = mint_direct.probe(
            w3, contract, wallet, config.MINT_QUANTITY, log=quiet)
        if data:
            target, calldata, value = contract, data, 0
            log(f"  mint is OPEN. {signature} accepted after {attempts} probe(s).")
            break

        time.sleep(FIRE_RETRY_SECONDS)

    if not calldata:
        log(f"  no callable mint before the timeout ({attempts} probes). Either it "
            f"never opened, it sold out, this wallet is not eligible, or it is "
            f"gated on a server signature. Nothing was sent.")
        return "Skipped", None

    if value > config.MAX_MINT_VALUE_WEI:
        log(f"  refusing to send: this mint costs {value} wei and exceeds the "
            f"{config.MAX_MINT_PRICE_NATIVE} native-coin cap. Raise "
            f"MAX_MINT_PRICE_NATIVE deliberately if you actually want to pay for it.")
        return "Skipped", None

    signed, summary = minter.build_transaction(target, calldata, value)
    log("  transaction ready:")
    for key, value in summary.items():
        log(f"      {key}: {value}")

    if dry_run:
        log("  DRY RUN: not broadcasting. This is what would have been sent.")
        return None, None

    try:
        tx_hash = minter.send(signed)
    except Exception as e:
        message = str(e).lower()
        if "nonce too low" in message or "invalid nonce" in message:
            log("  stale nonce, refetching and retrying once...")
            minter.refresh_nonce()
            signed, _ = minter.build_transaction(target, calldata, value)
            tx_hash = minter.send(signed)
        else:
            log(f"  broadcast FAILED: {type(e).__name__}: {str(e)[:200]}")
            log("  if that was a timeout the tx MAY still have landed. Check the "
                "wallet in an explorer before re-running.")
            return "Skipped", None

    log(f"  sent: {tx_hash}")
    try:
        ok = minter.wait_for_confirmation(tx_hash)
    except Exception:
        log("  sent but not confirmed in the wait window. Check the explorer.")
        return "Pending", tx_hash

    if ok:
        log(f"  MINTED {row['name']}.")
        return "Minted", tx_hash
    log(f"  transaction reverted. Someone took the last spot, or the stage closed.")
    return "Reverted", tx_hash


def main():
    parser = argparse.ArgumentParser(description="Mint the drops you armed in Notion")
    parser.add_argument("--dry-run", action="store_true",
                        help="Rehearse everything except the broadcast")
    parser.add_argument("--once", action="store_true",
                        help="Check the watchlist once and exit instead of looping")
    args = parser.parse_args()

    load_dotenv()

    wallet = os.getenv("WALLET_ADDRESS")
    private_key = os.getenv("PRIVATE_KEY")
    alchemy = os.getenv("ALCHEMY_API_KEY")
    missing = [n for n, v in (("WALLET_ADDRESS", wallet), ("PRIVATE_KEY", private_key),
                              ("ALCHEMY_API_KEY", alchemy)) if not v]
    if missing:
        log("Your .env is missing: " + ", ".join(missing))
        sys.exit(1)

    if not notion_log.enabled():
        # Not fatal. The offline board is a full stand-in for the Notion one, so
        # the executor works before Notion is configured. It is only empty if no
        # sweep has run yet, which is a different problem and says so.
        log(f"Notion is not configured, using the offline board at {settings.LOCAL_BOARD_FILE}")
        log('Arm a row there by setting "armed": true. Nothing else sets it.')

    log("LIVE mode: armed rows will be minted for real." if not args.dry_run
        else "DRY RUN: nothing will be broadcast.")

    w3, note = chain.connect()
    if note:
        log(f"Cannot reach {settings.CHAIN_NAME} Chain: {note}")
        sys.exit(1)

    rpc = f"https://robinhood-mainnet.g.alchemy.com/v2/{alchemy}"
    minter = Minter(rpc, private_key, wallet, settings.CHAIN_ID)

    while True:
        rows = notion_log.armed_rows(log=log)
        if not rows:
            log("No armed rows pending.")
        else:
            log(f"{len(rows)} armed row(s) pending.")

        for row in rows:
            open_time = parse_open_time(row.get("mint_open"))
            if open_time:
                delta = open_time - time.time()
                if delta > FIRE_LEAD_SECONDS:
                    log(f"  {row['name']}: opens in {int(delta)}s")
                    continue
                if delta < -FIRE_TIMEOUT_SECONDS:
                    log(f"  {row['name']}: open time passed over "
                        f"{int(-delta)}s ago, still trying anyway")

            result, tx_hash = fire(row, w3, minter, wallet, args.dry_run)
            if result:
                status = {"Minted": "Minted", "Reverted": "Missed"}.get(result)
                notion_log.mark_result(row["page_id"], result, tx_hash,
                                       status=status, log=log)

        if args.once:
            return

        # Sleep less when something is close, so a drop opening in 90s is not
        # missed by a 60s poll landing badly.
        soonest = min((parse_open_time(r.get("mint_open")) or 1e18) - time.time()
                      for r in rows) if rows else 1e18
        time.sleep(max(1.0, min(WATCHLIST_POLL_SECONDS, soonest - FIRE_LEAD_SECONDS)))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
        sys.exit(130)
