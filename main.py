"""
The bot. Run it like this:

    python main.py               (real: actually sends the mint transaction)

What it does, in order:
  1. Read the drop's schedule from OpenSea's documented Drops API.
  2. Find when your chosen stage opens.
  3. Wait, printing a countdown.
  4. At 5 seconds before open, "warm up" (open connections, pre-fetch nonce).
  5. At the opening, ask OpenSea's supported Drops API for ready-to-sign mint data.
  6. Verify its exact mint value against the selected stage and price cap,
     then build, sign, and send the transaction.
  7. Wait for the transaction to confirm and report success or failure.
"""

import argparse
import os
import sys
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path

from dotenv import load_dotenv

import config
import opensea_client
from minter import Minter


ROOT = Path(__file__).resolve().parent


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def redact_secrets(text):
    # Error messages from the network stack can embed the RPC url, which
    # contains the Alchemy key. Scrub every secret before anything is printed.
    for name in ("ALCHEMY_API_KEY", "PRIVATE_KEY", "OPENSEA_API_KEY"):
        value = os.getenv(name)
        if value and len(value) > 8:
            text = text.replace(value, f"<{name} hidden>")
            if value.startswith("0x"):
                text = text.replace(value[2:], f"<{name} hidden>")
    return text


def load_env():
    load_dotenv(ROOT / ".env")
    alchemy_key = os.getenv("ALCHEMY_API_KEY")
    key = os.getenv("PRIVATE_KEY")
    addr = os.getenv("WALLET_ADDRESS")
    opensea_key = os.getenv("OPENSEA_API_KEY")
    missing = [n for n, v in
               [("ALCHEMY_API_KEY", alchemy_key), ("PRIVATE_KEY", key),
                ("WALLET_ADDRESS", addr), ("OPENSEA_API_KEY", opensea_key)]
               if not v or "PASTE_" in v or "YOUR_" in v]
    if missing:
        print("Your .env is missing: " + ", ".join(missing))
        print("Copy .env.example to .env and fill in the four required values.")
        sys.exit(1)
    return alchemy_key, key, addr, opensea_key


def build_rpc_url(alchemy_key, chain_id):
    """
    Turns config.TARGET_CHAIN_ID into a real RPC url using the one Alchemy
    key in .env - no separate RPC url to set up per chain.
    """
    try:
        return config.rpc_url_for_chain(alchemy_key, chain_id)
    except ValueError:
        print(f"Chain ID {chain_id} is not configured in config.CHAIN_CONFIGS yet.")
        print("Add its OpenSea chain slug, chain ID, and Alchemy RPC subdomain to config.py.")
        sys.exit(1)


def find_target_stage(stages):
    for s in stages:
        if s["stageIndex"] == config.TARGET_STAGE_INDEX:
            return s
    return None


def _set_cli_price_cap(raw_value):
    try:
        value = Decimal(raw_value)
        scaled = value * (10 ** 18)
        if value < 0 or scaled != scaled.to_integral_value():
            raise ValueError
    except (InvalidOperation, ValueError):
        print("--max-mint-price must be a non-negative decimal with at most 18 decimals.")
        sys.exit(2)
    config.MAX_MINT_PRICE_NATIVE = str(value)
    config.MAX_MINT_VALUE_WEI = int(scaled)


def main():
    parser = argparse.ArgumentParser(description="OpenSea mint bot")
    parser.add_argument(
        "--confirm-live",
        action="store_true",
        help="Required acknowledgement that this command may broadcast a real mint.",
    )
    parser.add_argument(
        "--max-mint-price",
        metavar="NATIVE_COIN",
        help="Override the per-mint price cap for this run (0 keeps it free-only).",
    )
    args = parser.parse_args()

    if not args.confirm_live:
        parser.error("live execution requires --confirm-live")
    if os.getenv("ENABLE_LIVE_MINTS", "").strip().lower() not in {"1", "true", "yes", "on"}:
        parser.error("set ENABLE_LIVE_MINTS=true in .env before live execution")

    if args.max_mint_price is not None:
        _set_cli_price_cap(args.max_mint_price)

    log("LIVE mode: I will send a real transaction if the mint opens.")

    alchemy_key, private_key, wallet_address, opensea_api_key = load_env()
    chain_id = config.TARGET_CHAIN_ID
    rpc_url = build_rpc_url(alchemy_key, chain_id)
    log(f"Targeting chain ID {chain_id} via Alchemy.")
    log(f"Maximum mint price: {config.MAX_MINT_PRICE_NATIVE} native coin.")

    try:
        slug = config.target_collection_slug()
    except ValueError as exc:
        log(f"Target URL is invalid: {exc}")
        sys.exit(1)
    if not slug:
        log("Paste the OpenSea collection/drop URL into TARGET_COLLECTION_URL in config.py.")
        sys.exit(1)

    # --- Read the schedule from OpenSea's documented API ---
    client = opensea_client.get_api_client(opensea_api_key)
    try:
        drop_name, stages = opensea_client.get_drop_schedule(
            client, slug, opensea_api_key)
    except Exception as e:
        log(f"Could not read the OpenSea drop schedule: {redact_secrets(str(e))[:300]}")
        client.close()
        sys.exit(1)
    log(f"Found drop: {drop_name!r} with {len(stages)} stage(s):")
    for s in stages:
        when = (time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(s['startTime']))
                if s['startTime'] else "unknown")
        label = f" ({s['label']})" if s.get("label") else ""
        log(f"    stage {s['stageIndex']}{label}: opens {when}")

    target = find_target_stage(stages)
    if not target or not target["startTime"]:
        log(f"Could not find stage {config.TARGET_STAGE_INDEX} with a known start "
            f"time. Adjust TARGET_STAGE_INDEX in config.py to one listed above.")
        sys.exit(1)

    open_time = target["startTime"]
    if target.get("endTime") and time.time() > target["endTime"]:
        log(f"Stage {target['stageIndex']} already ENDED at "
            f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(target['endTime']))}. "
            f"Nothing to mint; nothing was sent.")
        sys.exit(1)
    if time.time() > open_time:
        log(f"Stage {target['stageIndex']} opened at "
            f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(open_time))} and is "
            f"already open - warming up and trying immediately.")
    else:
        log(f"Targeting stage {target['stageIndex']}, opens at "
            f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(open_time))}.")

    # --- Set up the blockchain side ---
    minter = Minter(
        rpc_url,
        private_key,
        wallet_address,
        chain_id,
        rpc_urls=config.rpc_urls_for_chain(alchemy_key, chain_id),
    )
    try:
        balance = minter.native_balance()
    except Exception as e:
        log(f"Could not read the wallet's gas balance: {type(e).__name__}.")
        sys.exit(1)
    if balance <= 0:
        log("Wallet has 0 native coin for gas. Fund it on the target chain before "
            "starting a live run. Nothing was signed or sent.")
        sys.exit(1)
    warmed = False

    # --- Wait / warm-up / fire loop ---
    while True:
        seconds_left = open_time - time.time()

        if seconds_left > config.WARMUP_LEAD_SECONDS:
            # Still far out: re-read the schedule each poll in case OpenSea
            # moves the time, and print a friendly countdown. A failed re-read
            # is fine - we keep the last known time and try again next poll.
            if seconds_left > config.WARMUP_LEAD_SECONDS + 5:
                try:
                    _, fresh_stages = opensea_client.get_drop_schedule(
                        client, slug, opensea_api_key)
                    fresh = find_target_stage(fresh_stages)
                    if fresh and fresh["startTime"] and fresh["startTime"] != open_time:
                        open_time = fresh["startTime"]
                        log(f"OpenSea MOVED the open time - now "
                            f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(open_time))}.")
                        seconds_left = open_time - time.time()
                        if seconds_left <= config.WARMUP_LEAD_SECONDS:
                            continue
                except Exception:
                    pass
            log(f"Waiting... mint opens in about {int(seconds_left)}s.")
            time.sleep(min(config.SCHEDULE_POLL_SECONDS, max(1, seconds_left - config.WARMUP_LEAD_SECONDS)))
            continue

        if not warmed and seconds_left <= config.WARMUP_LEAD_SECONDS:
            log("Warming up: checking chain and pre-fetching your wallet nonce...")
            live_chain, nonce = minter.warm_up()
            log(f"Warm-up done. Chain {live_chain}, next nonce {nonce}. Standing by.")
            warmed = True

        if seconds_left <= config.FIRE_LEAD_SECONDS:
            break

        time.sleep(0.05)

    # --- Fire loop: request official OpenSea mint transaction data ---
    log("FIRING: requesting ready-to-sign mint data from OpenSea's API...")
    # If we started late (mint already open), still give ourselves the full
    # timeout window from now rather than exiting without a single attempt.
    deadline = max(open_time, time.time()) + config.FIRE_TIMEOUT_SECONDS
    calldata = None
    attempts = 0
    last_note = None
    while time.time() < deadline and attempts < config.FIRE_MAX_ATTEMPTS:
        attempts += 1
        calldata, note = opensea_client.get_mint_calldata(
            client, slug, target["stageIndex"], config.MINT_QUANTITY,
            wallet_address, opensea_api_key)
        if calldata:
            log(f"Got valid mint instructions after {attempts} attempt(s).")
            break
        if note and note.startswith("STOP:"):
            log(note[5:].strip())
            break
        # Log each distinct reason once so the run log stays readable.
        if note != last_note:
            log(f"    attempt {attempts}: {note} - retrying...")
            last_note = note
        delays = config.FIRE_RETRY_DELAYS_SECONDS
        time.sleep(delays[min(attempts - 1, len(delays) - 1)])

    if not calldata:
        client.close()
        log("No usable mint transaction was received. Nothing was signed or sent.")
        sys.exit(1)

    client.close()

    # Bind the transaction to the exact stage price and quantity selected by
    # the operator. Unknown/fractional schedule prices are refused rather than
    # trusting a different value returned at fire time.
    try:
        unit_price = Decimal(str(target.get("price")))
        if unit_price < 0 or unit_price != unit_price.to_integral_value():
            raise ValueError
        approved_value_wei = int(unit_price) * int(config.MINT_QUANTITY)
    except (InvalidOperation, TypeError, ValueError):
        log("OpenSea did not provide an exact stage price in wei. Refusing to sign or send.")
        sys.exit(1)

    # --- Build + sign the transaction ---
    log("Building and signing the transaction...")
    signed, summary = minter.build_transaction(
        calldata["to"], calldata["data"], calldata["value"],
        approved_value_wei=approved_value_wei,
    )
    log("Transaction ready:")
    for k, v in summary.items():
        log(f"    {k}: {v}")

    # --- Send + confirm ---
    log("Sending the transaction to the network...")
    try:
        tx_hash = minter.send(signed)
    except Exception as e:
        msg = redact_secrets(str(e))
        # A stale pre-fetched nonce means OUR transaction was never accepted,
        # so one rebuild-and-resend with a fresh nonce cannot double-mint.
        if "nonce too low" in msg.lower() or "invalid nonce" in msg.lower():
            log("The pre-fetched nonce went stale (something else transacted from "
                "this wallet since warm-up). Refetching the nonce and retrying once...")
            minter.refresh_nonce()
            signed, summary = minter.build_transaction(
                calldata["to"], calldata["data"], calldata["value"],
                approved_value_wei=approved_value_wei,
            )
            log(f"Rebuilt with nonce {summary['nonce']}.")
            tx_hash = minter.send(signed)
        else:
            log(f"Broadcast FAILED: {type(e).__name__}: {msg[:300]}")
            log("IMPORTANT: if that was a network timeout, the transaction MAY still "
                "have reached the network. Before doing anything else, check your "
                "wallet address in a block explorer to see whether the mint went "
                "through. Do not immediately re-run.")
            sys.exit(1)
    log(f"Sent. Transaction hash: {tx_hash}")
    log("Waiting for it to confirm...")
    try:
        ok = minter.wait_for_confirmation(tx_hash)
    except Exception as e:
        log(f"Sent but could not confirm within the wait window: {e}")
        log("Check the transaction hash above in a block explorer.")
        return
    if ok:
        log("SUCCESS - the mint transaction confirmed. You minted.")
    else:
        log("The transaction confirmed but FAILED (reverted). Common causes: "
            "someone filled the last spot first, or the stage closed. No token "
            "was minted; you only paid the small network fee.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped by you (Ctrl-C).")
        sys.exit(130)
    except SystemExit:
        raise
    except Exception:
        # Network errors can embed the RPC url (which contains the Alchemy
        # key) in the traceback - scrub secrets before anything is printed.
        import traceback
        print(redact_secrets(traceback.format_exc()))
        sys.exit(1)
