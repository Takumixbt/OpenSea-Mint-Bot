"""Read-only readiness report for the OpenSea mint bot.

Run from this directory:

    python status.py
    python status.py --full-recon

This script never signs, broadcasts, or changes wallet state. It intentionally
prints only booleans and health facts; it never prints keys, API tokens, or the
wallet address.
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

results: list[tuple[str, str, str]] = []


def add(state: str, label: str, detail: str) -> None:
    results.append((state, label, detail))
    print(f"[{state:<7}] {label}: {detail}")


def filled(name: str) -> bool:
    value = os.getenv(name, "").strip()
    return bool(value) and "PASTE_" not in value and "YOUR_" not in value


def check_config():
    import config

    try:
        slug = config.target_collection_slug()
    except ValueError as exc:
        add("BLOCKED", "Target collection", str(exc))
        slug = None
    if slug:
        add("OK", "Target collection", f"configured as {slug!r}")
    else:
        add("BLOCKED", "Target collection", "paste an OpenSea collection/drop URL into config.py")

    if config.TARGET_CHAIN_ID not in config.CHAIN_RPC_SUBDOMAINS:
        add("BLOCKED", "Target chain", f"chain {config.TARGET_CHAIN_ID} has no RPC mapping")
    else:
        add("OK", "Target chain", f"chain ID {config.TARGET_CHAIN_ID}")

    if config.MINT_QUANTITY < 1:
        add("BLOCKED", "Mint quantity", "MINT_QUANTITY must be at least 1")
    else:
        add("OK", "Mint quantity", str(config.MINT_QUANTITY))

    if config.MAX_MINT_VALUE_WEI == 0:
        add("OK", "Mint-value safety cap", "free-mint only (0 wei max)")
    else:
        add(
            "WARN",
            "Mint-value safety cap",
            f"paid mints allowed up to {config.MAX_MINT_PRICE_NATIVE} native coin",
        )


def check_environment():
    required = ("ALCHEMY_API_KEY", "PRIVATE_KEY", "WALLET_ADDRESS")
    missing = [name for name in required if not filled(name)]
    if missing:
        add("BLOCKED", "Required .env values", "missing or placeholder: " + ", ".join(missing))
    else:
        add("OK", "Required .env values", "Alchemy key, private key, and wallet address are present")

    if filled("OPENSEA_API_KEY"):
        add("OK", "Official OpenSea API key", "present (not printed)")
    else:
        add("INFO", "Official OpenSea API key", "not configured; the existing bot uses its internal website flow")

    if filled("NOTION_TOKEN") and filled("NOTION_DATABASE_ID"):
        add("OK", "Radar Notion settings", "token and database ID are present")
    elif filled("NOTION_TOKEN") or filled("NOTION_DATABASE_ID"):
        add("WARN", "Radar Notion settings", "token and database ID must both be set; using the local board")
    else:
        add("WARN", "Radar Notion settings", "not configured; radar will use its local offline board")


def check_dependencies():
    modules = ("httpx", "web3", "eth_account", "dotenv")
    missing = []
    for module in modules:
        try:
            importlib.import_module(module)
        except Exception:
            missing.append(module)
    if missing:
        add("BLOCKED", "Python dependencies", "cannot import: " + ", ".join(missing))
    else:
        add("OK", "Python dependencies", "all required imports succeeded")


def check_wallet_identity():
    if not all(filled(name) for name in ("PRIVATE_KEY", "WALLET_ADDRESS")):
        return
    try:
        from eth_account import Account

        derived = Account.from_key(os.environ["PRIVATE_KEY"]).address
        configured = os.environ["WALLET_ADDRESS"].strip()
        if derived.lower() == configured.lower():
            add("OK", "Wallet identity", "private key matches WALLET_ADDRESS")
        else:
            add("BLOCKED", "Wallet identity", "private key does not match WALLET_ADDRESS")
    except Exception:
        add("BLOCKED", "Wallet identity", "PRIVATE_KEY is not a valid EVM private key")


def check_session():
    import config

    path = ROOT / config.SESSION_FILE
    if not path.exists():
        add("WARN", "OpenSea session", "no saved session; the first run must complete wallet sign-in")
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        saved_at = int(data.get("saved_at", 0))
        cookies = data.get("cookies")
        age_hours = max(0, (time.time() - saved_at) / 3600)
        if not isinstance(cookies, dict) or not cookies:
            add("WARN", "OpenSea session", "session file has no cookies; a fresh sign-in is required")
            return False
        if age_hours > 72:
            add("WARN", "OpenSea session", f"saved session is {age_hours:.1f} hours old; refresh required")
            return False
        add("OK", "OpenSea session", f"saved session is {age_hours:.1f} hours old")
        return True
    except Exception:
        add("WARN", "OpenSea session", "session file is unreadable; a fresh sign-in is required")
        return False


def check_networks(no_network: bool):
    import config

    if no_network:
        add("INFO", "Network checks", "skipped by --no-network")
        return

    if not all(filled(name) for name in ("ALCHEMY_API_KEY", "PRIVATE_KEY", "WALLET_ADDRESS")):
        add("BLOCKED", "Alchemy RPC", "skipped because required .env values are not ready")
        return

    try:
        from web3 import Web3

        subdomain = config.CHAIN_RPC_SUBDOMAINS[config.TARGET_CHAIN_ID]
        rpc = f"https://{subdomain}.g.alchemy.com/v2/{os.environ['ALCHEMY_API_KEY']}"
        w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 10}))
        if not w3.is_connected():
            add("BLOCKED", "Alchemy RPC", "could not connect")
        else:
            live_chain = int(w3.eth.chain_id)
            if live_chain != config.TARGET_CHAIN_ID:
                add("BLOCKED", "Alchemy RPC", f"connected to chain {live_chain}, expected {config.TARGET_CHAIN_ID}")
            else:
                address = Web3.to_checksum_address(os.environ["WALLET_ADDRESS"])
                w3.eth.get_transaction_count(address, "pending")
                add("OK", "Alchemy RPC", f"connected to chain {live_chain}; wallet nonce readable")
                balance = w3.eth.get_balance(address)
                balance_native = float(Web3.from_wei(balance, "ether"))
                gas_ceiling = w3.to_wei(config.MAX_FEE_CAP_GWEI, "gwei") * config.GAS_LIMIT_MAX
                conservative_required = config.MAX_MINT_VALUE_WEI + gas_ceiling
                required_native = float(Web3.from_wei(conservative_required, "ether"))
                if balance <= 0:
                    add("BLOCKED", "Gas balance", "wallet has 0 native coin; fund it before a live run")
                elif balance < conservative_required:
                    add(
                        "WARN",
                        "Gas balance",
                        f"{balance_native:.12f} native available; conservative configured ceiling is "
                        f"{required_native:.12f} (the actual estimate may be lower)",
                    )
                else:
                    add("OK", "Gas balance", f"{balance_native:.12f} native available")
    except Exception as exc:
        add("BLOCKED", "Alchemy RPC", f"read-only check failed ({type(exc).__name__})")

    try:
        import httpx

        response = httpx.post(
            config.GQL_ENDPOINT,
            headers={
                "content-type": "application/json",
                "user-agent": config.USER_AGENT,
                "x-app-id": config.APP_ID_HEADER,
            },
            json={"query": "query{__typename}"},
            timeout=15,
        )
        body = response.json()
        if response.status_code < 500 and isinstance(body, dict) and ("data" in body or "errors" in body):
            add("OK", "OpenSea GraphQL endpoint", f"responded with HTTP {response.status_code}")
        else:
            add("BLOCKED", "OpenSea GraphQL endpoint", f"unexpected HTTP {response.status_code} response")
    except Exception as exc:
        add("BLOCKED", "OpenSea GraphQL endpoint", f"read-only check failed ({type(exc).__name__})")


def check_radar():
    path = ROOT / "radar" / "state" / "board.json"
    if not path.exists():
        add("INFO", "Radar board", "no local board found yet")
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        rows = data.get("rows") or []
        armed = sum(1 for row in rows if row.get("armed") is True)
        pending = sum(1 for row in rows if row.get("result") == "Pending")
        stamp = data.get("updated_at") or "unknown"
        add("INFO", "Radar board", f"{len(rows)} rows, {armed} armed, {pending} pending; updated {stamp}")
    except Exception:
        add("WARN", "Radar board", "local board is unreadable")


def run_full_recon():
    print("\n--- full OpenSea drift check ---")
    completed = subprocess.run(
        [sys.executable, str(ROOT / "recon_check.py")],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.stdout:
        print(completed.stdout, end="")
    if completed.stderr:
        print(completed.stderr, end="", file=sys.stderr)
    if completed.returncode == 0:
        add("OK", "Full OpenSea drift check", "no CHANGED/FAIL items")
    else:
        add("BLOCKED", "Full OpenSea drift check", f"recon_check.py exited {completed.returncode}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only NFT mint bot readiness report")
    parser.add_argument("--no-network", action="store_true", help="skip RPC and OpenSea endpoint checks")
    parser.add_argument("--full-recon", action="store_true", help="also run the slower OpenSea bundle drift check")
    args = parser.parse_args()

    print(f"Mint bot status — {ROOT}")
    print("Read-only checks only; no transaction is signed or broadcast.\n")
    check_config()
    check_environment()
    check_dependencies()
    check_wallet_identity()
    session_ready = check_session()
    check_networks(args.no_network)
    check_radar()
    if args.full_recon:
        run_full_recon()

    blocked = sum(1 for state, _, _ in results if state == "BLOCKED")
    warnings = sum(1 for state, _, _ in results if state == "WARN")
    target_ready = any(label == "Target collection" and state == "OK" for state, label, _ in results)

    print("\nSummary")
    if blocked:
        print(f"NOT READY — {blocked} blocking check(s), {warnings} warning(s).")
    elif not target_ready:
        print("NOT READY — set TARGET_COLLECTION_URL to a real OpenSea drop first.")
    elif not session_ready:
        print("SETUP READY, NOT INSTANT-READY — complete one OpenSea sign-in, then dry-run the target drop.")
    else:
        print("CONFIGURED — still run a dry run for the specific drop before live use.")
    return 1 if blocked or not target_ready else 0


if __name__ == "__main__":
    raise SystemExit(main())
