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
        raw = parts[1]

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
# TIMING
# ---------------------------------------------------------------------------

# How often (in seconds) to re-check the drop's schedule while we wait for it
# to get close. A slow, polite poll - we are just watching the clock here.
SCHEDULE_POLL_SECONDS = 5

# How many seconds BEFORE the mint opens to start "warming up": re-reading the
# drop, opening the network connections, pre-fetching the wallet's transaction
# number and fees, reading the SeaDrop stage, and signing, so none of that slow
# work happens during the race.
#
# This has to be longer than the preparation actually takes, or the
# transaction is signed after the opening and the mint is late. Measured on
# Base from a home connection the full sequence took about 13 seconds; on a
# VPS near the RPC provider it is closer to 1-2. The default leaves room for
# the slow case because arming early costs nothing: execute() waits for the
# exact opening instant before it broadcasts.
#
# The trade-off of a longer lead is that gas fees are read that much earlier.
# The fee formula already carries 2x base-fee headroom and MAX_FEE_CAP_GWEI
# still bounds the worst case, so this is safe; lower it only if you are on a
# fast link and want the freshest possible fee reading.
try:
    WARMUP_LEAD_SECONDS = float(os.getenv("WARMUP_LEAD_SECONDS", "30"))
except (TypeError, ValueError):
    raise ValueError("WARMUP_LEAD_SECONDS must be a number of seconds")
if not 1 <= WARMUP_LEAD_SECONDS <= 600:
    raise ValueError("WARMUP_LEAD_SECONDS must be between 1 and 600")

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

# Public SeaDrop stages can be prepared and signed directly from on-chain
# configuration.  This is the fast path used by the Telegram bot when the
# collection exposes a compatible public SeaDrop stage.  Restricted stages,
# allowlists, and custom contracts continue through their existing adapters.
DIRECT_PUBLIC_SEADROP = os.getenv("DIRECT_PUBLIC_SEADROP", "true").strip().lower() in {
    "1", "true", "yes", "on"
}
try:
    DIRECT_SEADROP_START_TOLERANCE_SECONDS = int(
        os.getenv("DIRECT_SEADROP_START_TOLERANCE_SECONDS", "120")
    )
except (TypeError, ValueError):
    raise ValueError("DIRECT_SEADROP_START_TOLERANCE_SECONDS must be an integer")
if DIRECT_SEADROP_START_TOLERANCE_SECONDS < 0:
    raise ValueError("DIRECT_SEADROP_START_TOLERANCE_SECONDS cannot be negative")


# ---------------------------------------------------------------------------
# DISCOVERY, DAILY RUNNER, AND CHAIN SUPPORT
# ---------------------------------------------------------------------------

# OpenSea mainnet chain slugs that can be signed by the configured EVM wallet.
# The list mirrors OpenSea's current /chains response. Solana and Hyperliquid
# (the non-EVM exchange chain, distinct from HyperEVM) are intentionally absent.
# Most routes use Alchemy's universal-key endpoint; the three networks Alchemy
# does not currently expose use their official public RPC by default. Every
# network can be overridden with MINT_RPC_URL_<CHAIN> in .env.
CHAIN_CONFIGS = {
    "ethereum": {"chain_id": 1, "rpc_subdomain": "eth-mainnet", "native": "ETH", "explorer": "https://etherscan.io"},
    "optimism": {"chain_id": 10, "rpc_subdomain": "opt-mainnet", "native": "ETH", "explorer": "https://optimistic.etherscan.io"},
    "unichain": {"chain_id": 130, "rpc_subdomain": "unichain-mainnet", "native": "ETH", "explorer": "https://uniscan.xyz"},
    "polygon": {"chain_id": 137, "rpc_subdomain": "polygon-mainnet", "native": "POL", "explorer": "https://polygonscan.com"},
    "monad": {"chain_id": 143, "rpc_subdomain": "monad-mainnet", "native": "MON", "explorer": "https://monadscan.com"},
    "shape": {"chain_id": 360, "rpc_subdomain": "shape-mainnet", "native": "ETH", "explorer": "https://shapescan.xyz"},
    "flow": {"chain_id": 747, "rpc_subdomain": "flow-mainnet", "native": "FLOW", "explorer": "https://evm.flowscan.io"},
    "stablechain": {"chain_id": 988, "rpc_subdomain": "stable-mainnet", "native": "USDT0", "explorer": "https://stablescan.xyz"},
    "hyperevm": {"chain_id": 999, "rpc_subdomain": "hyperliquid-mainnet", "native": "HYPE", "explorer": "https://hyperevmscan.io"},
    "sei": {"chain_id": 1329, "rpc_subdomain": "sei-mainnet", "native": "SEI", "explorer": "https://seiscan.io"},
    "soneium": {"chain_id": 1868, "rpc_subdomain": "soneium-mainnet", "native": "ETH", "explorer": "https://soneium.blockscout.com"},
    "ronin": {"chain_id": 2020, "rpc_subdomain": "ronin-mainnet", "native": "RON", "explorer": "https://app.roninchain.com"},
    "abstract": {"chain_id": 2741, "rpc_subdomain": "abstract-mainnet", "native": "ETH", "explorer": "https://abscan.org"},
    "megaeth": {"chain_id": 4326, "rpc_subdomain": "megaeth-mainnet", "native": "ETH", "explorer": "https://mega.etherscan.io"},
    "robinhood": {"chain_id": 4663, "rpc_subdomain": "robinhood-mainnet", "native": "ETH", "explorer": "https://robinhoodchain.blockscout.com"},
    "somnia": {"chain_id": 5031, "rpc_url": "https://api.infra.mainnet.somnia.network", "native": "SOMI", "explorer": "https://explorer.somnia.network"},
    "b3": {"chain_id": 8333, "rpc_url": "https://mainnet-rpc.b3.fun", "native": "ETH", "explorer": "https://explorer.b3.fun"},
    "base": {"chain_id": 8453, "rpc_subdomain": "base-mainnet", "native": "ETH", "explorer": "https://basescan.org"},
    "ape_chain": {"chain_id": 33139, "rpc_subdomain": "apechain-mainnet", "native": "APE", "explorer": "https://apescan.io"},
    "arbitrum": {"chain_id": 42161, "rpc_subdomain": "arb-mainnet", "native": "ETH", "explorer": "https://arbiscan.io"},
    "avalanche": {"chain_id": 43114, "rpc_subdomain": "avax-mainnet", "native": "AVAX", "explorer": "https://snowtrace.io"},
    "gunzilla": {
        "chain_id": 43419,
        "rpc_url": "https://subnets.avax.network/gunzilla/mainnet/rpc",
        "native": "GUN",
        "explorer": "https://gunzscan.io",
    },
    "ink": {"chain_id": 57073, "rpc_subdomain": "ink-mainnet", "native": "ETH", "explorer": "https://explorer.inkonchain.com"},
    "animechain": {"chain_id": 69000, "rpc_subdomain": "anime-mainnet", "native": "ANIME", "explorer": "https://explorer.anime.xyz"},
    "bera_chain": {"chain_id": 80094, "rpc_subdomain": "berachain-mainnet", "native": "BERA", "explorer": "https://berascan.com"},
    "blast": {"chain_id": 81457, "rpc_subdomain": "blast-mainnet", "native": "ETH", "explorer": "https://blastscan.io"},
    "zora": {"chain_id": 7777777, "rpc_subdomain": "zora-mainnet", "native": "ETH", "explorer": "https://explorer.zora.energy"},
}

# Telegram inline buttons render emoji but not images, so each network gets one
# glyph. Every glyph is distinct so networks stay tellable apart at a glance.
# These are labels only: the signer still resolves everything from
# CHAIN_CONFIGS above.
CHAIN_ICONS = {
    "ethereum": "\u27e0",
    "optimism": "\U0001f534",
    "unichain": "\U0001f984",
    "polygon": "\U0001f7e3",
    "monad": "\U0001f7ea",
    "shape": "\U0001f537",
    "flow": "\U0001f30a",
    "stablechain": "\U0001f4b5",
    "hyperevm": "\U0001f7e2",
    "sei": "\U0001f33e",
    "soneium": "\u26ab",
    "ronin": "\u2694\ufe0f",
    "abstract": "\U0001f7e9",
    "megaeth": "\u26a1",
    "robinhood": "\U0001fab6",
    "somnia": "\U0001f31b",
    "b3": "\U0001f3ae",
    "base": "\U0001f535",
    "ape_chain": "\U0001f9a7",
    "arbitrum": "\U0001f30c",
    "avalanche": "\U0001f3d4\ufe0f",
    "gunzilla": "\U0001f996",
    "ink": "\U0001f5a4",
    "animechain": "\U0001f338",
    "bera_chain": "\U0001f43b",
    "blast": "\U0001f7e1",
    "zora": "\U0001f7e0",
}
DEFAULT_CHAIN_ICON = "\u26d3\ufe0f"

# Title-casing a slug gets most networks right but mangles the few that carry
# internal capitals or no space at all.
CHAIN_DISPLAY_NAMES = {
    "hyperevm": "HyperEVM",
    "megaeth": "MegaETH",
    "ape_chain": "ApeChain",
    "bera_chain": "Berachain",
    "animechain": "AnimeChain",
    "stablechain": "Stable",
    "b3": "B3",
    "sei": "Sei",
}


def chain_icon(chain_slug):
    """Return the display glyph for an OpenSea network slug."""
    return CHAIN_ICONS.get((chain_slug or "").strip().lower(), DEFAULT_CHAIN_ICON)


def chain_label(chain_slug):
    """Return the human network name used in Telegram text and buttons."""
    slug = (chain_slug or "unknown").strip().lower()
    override = CHAIN_DISPLAY_NAMES.get(slug)
    if override:
        return override
    return slug.replace("_", " ").replace("-", " ").title()


CHAIN_ALIASES = {
    "eth": "ethereum",
    "ether": "ethereum",
    "ethereum": "ethereum",
    "mainnet": "ethereum",
    "base": "base",
    "op": "optimism",
    "opt": "optimism",
    "optimism": "optimism",
    "arb": "arbitrum",
    "arbitrum": "arbitrum",
    "matic": "polygon",
    "poly": "polygon",
    "polygon": "polygon",
    "avax": "avalanche",
    "avalanche": "avalanche",
    "ape": "ape_chain",
    "apechain": "ape_chain",
    "ape_chain": "ape_chain",
    "bera": "bera_chain",
    "berachain": "bera_chain",
    "bera_chain": "bera_chain",
    "hyper": "hyperevm",
    "hyperevm": "hyperevm",
    "anime": "animechain",
    "animechain": "animechain",
    "mega": "megaeth",
    "megaeth": "megaeth",
    "stable": "stablechain",
    "stablechain": "stablechain",
    "gun": "gunzilla",
    "gunzilla": "gunzilla",
    "uni": "unichain",
    "unichain": "unichain",
}


def resolve_chain_slug(value):
    """Map ETH, Base, ethereum, etc. to a CHAIN_CONFIGS slug, or ``all``."""
    raw = str(value or "").strip().lower()
    if not raw:
        return None
    collapsed = raw.replace(" ", "_").replace("-", "_")
    if collapsed in {"all", "*", "every"}:
        return "all"
    if collapsed in CHAIN_CONFIGS:
        return collapsed
    if collapsed in CHAIN_ALIASES:
        return CHAIN_ALIASES[collapsed]
    for slug, label in CHAIN_DISPLAY_NAMES.items():
        if label.lower().replace(" ", "_") == collapsed:
            return slug
    for slug in CHAIN_CONFIGS:
        if chain_label(slug).lower().replace(" ", "_") == collapsed:
            return slug
    return None


# A blank MONITORED_CHAINS value also falls back to this setting. "all" makes
# /scan cover every OpenSea EVM drop instead of silently omitting newer chains.
MONITORED_CHAINS = "all"

# Discovery requests this many OpenSea drops per API page. The Telegram route
# lists live stages plus stages opening today, including free, paid, and
# restricted entries.
# How far ahead /scan looks, as a rolling window from right now. This is not
# anchored to midnight: a drop opening in four hours is equally interesting at
# 09:00 and at 23:00. Widen it with DISCOVERY_WINDOW_HOURS in .env; OpenSea
# publishes few drops more than a couple of days out, so 24 covers most of the
# catalogue and 72 covers nearly all of it.
try:
    DISCOVERY_WINDOW_HOURS = float(os.getenv("DISCOVERY_WINDOW_HOURS", "24"))
except (TypeError, ValueError):
    raise ValueError("DISCOVERY_WINDOW_HOURS must be a number of hours")
if not 1 <= DISCOVERY_WINDOW_HOURS <= 24 * 90:
    raise ValueError("DISCOVERY_WINDOW_HOURS must be between 1 and 2160")

# Floor applied when a caller still asks for a day-anchored scan, so an
# evening scan cannot collapse to a few minutes of look-ahead.
DISCOVERY_MIN_WINDOW_HOURS = 12
# The global /drops cursor already covers every network, so the merged calendar
# is cached for this long and filtered locally. Scanning a second network then
# costs no extra OpenSea requests.
DISCOVERY_CALENDAR_TTL_SECONDS = 90
DISCOVERY_LIMIT_PER_CHAIN = 100
# Follow every cursor returned by OpenSea. Set a positive value only as an
# emergency ceiling; 0 means no artificial page limit.
DISCOVERY_MAX_PAGES_PER_CHAIN = 0
# OpenSea exposes its mintable collections through three drop feeds. Merge all
# three and exhaust every page so /scan is not a featured/top-project sample.
DISCOVERY_DROP_TYPES = ("upcoming", "recently_minted", "featured")
# Legacy knobs retained for configuration compatibility. Ranked/trending
# collections are trading data and are no longer mixed into mint discovery.
DISCOVERY_RANKED_FALLBACK_LIMIT = 100
DISCOVERY_RANKED_FALLBACK_WORKERS = 8
DISCOVERY_DETAIL_WORKERS = 16
DISCOVERY_PUBLIC_ONLY = True
DISCOVERY_REQUEST_DELAY_SECONDS = 0.15
# The day boundary is a fixed UTC offset so Windows and Linux VPS machines
# behave identically. 0 means UTC; set DISCOVERY_UTC_OFFSET_HOURS=1 for WAT.
DISCOVERY_UTC_OFFSET_HOURS = "0"

# Daily scheduler defaults. Live mode remains disabled until the operator sets
# ENABLE_LIVE_MINTS=true and confirms it from the CLI or the authorized Telegram chat.
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
    raw = os.getenv("MONITORED_CHAINS", "").strip().lower()
    if not raw:
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
    """Return the configured primary RPC URL for an OpenSea EVM chain."""
    for slug, settings in CHAIN_CONFIGS.items():
        if settings["chain_id"] != chain_id:
            continue
        override = os.getenv(f"MINT_RPC_URL_{slug.upper()}", "").strip()
        if override.startswith(("http://", "https://")):
            return override
        subdomain = str(settings.get("rpc_subdomain") or "").strip()
        if subdomain:
            if not str(alchemy_key or "").strip():
                raise ValueError(f"ALCHEMY_API_KEY is required for {slug}")
            return f"https://{subdomain}.g.alchemy.com/v2/{alchemy_key}"
        public_rpc = str(settings.get("rpc_url") or "").strip()
        if public_rpc.startswith(("http://", "https://")):
            return public_rpc
        raise ValueError(f"chain ID {chain_id} has no configured RPC endpoint")
    raise ValueError(f"chain ID {chain_id} has no configured RPC mapping")


def rpc_urls_for_chain(alchemy_key, chain_id):
    """Return the primary RPC plus optional broadcast endpoints.

    ``MINT_RPC_URLS_<CHAIN>`` (or the generic ``MINT_RPC_URLS``) may contain
    comma-separated HTTP(S) endpoints. The chain's primary endpoint remains
    first for reads and transaction preparation; extra endpoints are used to
    fan out an identical signed transaction at launch.
    """
    primary = rpc_url_for_chain(alchemy_key, chain_id)
    slug = chain_slug_for_id(chain_id)
    configured = ""
    if slug:
        configured = os.getenv(f"MINT_RPC_URLS_{slug.upper()}", "").strip()
    if not configured:
        configured = os.getenv("MINT_RPC_URLS", "").strip()

    urls = [primary]
    for value in configured.split(","):
        value = value.strip()
        if not value or value in urls:
            continue
        if value.startswith(("http://", "https://")):
            urls.append(value)
    return urls


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
