"""Read-only readiness report for the NFT Mint Bot.

Run from this directory:

    python status.py

This script never signs, broadcasts, or changes wallet state. It prints only
health facts; it never prints keys, API tokens, or the wallet address.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import importlib
import os
import sys
from decimal import Decimal, InvalidOperation
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
    upper = value.upper()
    return bool(value) and "PASTE_" not in upper and "YOUR_" not in upper


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
        add("INFO", "Target collection", "not configured; use Telegram /scan or set a drop in config.py")

    target_chain = config.chain_slug_for_id(config.TARGET_CHAIN_ID)
    if target_chain is None:
        add("BLOCKED", "Target chain", f"chain {config.TARGET_CHAIN_ID} has no configured EVM RPC")
    else:
        add("OK", "Target chain", f"{target_chain} (chain ID {config.TARGET_CHAIN_ID})")

    if not isinstance(config.MINT_QUANTITY, int) or not 1 <= config.MINT_QUANTITY <= 100:
        add("BLOCKED", "Mint quantity", "MINT_QUANTITY must be an integer from 1 through 100")
    else:
        add("OK", "Mint quantity", str(config.MINT_QUANTITY))

    if config.MAX_MINT_VALUE_WEI == 0:
        add("OK", "Mint-value safety cap", "free-mint only (0 native coin max)")
    else:
        add("WARN", "Mint-value safety cap", f"paid mints allowed up to {config.MAX_MINT_PRICE_NATIVE} native coin")

    if config.MAX_BUY_VALUE_WEI == 0:
        add("OK", "Purchase safety cap", "secondary buying locked")
    else:
        add("WARN", "Purchase safety cap", f"buys allowed up to {config.MAX_BUY_PRICE_NATIVE} native coin")

    configured = config.monitored_chain_slugs()
    unsupported = [slug for slug in configured if not config.chain_config(slug)]
    supported = [slug for slug in configured if config.chain_config(slug)]
    if not supported:
        add("BLOCKED", "Daily scan chains", "MONITORED_CHAINS has no configured EVM chains")
    elif unsupported:
        add("WARN", "Daily scan chains", f"using {', '.join(supported)}; ignored unknown {', '.join(unsupported)}")
    else:
        add("OK", "Daily scan chains", ", ".join(supported))

    try:
        daily_limit = int(os.getenv("MAX_DAILY_MINTS", str(config.MAX_DAILY_MINTS)))
        if daily_limit < 1:
            raise ValueError
        daily_gas = Decimal(os.getenv("MAX_DAILY_GAS_NATIVE", config.MAX_DAILY_GAS_NATIVE))
        if daily_gas < 0 or not daily_gas.is_finite():
            raise ValueError
        add("OK", "Daily safety limits", f"up to {daily_limit} candidate attempts and configured gas cap")
    except (InvalidOperation, TypeError, ValueError):
        add("BLOCKED", "Daily safety limits", "MAX_DAILY_MINTS and MAX_DAILY_GAS_NATIVE must be valid non-negative values")


def check_environment():
    required = ("ALCHEMY_API_KEY", "PRIVATE_KEY", "WALLET_ADDRESS", "OPENSEA_API_KEY")
    missing = [name for name in required if not filled(name)]
    if missing:
        add("BLOCKED", "Required .env values", "missing or placeholder: " + ", ".join(missing))
    else:
        add("OK", "Required .env values", "Alchemy key, private key, wallet address, and OpenSea API key are present")

    if not missing:
        try:
            from wallets import load_wallet_profiles
            profiles = load_wallet_profiles(
                os.getenv("PRIVATE_KEY"), os.getenv("WALLET_ADDRESS")
            )
            add("OK", "Signing wallets", f"{len(profiles)} configured; keys not printed")
        except ValueError as exc:
            add("BLOCKED", "Signing wallets", str(exc))

    if filled("OPENSEA_API_KEY"):
        add("OK", "Official OpenSea API key", "present (not printed)")
    else:
        add("BLOCKED", "Official OpenSea API key", "required for discovery and mint-data requests")

    telegram_token = filled("TELEGRAM_BOT_TOKEN")
    telegram_chat = filled("TELEGRAM_ALLOWED_CHAT_ID")
    if telegram_token and telegram_chat:
        add("OK", "Telegram control", "bot token and authorized chat ID are present")
    elif telegram_token or telegram_chat:
        add("WARN", "Telegram control", "set both TELEGRAM_BOT_TOKEN and TELEGRAM_ALLOWED_CHAT_ID")
    else:
        add("INFO", "Telegram control", "not configured; the CLI still works")

    if os.getenv("ENABLE_LIVE_MINTS", "").strip().lower() in {"1", "true", "yes", "on"}:
        add("WARN", "Live switch", "enabled; use only with a funded throwaway wallet")
    else:
        add("OK", "Live switch", "disabled (safe default)")


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


def check_networks(no_network: bool):
    import config

    if no_network:
        add("INFO", "Network checks", "skipped by --no-network")
        return
    if not all(filled(name) for name in ("ALCHEMY_API_KEY", "PRIVATE_KEY", "WALLET_ADDRESS")):
        add("BLOCKED", "Alchemy RPC", "skipped because required .env values are not ready")
        return

    from web3 import Web3

    address = Web3.to_checksum_address(os.environ["WALLET_ADDRESS"])
    chains = [slug for slug in config.monitored_chain_slugs() if config.chain_config(slug)]

    def inspect_chain(slug):
        settings = config.chain_config(slug)
        try:
            rpc = config.rpc_url_for_chain(os.environ["ALCHEMY_API_KEY"], settings["chain_id"])
            w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 6}))
            if not w3.is_connected():
                return "BLOCKED", "could not connect"
            live_chain = int(w3.eth.chain_id)
            if live_chain != settings["chain_id"]:
                return "BLOCKED", f"connected to chain {live_chain}, expected {settings['chain_id']}"
            w3.eth.get_transaction_count(address, "pending")
            balance = w3.eth.get_balance(address)
            balance_native = Decimal(balance) / Decimal(10 ** 18)
            gas_ceiling = (
                Decimal(config.MAX_FEE_CAP_GWEI)
                * Decimal(config.GAS_LIMIT_MAX)
                / Decimal(10 ** 9)
            )
            if balance <= 0:
                return "WARN", f"connected; wallet has 0 {settings['native']} for gas"
            if balance_native < gas_ceiling:
                return (
                    "WARN",
                    f"connected; balance {balance_native:.12f} {settings['native']} is below "
                    f"the configured worst-case gas envelope {gas_ceiling} {settings['native']}",
                )
            return "OK", f"connected; balance {balance_native:.12f} {settings['native']}"
        except Exception as exc:
            return "BLOCKED", f"read-only check failed ({type(exc).__name__})"

    checked = {}
    with ThreadPoolExecutor(max_workers=min(8, len(chains) or 1)) as executor:
        futures = {executor.submit(inspect_chain, slug): slug for slug in chains}
        for future in as_completed(futures):
            checked[futures[future]] = future.result()
    for slug in chains:
        state, detail = checked[slug]
        add(state, f"RPC {slug}", detail)


def check_official_opensea_api():
    """Check the API key and configured target-drop route without minting."""
    import config

    if not filled("OPENSEA_API_KEY"):
        return
    try:
        import httpx

        response = httpx.get(
            f"{config.OPENSEA_API_BASE_URL.rstrip('/')}/drops",
            headers={
                "accept": "application/json",
                "user-agent": config.USER_AGENT,
                "x-api-key": os.environ["OPENSEA_API_KEY"],
            },
            params={"type": "upcoming", "limit": 1, "chains": "ethereum"},
            timeout=15,
        )
        if response.status_code == 200 and isinstance(response.json(), dict):
            add("OK", "OpenSea Drops API", "API key accepted and discovery route is readable")
        elif response.status_code in (401, 403):
            add("BLOCKED", "OpenSea Drops API", f"API key rejected (HTTP {response.status_code})")
        elif response.status_code == 429:
            add("WARN", "OpenSea Drops API", "rate limited (HTTP 429); try again later")
        else:
            add("BLOCKED", "OpenSea Drops API", f"unexpected HTTP {response.status_code} response")
    except Exception as exc:
        add("BLOCKED", "OpenSea Drops API", f"read-only check failed ({type(exc).__name__})")


def main() -> int:
    from resolver import install as install_secure_dns
    install_secure_dns()
    parser = argparse.ArgumentParser(description="Read-only NFT Mint Bot readiness report")
    parser.add_argument("--no-network", action="store_true", help="skip RPC and OpenSea endpoint checks")
    args = parser.parse_args()

    print(f"NFT Mint Bot status — {ROOT}")
    print("Read-only checks only; no transaction is signed or broadcast.\n")
    check_config()
    check_environment()
    check_dependencies()
    check_wallet_identity()
    check_networks(args.no_network)
    if args.no_network:
        add("INFO", "OpenSea Drops API", "skipped by --no-network")
    else:
        check_official_opensea_api()

    blocked = sum(1 for state, _, _ in results if state == "BLOCKED")
    warnings = sum(1 for state, _, _ in results if state == "WARN")
    target_ready = any(label == "Target collection" and state == "OK" for state, label, _ in results)
    telegram_ready = filled("TELEGRAM_BOT_TOKEN") and filled("TELEGRAM_ALLOWED_CHAT_ID")

    print("\nSummary")
    if blocked:
        print(f"NOT READY — {blocked} blocking check(s), {warnings} warning(s).")
    elif not target_ready:
        if telegram_ready:
            print("TELEGRAM READY — set TARGET_COLLECTION_URL only if you also want the one-drop CLI.")
        else:
            print("CLI NOT READY — set TARGET_COLLECTION_URL to use the one-drop CLI; Telegram discovery can still scan.")
    else:
        print("CONFIGURED — review the specific drop, quantity, price cap, and gas balance before live use.")
    return 1 if blocked or (not target_ready and not telegram_ready) else 0


if __name__ == "__main__":
    raise SystemExit(main())
