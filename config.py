"""
All the non-secret settings for the bot live here. You can safely open this
file, change the values, and save it. Nothing secret is in this file - your
private key and RPC url live in the separate .env file.

Each setting has a plain-English comment saying what it does.
"""

from decimal import Decimal, InvalidOperation
from urllib.parse import unquote, urlsplit

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

# How many tokens to mint in the single transaction. Free drops usually allow 1.
MINT_QUANTITY = 1

# Which chain this drop is on. Check the collection's OpenSea page - the
# chain is shown right there. Change this per drop; everything else (the RPC
# connection) follows automatically from CHAIN_RPC_SUBDOMAINS below, using
# the single Alchemy key in your .env.
#   1 = Ethereum, 8453 = Base, 137 = Polygon, 10 = Optimism,
#   42161 = Arbitrum, 4663 = Robinhood Chain
TARGET_CHAIN_ID = 8453


# ---------------------------------------------------------------------------
# TIMING (mirrors the approach in the article the bot is based on)
# ---------------------------------------------------------------------------

# How often (in seconds) to re-check the drop's schedule while we wait for it
# to get close. A slow, polite poll - we are just watching the clock here.
SCHEDULE_POLL_SECONDS = 15

# How many seconds BEFORE the mint opens to start "warming up": opening the
# network connections, pre-fetching the wallet's transaction number, and
# confirming the chain id, so none of that slow work happens during the race.
WARMUP_LEAD_SECONDS = 5

# How many seconds BEFORE the mint opens to start hammering OpenSea for the
# real mint instructions (the salt + signature calldata). OpenSea only hands
# these out right at open time, so we start asking a moment early and retry
# fast until it answers.
FIRE_LEAD_SECONDS = 1.5

# When in the fast retry loop, how long (in seconds) to wait between attempts.
# 0.15 = about 7 tries per second. Lower is more aggressive.
FIRE_RETRY_SECONDS = 0.15

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
MAX_MINT_PRICE_NATIVE = "0"

try:
    _mint_price_scaled = Decimal(MAX_MINT_PRICE_NATIVE) * (10 ** 18)
    if _mint_price_scaled < 0 or _mint_price_scaled != _mint_price_scaled.to_integral_value():
        raise ValueError
    MAX_MINT_VALUE_WEI = int(_mint_price_scaled)
except (InvalidOperation, ValueError):
    raise ValueError(
        "MAX_MINT_PRICE_NATIVE must be a non-negative decimal with at most 18 decimals"
    )


# ---------------------------------------------------------------------------
# OPENSEA ENDPOINTS AND HEADERS  (verified live 2026-08-11)
# ---------------------------------------------------------------------------
# recon_check.py re-checks that these are still what OpenSea uses. If OpenSea
# changes something, recon_check.py will tell you which line here to update.

# The INTERNAL GraphQL endpoint the real OpenSea website uses for minting.
# (The public api.opensea.io one does NOT expose the mint calldata.)
GQL_ENDPOINT = "https://gql.opensea.io/graphql"

# The website currently sends SIWE login requests to its same-origin internal
# API. The old gql.opensea.io/auth/* route returns 404 and must not be used.
AUTH_NONCE_URL = "https://opensea.io/__api/auth/siwe/nonce"
AUTH_VERIFY_URL = "https://opensea.io/__api/auth/siwe/verify"

# The website OpenSea's SIWE message is scoped to. The trailing slash matters -
# the signed message must match byte-for-byte or OpenSea rejects the login.
OPENSEA_DOMAIN = "opensea.io"
OPENSEA_URI = "https://opensea.io/"


def opensea_signin_uri():
    """Return the OpenSea page URI used in the SIWE message."""
    try:
        slug = target_collection_slug()
    except ValueError:
        slug = None
    if slug:
        return f"https://opensea.io/collection/{slug}/overview"
    return OPENSEA_URI

# Extra header OpenSea requires on every GraphQL call. Without it, even a
# correctly-logged-in request is rejected. Confirmed still present in the
# live site bundle on 2026-07-18.
APP_ID_HEADER = "os2-web"

# Browser-like headers so our requests look like the real website's.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# Where the saved login session (cookies) is stored so you don't re-sign every
# run. OpenSea sessions last about 3.5 days. This file is gitignored.
SESSION_FILE = "session.json"


# ---------------------------------------------------------------------------
# CHAIN RPC LOOKUP  (one Alchemy key, any chain)
# ---------------------------------------------------------------------------
# Alchemy gives one API key that works across every chain it supports - you
# don't need a separate key or app per chain. This maps a chain ID to the
# subdomain Alchemy uses for it, so the bot builds the right RPC url on its
# own from TARGET_CHAIN_ID above + ALCHEMY_API_KEY in your .env.
#
# To mint on a chain not listed here: search "Alchemy <chain name> RPC",
# open the result, and copy the part of the URL between "https://" and
# ".g.alchemy.com" - that's the subdomain. Add a line below with its chain ID.
CHAIN_RPC_SUBDOMAINS = {
    1: "eth-mainnet",           # Ethereum
    8453: "base-mainnet",       # Base
    137: "polygon-mainnet",     # Polygon
    10: "opt-mainnet",          # Optimism
    42161: "arb-mainnet",       # Arbitrum
    4663: "robinhood-mainnet",  # Robinhood Chain
}
