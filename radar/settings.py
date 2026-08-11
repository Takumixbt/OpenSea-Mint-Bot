"""
Settings for the Free Mint Radar (the discovery half of the system).

The radar finds candidate free mints BEFORE they are public knowledge, scores
them, and writes them to a Notion watchlist. The mint bot in the parent folder
is the executor - it only ever fires on rows you have ticked "Armed".

Nothing secret lives here. Tokens and keys stay in .env.
"""

import os

# ---------------------------------------------------------------------------
# CHAIN
# ---------------------------------------------------------------------------

# Robinhood Chain. This is an Arbitrum-tech L2 that went live 2026-07-01 and is
# natively supported on OpenSea, which is why it is the specialization target:
# it is young, gas is near-zero, and the bot population is far thinner than on
# Ethereum mainnet.
CHAIN_ID = 4663
CHAIN_NAME = "Robinhood"

# Explorer base used only for building human-clickable links in Notion notes.
EXPLORER_TX = "https://explorer.rhchain.com/tx/"
EXPLORER_ADDRESS = "https://explorer.rhchain.com/address/"


# ---------------------------------------------------------------------------
# THE ALPHA: SMART-ACCOUNT FOLLOW GRAPH
# ---------------------------------------------------------------------------
# The core signal. We snapshot WHO each curated smart account follows, then diff
# against the previous snapshot. A project X account newly followed by several
# smart accounts is a leading indicator - the follows happen days before a mint
# is announced. twitter-cli gives snapshots only, so the delta is computed here.

# File holding the curated handles, one per line, '#' comments allowed.
SMART_ACCOUNTS_FILE = os.path.join(os.path.dirname(__file__), "smart_accounts.txt")

# How many accounts each smart handle's following-list is pulled at a time.
#
# Measured 2026-08-05: twitter-cli returns at most 200 regardless of what is
# asked for, because it fetches a single page and exposes no pagination. So this
# is effectively capped at 200 and the sweep sees a WINDOW, not a full list.
# smart_graph.py is built around that: it diffs against the cumulative union of
# every window ever seen, so a follow scrolling back into view cannot be
# mistaken for a new one. Left high in case pagination lands upstream.
FOLLOWING_FETCH_LIMIT = 2000

# How many distinct smart accounts must newly follow a handle before it becomes
# a candidate. 2 filters out one-off noise while staying early. Raising this
# makes you later but more certain; the asymmetric payoff argues for staying low.
MIN_SMART_FOLLOWS = 2

# Seconds to sleep between per-handle twitter-cli calls. The CLI drives a real
# logged-in session, and hammering follower endpoints is the fastest way to get
# the account limited. Do not drop this below ~2s, and never run the follow-graph
# sweep from a VPS or datacenter IP.
#
# Raised from 3.0 to 4.0 on 2026-08-05: a full sweep makes two passes over the
# list (follow graph plus timeline scan), and at 3s the last account of the
# second pass came back HTTP 429. Losing an account to a rate limit costs a
# whole sweep's signal from them, which is far more expensive than the extra
# 45 seconds this spends.
XCLI_PAUSE_SECONDS = 4.0

# Handles that are never treated as project candidates when they show up in a
# follow diff: exchanges, media, infra, and the smart accounts themselves.
FOLLOW_DIFF_IGNORE = {
    "opensea", "robinhoodcrypto", "robinhoodapp", "arbitrum", "ethereum",
    "coinbase", "binance", "magiceden", "blur_io", "zora", "manifoldxyz",
    "cointelegraph", "coindesk", "theblock__", "elonmusk", "vitalikbuterin",
}


# ---------------------------------------------------------------------------
# DISCOVERY (the wide net, complements the follow graph)
# ---------------------------------------------------------------------------

# --- Timeline scan (the primary wide-net source) ---------------------------
# X's search endpoint is dead from twitter-cli's session (it 404s), so the radar
# reads the smart accounts' own timelines instead. This turned out to be the
# better design regardless: searching all of X for "free mint" returns global
# noise from every chain, while a timeline hit is already endorsed by someone on
# the curated list. A retweet is the strongest form, because X reports the
# original author, which hands over the project handle directly.

# Posts pulled per smart account per sweep. Kept modest: the cost is one API
# call plus XCLI_PAUSE_SECONDS per handle, and the whole list is scanned.
TIMELINE_POSTS_PER_ACCOUNT = 30

# Ignore posts older than this. A mint announcement is only actionable while the
# mint is still ahead of you.
TIMELINE_LOOKBACK_HOURS = 72

# A timeline post must contain one of these to be treated as mint chatter.
# Deliberately loose - the safety screen downstream is the real filter, and a
# missed mint costs far more than a screened dud.
MINT_KEYWORDS = (
    "free mint", "freemint", "mint is live", "minting now", "mint live",
    "public mint", "allowlist", "whitelist", "wl mint", "fcfs",
    "mint out", "minted out", "gas only", "0 eth", "0eth",
    "robinhood chain", "rhchain", "robinhood",
)

# Keywords that make a post chain-relevant. A post matching MINT_KEYWORDS but
# naming no chain still counts, just at a lower weight - most mint posts never
# name their chain, and discarding them would gut the feed.
CHAIN_KEYWORDS = ("robinhood", "rhchain", "rh chain", "4663")

# X searches, kept for the day the endpoint comes back. from_x_search() degrades
# to a note when it 404s, so leaving these configured costs nothing.
X_SEARCH_QUERIES = [
    "robinhood chain free mint",
    "rhchain mint",
    "robinhood chain nft mint",
    "free mint robinhood chain opensea",
]

# How many results per query, and how far back to look.
X_SEARCH_LIMIT = 40
X_SEARCH_LOOKBACK_HOURS = 48

# Minimum engagement for an X-search hit to be worth screening. Follow-graph
# candidates bypass this entirely - the whole point is that they are early and
# therefore have no engagement yet.
X_SEARCH_MIN_LIKES = 5


# ---------------------------------------------------------------------------
# SAFETY SCREEN (this is the filter that matters)
# ---------------------------------------------------------------------------
# Deliberately NOT a quality filter. On a near-zero-gas chain the cost of
# minting a dud is cents and the cost of missing a winner is multiple ETH, so
# the screen only rejects things that look actively malicious or fake.

# An X account younger than this many days with a mint live already is a common
# throwaway-scam pattern. Flagged, not auto-rejected.
MIN_X_ACCOUNT_AGE_DAYS = 14

# A deployer wallet with fewer than this many transactions is brand new. Also
# flagged only - plenty of legitimate teams deploy from a fresh wallet.
MIN_DEPLOYER_TX_COUNT = 5

# Risk flags that force Status=Rejected and block arming, no matter the score.
# Keep this list short and genuinely fatal.
BLOCKING_FLAGS = {"prior-rug-linked"}

# ERC-721s that are plumbing rather than collectibles. DeFi protocols mint
# position and fee-receipt NFTs continuously, so without this they dominate the
# on-chain feed and crowd out real drops. Matched case-insensitively against the
# contract's name(). Substrings, so keep them specific enough not to catch a
# real collection: "position" alone would eat a project called "Positions".
INFRA_NFT_PATTERNS = (
    "positions nft", "position nft", "fee beneficiary", "uniswap",
    "liquidity", "lp token", "v3 pool", "v4 pool", "sushiswap",
    "pancake", "aerodrome", "velodrome", "curve.fi", "gauge",
    "vesting", "staking", "wrapped", "receipt",
)


# ---------------------------------------------------------------------------
# SCORING WEIGHTS
# ---------------------------------------------------------------------------
# Score exists to RANK the queue, not to gate it. Everything that is not
# blocked lands on the watchlist; the score decides what you look at first.

W_SMART_FOLLOW = 25       # per distinct smart account following (dominant term)
W_SMART_FOLLOW_CAP = 150  # ceiling so one viral project cannot swamp the board
W_SMART_MENTION = 12      # per smart account that posted or retweeted about it
W_SMART_MENTION_CAP = 72  # a loud crowd is weaker evidence than a quiet follow

# How much a mention counts for depends on its shape. A smart account
# retweeting a project's own post is real endorsement; a handle merely appearing
# in the text of someone's post is not, because that set also contains the
# artist, the collab, the friend, and whoever got tagged for reach. Weighting
# these the same put a person with no mint at the top of the board.
EVIDENCE_MULTIPLIER = {"retweeted": 1.0, "quoted": 0.6, "mentioned": 0.25}

# A row with no mint contract is unproven: there is no price, no deployer, no
# bytecode, and no proof it is a mint at all rather than a person who got
# tagged. It stays on the board because it may resolve later, but it must never
# outrank something confirmed live and free.
W_NO_CONTRACT = -45
W_CHAIN_MATCH = 15        # the post actually named Robinhood Chain
W_X_ENGAGEMENT = 0.05     # per like across matched posts, heavily discounted
W_ACCOUNT_AGE = 10        # X account older than the minimum
W_CLEAN_DEPLOYER = 15     # deployer has real prior history
W_HAS_CONTRACT = 20       # we actually resolved a mint contract to fire at
W_LIVE_MINT = 30          # tokens are coming out of it right now, on-chain fact
W_FREE_MINT = 25          # people are minting it for gas only, confirmed on-chain
W_PAID_MINT = -60         # it costs real ETH, which is not what this hunts
W_RISK_FLAG = -20         # per non-fatal risk flag

# Flags that describe the state of the evidence rather than a risk, so they must
# not be penalised. "no-x-account" already costs a row every social point in the
# scoring; charging it a risk penalty on top would double-count the same fact.
INFO_FLAGS = {"clean", "no-x-account"}


# ---------------------------------------------------------------------------
# NOTION
# ---------------------------------------------------------------------------

# Optional Notion settings. They are read at runtime so loading .env after this
# module is imported still works. Leave them blank to use the local board.
def notion_database_id():
    return os.getenv("NOTION_DATABASE_ID", "").strip()


def notion_database_url():
    return os.getenv("NOTION_DATABASE_URL", "").strip()


NOTION_VERSION = "2022-06-28"

# When the Notion settings are absent from .env the radar still works: rows are
# appended to this file instead, and can be flushed to Notion later. Never a
# silent loss.
NOTION_QUEUE_FILE = os.path.join(os.path.dirname(__file__), "state", "notion_queue.jsonl")

# The offline mirror of the watchlist. Connecting Notion requires a manual
# integration and share step, and the executor should not be dead in the water
# until that happens. So the board can also live here as plain JSON:
# arm a row by setting "armed": true, and the watcher treats it exactly like a
# ticked Notion checkbox. Same safety model either way - nothing in the codebase
# ever sets that flag, only a human does.
LOCAL_BOARD_FILE = os.path.join(os.path.dirname(__file__), "state", "board.json")

# Results the executor could not write back because Notion was unreachable.
# Replayed by `radar_scan.py --flush` alongside the queued rows.
RESULT_QUEUE_FILE = os.path.join(os.path.dirname(__file__), "state", "result_queue.jsonl")


# ---------------------------------------------------------------------------
# LOCAL STATE
# ---------------------------------------------------------------------------

STATE_DIR = os.path.join(os.path.dirname(__file__), "state")
FOLLOW_SNAPSHOT_DIR = os.path.join(STATE_DIR, "follow_snapshots")
CANDIDATES_FILE = os.path.join(STATE_DIR, "candidates.json")
SEEN_FILE = os.path.join(STATE_DIR, "seen.json")
