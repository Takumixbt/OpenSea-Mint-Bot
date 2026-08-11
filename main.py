"""
The bot. Run it like this:

    python main.py --dry-run     (safe: does everything EXCEPT send the tx)
    python main.py               (real: actually sends the mint transaction)

What it does, in order:
  1. Log in to OpenSea with your wallet (or reuse a saved session).
  2. Read the drop's schedule and find when your chosen stage opens.
  3. Wait, printing a countdown.
  4. At 5 seconds before open, "warm up" (open connections, pre-fetch nonce).
  5. At 1.5 seconds before open, hammer OpenSea for the real mint calldata.
  6. The instant valid calldata arrives, build + sign the transaction and send
     it (or, in --dry-run, print exactly what it WOULD send and stop).
  7. Wait for the transaction to confirm and report success or failure.
"""

import argparse
import os
import sys
import time
from decimal import Decimal, InvalidOperation

from dotenv import load_dotenv

import config
import opensea_auth
import opensea_client
from minter import Minter


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def redact_secrets(text):
    # Error messages from the network stack can embed the RPC url, which
    # contains the Alchemy key. Scrub every secret before anything is printed.
    for name in ("ALCHEMY_API_KEY", "PRIVATE_KEY"):
        value = os.getenv(name)
        if value and len(value) > 8:
            text = text.replace(value, f"<{name} hidden>")
            if value.startswith("0x"):
                text = text.replace(value[2:], f"<{name} hidden>")
    return text


def looks_like_auth_failure(exc):
    import httpx
    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in (401, 403):
        return True
    msg = str(exc).lower()
    return any(word in msg for word in ("unauthorized", "unauthenticated", "forbidden", "not logged in"))


def load_env():
    load_dotenv()
    alchemy_key = os.getenv("ALCHEMY_API_KEY")
    key = os.getenv("PRIVATE_KEY")
    addr = os.getenv("WALLET_ADDRESS")
    missing = [n for n, v in
               [("ALCHEMY_API_KEY", alchemy_key), ("PRIVATE_KEY", key),
                ("WALLET_ADDRESS", addr)] if not v]
    if missing:
        print("Your .env is missing: " + ", ".join(missing))
        print("Copy .env.example to .env and fill it in (see README steps 3-5).")
        sys.exit(1)
    return alchemy_key, key, addr


def build_rpc_url(alchemy_key, chain_id):
    """
    Turns config.TARGET_CHAIN_ID into a real RPC url using the one Alchemy
    key in .env - no separate RPC url to set up per chain.
    """
    subdomain = config.CHAIN_RPC_SUBDOMAINS.get(chain_id)
    if not subdomain:
        print(f"Chain ID {chain_id} isn't in config.CHAIN_RPC_SUBDOMAINS yet.")
        print("Search \"Alchemy <chain name> RPC\", copy the subdomain from the "
              "url it shows you, and add {chain_id}: \"that-subdomain\" to "
              "CHAIN_RPC_SUBDOMAINS in config.py.")
        sys.exit(1)
    return f"https://{subdomain}.g.alchemy.com/v2/{alchemy_key}"


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
    parser.add_argument("--dry-run", action="store_true",
                        help="Run everything but do NOT send the transaction.")
    parser.add_argument(
        "--max-mint-price",
        metavar="NATIVE_COIN",
        help="Override the per-mint price cap for this run (0 keeps it free-only).",
    )
    args = parser.parse_args()

    if args.max_mint_price is not None:
        _set_cli_price_cap(args.max_mint_price)

    if args.dry_run:
        log("DRY RUN mode: I will do everything except actually send the mint.")
    else:
        log("LIVE mode: I will send a real transaction if the mint opens.")

    alchemy_key, private_key, wallet_address = load_env()
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

    # --- Log in to OpenSea ---
    client = opensea_auth.get_authenticated_client(private_key, wallet_address)

    # --- Read the schedule ---
    # This first call doubles as the check that a reused saved session is
    # actually still accepted; if OpenSea rejects it, log in fresh once.
    try:
        drop_name, stages = opensea_client.get_drop_schedule(client, slug)
    except Exception as e:
        if not looks_like_auth_failure(e):
            raise
        log("Saved OpenSea session was rejected - signing in fresh...")
        opensea_auth.discard_session()
        client.close()
        client = opensea_auth.get_authenticated_client(private_key, wallet_address,
                                                       force_fresh=True)
        drop_name, stages = opensea_client.get_drop_schedule(client, slug)
    log(f"Found drop: {drop_name!r} with {len(stages)} stage(s):")
    for s in stages:
        when = (time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(s['startTime']))
                if s['startTime'] else "unknown")
        log(f"    stage {s['stageIndex']}: opens {when}")

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
    minter = Minter(rpc_url, private_key, wallet_address, chain_id)
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
                    _, fresh_stages = opensea_client.get_drop_schedule(client, slug)
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

    # --- Fire loop: hammer OpenSea for the real calldata ---
    log("FIRING: asking OpenSea for the mint instructions (salt + signature)...")
    # If we started late (mint already open), still give ourselves the full
    # timeout window from now rather than exiting without a single attempt.
    deadline = max(open_time, time.time()) + config.FIRE_TIMEOUT_SECONDS
    calldata = None
    attempts = 0
    last_note = None
    while time.time() < deadline:
        attempts += 1
        calldata, note = opensea_client.get_mint_calldata(
            client, slug, target["stageIndex"], config.MINT_QUANTITY, wallet_address)
        if calldata:
            log(f"Got valid mint instructions after {attempts} attempt(s).")
            break
        # Log each distinct reason once, not 7 times per second.
        if note != last_note:
            log(f"    attempt {attempts}: {note} - retrying...")
            last_note = note
        time.sleep(config.FIRE_RETRY_SECONDS)

    if not calldata:
        log("Never received valid mint instructions before the timeout. "
            "The mint may not have opened, be sold out, or your wallet may not "
            "be eligible for this stage. Nothing was sent.")
        sys.exit(1)

    # --- Build + sign the transaction ---
    log("Building and signing the transaction...")
    signed, summary = minter.build_transaction(
        calldata["to"], calldata["data"], calldata["value"])
    log("Transaction ready:")
    for k, v in summary.items():
        log(f"    {k}: {v}")

    if args.dry_run:
        log("DRY RUN: stopping here. Nothing was sent. The transaction above is "
            "what I WOULD have broadcast. If it looks right, run without --dry-run.")
        return

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
                calldata["to"], calldata["data"], calldata["value"])
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
