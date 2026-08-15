"""
All the non-secret settings for the bot live here. You can safely open this
file, change the values, and save it. Nothing secret is in this file - your
private key and RPC url live in the separate .env file.

Each setting has a plain-English comment saying what it does.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import os
from urllib.parse import unquote, urlsplit

from dotenv import load_dotenv


load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

# ---------------------------------------------------------------------------
# WHICH DROP ARE YOU TRYING TO MINT?
# ---------------------------------------------------------------------------

# Beginner-friendly option: paste the complete OpenSea collection/drop URL here.
# The bot extracts the slug and ignores any query string. Leave this blank if
# you prefer to use TARGET_COLLECTION_SLUG below.
TARGET_COLLECTION_URL = ""

# Advanced option: the short name in the OpenSea web address. Example:
# https://opensea.io/collection/cool-cats -> "cool-cats".
# TARGET_COLLECTION_URL takes precedence when it is not blank.
TARGET_COLLECTION_SLUG = "PUT-THE-DROP-SLUG-HERE"


def target_collection_slug():
    """Return a validated OpenSea drop slug from either config input."""
    raw = (TARGET_COLLECTION_URL or TARGET_COLLECTION_SLUG or "").strip()
    if not raw or raw == "PUT-THE-DROP-SLUG-HERE":
        return None

    if raw.startswith(("https://", "http://")):
        parsed = urlsplit(raw)
        if parsed.netloc.lower() not in {"opensea.io", "www.opensea.io"}:
            raise ValueError("TARGET_COLLECTION_URL must point to opensea.io")
        parts = [unquote(part) for part in parsed.path.split("/") if part]
        if len(parts) < 2 or parts[0].lower() not in {"collection", "collections", "drop", "drops"}:
            raise ValueError(
                "TARGET_COLLECTION_URL must look like https://opensea.io/collection/<slug> "
                "or https://opensea.io/drops/<slug>"
            )
        raw = parts[-1]

    slug = raw.strip("/")
    if not slug or any(char.isspace() for char in slug):
        raise ValueError("the OpenSea collection slug cannot be empty or contain spaces")
    return slug

# Some drops have several "stages" (e.g. an allowlist stage, then a public
# stage). Stage 0 is usually the first one. If you only care about the public
# stage and there are earlier ones, you may need to raise this number.
# The bot prints every stage it finds when it runs, so you can see the choices.
TARGET_STAGE_INDEX = 0

# How many tokens to mint in the single transaction. OpenSea accepts 1-100;
# keep this at 1 unless the drop's wallet limit explicitly allows more.
MINT_QUANTITY = 1

# Which chain this drop is on. Check the collection's OpenSea page - the
# chain is shown right there. Change this per drop; everything else (the RPC
# connection) follows automatically from CHAIN_CONFIGS below, using
# the single Alchemy key in your .env.
#   1 = Ethereum, 8453 = Base, 137 = Polygon, 10 = Optimism,
#   42161 = Arbitrum, 4663 = Robinhood Chain
TARGET_CHAIN_ID = 8453  # Base


# ---------------------------------------------------------------------------
# TIMING (mirrors the approach in the article the bot is based on)
# ---------------------------------------------------------------------------

# How often (in seconds) to re-check the drop's schedule while we wait for it
# to get close. A slow, polite poll - we are just watching the clock here.
SCHEDULE_POLL_SECONDS = 5

# How many seconds BEFORE the mint opens to start "warming up": opening the
# network connections, pre-fetching the wallet's transaction number, and
# confirming the chain id, so none of that slow work happens during the race.
WARMUP_LEAD_SECONDS = 10

# How many seconds BEFORE the scheduled opening to leave the warm-up loop.
# The supported Drops API rejects early requests with HTTP 409, so the first
# mint-data request is made at the scheduled opening rather than hammering it
# before the drop is active.
FIRE_LEAD_SECONDS = 0.0

# If OpenSea activates a route a fraction of a second late, retry quickly at
# first, then back off. This keeps the first few seconds competitive without
# creating an unbounded request loop.
FIRE_RETRY_DELAYS_SECONDS = (0.20, 0.35, 0.50, 0.75, 1.0, 1.5, 2.0, 3.0, 5.0)

# Hard cap on mint-data requests in one opening window. This prevents a bad
# schedule or an API outage from creating an unbounded request loop.
FIRE_MAX_ATTEMPTS = 10

# Safety stop: if the mint instructions never arrive, give up this many seconds
# AFTER the scheduled open time instead of looping forever.
FIRE_TIMEOUT_SECONDS = 30


# ---------------------------------------------------------------------------
# GAS (how much you are willing to pay the network to include your transaction)
# ---------------------------------------------------------------------------
# For a FREE mint you still pay a small network fee ("gas") in the chain's coin
# (ETH on Ethereum/Base/etc). To win a race you usually bid a bit above the
# going rate. These knobs control that bid. They are deliberately capped so a
# weird network spike can't drain your wallet.

# We take the current going tip and multiply it by this to bid a little higher
# and jump the queue. 1.0 = match everyone else. 1.5 = bid 50% over. 2.0 = double.
PRIORITY_FEE_MULTIPLIER = 1.5

# A HARD CEILING on the total fee per unit of gas you will ever pay, in "gwei"
# (a tiny unit of the chain's coin). If the network's required fee is above
# this, the bot refuses to send rather than overpay. Raise it for pricey
# Ethereum mainnet moments; it can be much lower on cheap chains like Base.
MAX_FEE_CAP_GWEI = 50

# Extra head-room added to the network's estimated gas amount, as a multiplier,
# so a slightly heavier-than-estimated mint still goes through. 1.2 = +20%.
GAS_LIMIT_MULTIPLIER = 1.2

# If the automatic gas estimate fails (some drops block it before open time),
# fall back to this fixed gas amount. A simple NFT mint is usually well under
# 250000. Raise only if you see "out of gas" style failures.
GAS_LIMIT_FALLBACK = 250000

# An ABSOLUTE ceiling on the gas amount, whatever the estimate says. Your
# worst-case total network fee is roughly GAS_LIMIT_MAX x MAX_FEE_CAP_GWEI
# (with the defaults: 500000 gas x 50 gwei = 0.025 ETH). If an estimate ever
# comes back above this, the bot clamps it rather than authorize a surprise.
GAS_LIMIT_MAX = 500000

# Maximum amount of the chain's native coin the bot may send as the mint price.
# Use a human-readable value here: "0" means free-only; "0.02" allows a paid
# mint up to 0.02 ETH/MATIC/etc. Free mints (value 0) still pass automatically.
# This is deliberately a cap, never an instruction to spend that amount.
MAX_MINT_PRICE_NATIVE = os.getenv("MAX_MINT_PRICE_NATIVE", "0").strip() or "0"

try:
    _mint_price_scaled = Decimal(MAX_MINT_PRICE_NATIVE) * (10 ** 18)
    if _mint_price_scaled < 0 or _mint_price_scaled != _mint_price_scaled.to_integral_value():
        raise ValueError
    MAX_MINT_VALUE_WEI = int(_mint_price_scaled)
except (InvalidOperation, ValueError):
    raise ValueError(
        "MAX_MINT_PRICE_NATIVE must be a non-negative decimal with at most 18 decimals"
    )


def set_max_mint_price_native(value):
    """Update the in-process hard mint-value ceiling safely."""
    global MAX_MINT_PRICE_NATIVE, MAX_MINT_VALUE_WEI
    text = str(value or "").strip()
    try:
        scaled = Decimal(text) * (10 ** 18)
        if scaled < 0 or scaled > Decimal("100") * (10 ** 18):
            raise ValueError
        if scaled != scaled.to_integral_value():
            raise ValueError
    except (InvalidOperation, ValueError):
        raise ValueError("price cap must be a number from 0 to 100 with at most 18 decimals")
    MAX_MINT_PRICE_NATIVE = text
    MAX_MINT_VALUE_WEI = int(scaled)
    return text


# Separate hard ceiling for secondary-market purchases. A floor-price preview
# is never permission to spend more if the listing changes before confirmation.
MAX_BUY_PRICE_NATIVE = os.getenv("MAX_BUY_PRICE_NATIVE", "0").strip() or "0"


def _native_cap_wei(value, name):
    try:
        scaled = Decimal(str(value)) * (10 ** 18)
        if scaled < 0 or scaled > Decimal("10000") * (10 ** 18):
            raise ValueError
        if scaled != scaled.to_integral_value():
            raise ValueError
        return int(scaled)
    except (InvalidOperation, ValueError):
        raise ValueError(f"{name} must be a non-negative decimal with at most 18 decimals")


MAX_BUY_VALUE_WEI = _native_cap_wei(MAX_BUY_PRICE_NATIVE, "MAX_BUY_PRICE_NATIVE")


def set_max_buy_price_native(value):
    """Update the in-process hard purchase-value ceiling safely."""
    global MAX_BUY_PRICE_NATIVE, MAX_BUY_VALUE_WEI
    text = str(value or "").strip()
    MAX_BUY_VALUE_WEI = _native_cap_wei(text, "buy price cap")
    MAX_BUY_PRICE_NATIVE = text
    return text


# ---------------------------------------------------------------------------
# OPENSEA ENDPOINTS AND HEADERS  (verified live 2026-08-11)
# ---------------------------------------------------------------------------
# OpenSea's documented Drops API is used for both schedule and mint data.
# OPENSEA_API_KEY lives in .env and is intentionally not stored in this file.
OPENSEA_API_BASE_URL = "https://api.opensea.io/api/v2"

# A normal browser-like user agent for API requests.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


# ---------------------------------------------------------------------------
# DISCOVERY, DAILY RUNNER, AND CHAIN SUPPORT
# ---------------------------------------------------------------------------

# OpenSea chain slugs that have an EVM chain ID and an Alchemy RPC mapping.
# Set MONITORED_CHAINS to a comma-separated subset, or "all" for every entry
# below. OpenSea also lists non-EVM chains; those are intentionally skipped by
# this wallet signer instead of being misclassified as EVM drops.
CHAIN_CONFIGS = {
    "ethereum": {"chain_id": 1, "rpc_subdomain": "eth-mainnet", "native": "ETH"},
    "base": {"chain_id": 8453, "rpc_subdomain": "base-mainnet", "native": "ETH"},
    "polygon": {"chain_id": 137, "rpc_subdomain": "polygon-mainnet", "native": "POL"},
    "optimism": {"chain_id": 10, "rpc_subdomain": "opt-mainnet", "native": "ETH"},
    "arbitrum": {"chain_id": 42161, "rpc_subdomain": "arb-mainnet", "native": "ETH"},
    "robinhood": {"chain_id": 4663, "rpc_subdomain": "robinhood-mainnet", "native": "ETH"},
    "zora": {"chain_id": 7777777, "rpc_subdomain": "zora-mainnet", "native": "ETH"},
    "blast": {"chain_id": 81457, "rpc_subdomain": "blast-mainnet", "native": "ETH"},
    "avalanche": {"chain_id": 43114, "rpc_subdomain": "avax-mainnet", "native": "AVAX"},
    "unichain": {"chain_id": 130, "rpc_subdomain": "unichain-mainnet", "native": "ETH"},
    "shape": {"chain_id": 360, "rpc_subdomain": "shape-mainnet", "native": "ETH"},
}

# The default set covers the main EVM chains. Add a supported key above if your
# Alchemy account supports it.
MONITORED_CHAINS = "ethereum,base,polygon,optimism,arbitrum,robinhood"

# Discovery reads up to this many upcoming drops per chain, then fetches their
# stage details. The Telegram route lists today's stages and labels free, paid,
# and restricted entries.
DISCOVERY_WINDOW_HOURS = 24
DISCOVERY_LIMIT_PER_CHAIN = 50
# Follow at most this many OpenSea result pages per chain during one scan.
# Increase only if you intentionally want a wider, slower scan.
DISCOVERY_MAX_PAGES_PER_CHAIN = 5
# OpenSea has no exhaustive "all active today" endpoint. Merge every official
# drop-calendar feed so active/recent drops are not missed by an upcoming-only
# scan. Results are deduplicated by collection and mint stage.
DISCOVERY_DROP_TYPES = ("upcoming", "recently_minted", "featured")
# A chain-specific Telegram scan also checks OpenSea's most active collections
# and validates each one through the drop-details endpoint. This catches live
# SeaDrop mints that OpenSea has not placed in any drop-calendar feed yet.
DISCOVERY_RANKED_FALLBACK_LIMIT = 100
DISCOVERY_RANKED_FALLBACK_WORKERS = 8
DISCOVERY_PUBLIC_ONLY = True
DISCOVERY_REQUEST_DELAY_SECONDS = 0.15
# The day boundary is a fixed UTC offset so Windows and Linux VPS machines
# behave identically. 0 means UTC; set DISCOVERY_UTC_OFFSET_HOURS=1 for WAT.
DISCOVERY_UTC_OFFSET_HOURS = "0"

# Daily scheduler defaults. Live mode remains disabled until the operator sets
# ENABLE_LIVE_MINTS=true and confirms it from the authorized Telegram chat.
DAILY_SCAN_INTERVAL_SECONDS = 24 * 60 * 60
MAX_DAILY_MINTS = 5
MAX_DAILY_GAS_NATIVE = "0.05"
DAILY_STATE_FILE = "state/daily_mints.json"

# One-time schedules created from Telegram are persisted locally so a restart
# does not silently discard an armed mint. The scheduler resumes armed entries
# when the bot starts; live entries still require ENABLE_LIVE_MINTS=true.
MINT_SCHEDULES_STATE_FILE = "state/mint_schedules.json"
SCHEDULE_POLL_SECONDS = 5

# Telegram uses long polling, so a VPS only needs to keep this process online.
TELEGRAM_POLL_TIMEOUT_SECONDS = 25


def discovery_timezone():
    """Return the configured fixed-offset timezone used for daily scans."""
    raw = os.getenv("DISCOVERY_UTC_OFFSET_HOURS", str(DISCOVERY_UTC_OFFSET_HOURS)).strip()
    try:
        hours = Decimal(raw)
        if not hours.is_finite() or hours < Decimal("-24") or hours >= Decimal("24"):
            raise ValueError
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError("DISCOVERY_UTC_OFFSET_HOURS must be between -24 and 24")
    return timezone(timedelta(hours=float(hours)))


def discovery_day_bounds(timestamp=None):
    """Return ``(start_epoch, end_epoch, label)`` for the current configured day."""
    tz = discovery_timezone()
    current = (
        datetime.fromtimestamp(timestamp, tz)
        if timestamp is not None
        else datetime.now(tz)
    )
    start = current.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1) - timedelta(seconds=1)
    return int(start.timestamp()), int(end.timestamp()), start.date().isoformat()


def monitored_chain_slugs():
    """Return configured OpenSea chain slugs with duplicates removed."""
    raw = (MONITORED_CHAINS or "").strip().lower()
    if raw == "all":
        return list(CHAIN_CONFIGS)
    result = []
    for slug in raw.split(","):
        slug = slug.strip()
        if slug and slug not in result:
            result.append(slug)
    return result


def chain_config(chain_slug):
    """Return EVM chain settings or None for an unsupported OpenSea slug."""
    return CHAIN_CONFIGS.get((chain_slug or "").strip().lower())


def rpc_url_for_chain(alchemy_key, chain_id):
    """Build the Alchemy RPC URL for a configured EVM chain ID."""
    for settings in CHAIN_CONFIGS.values():
        if settings["chain_id"] == chain_id:
            return f"https://{settings['rpc_subdomain']}.g.alchemy.com/v2/{alchemy_key}"
    raise ValueError(f"chain ID {chain_id} has no configured Alchemy RPC mapping")


def chain_slug_for_id(chain_id):
    """Return the OpenSea slug for a configured EVM chain ID, if known."""
    for slug, settings in CHAIN_CONFIGS.items():
        if settings["chain_id"] == chain_id:
            return slug
    return None


try:
    _daily_gas_scaled = Decimal(MAX_DAILY_GAS_NATIVE) * (10 ** 18)
    if _daily_gas_scaled < 0 or _daily_gas_scaled != _daily_gas_scaled.to_integral_value():
        raise ValueError
    MAX_DAILY_GAS_WEI = int(_daily_gas_scaled)
except (InvalidOperation, ValueError):
    raise ValueError("MAX_DAILY_GAS_NATIVE must be a non-negative decimal with at most 18 decimals")
