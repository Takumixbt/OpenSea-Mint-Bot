"""
The wide net: public mint announcements on X, and OpenSea's own drops feed for
Robinhood Chain.

This complements the follow graph rather than replacing it. The follow graph is
early but narrow (it only sees what your curated accounts touch); this is late
but broad. Anything found here is already partly public, which is why it scores
far lower per settings.W_X_ENGAGEMENT.
"""

import re
import time
from datetime import datetime, timedelta, timezone

import httpx

from . import settings, store, xcli

# Handles mentioned in mint posts that are never the project itself.
_NOISE = settings.FOLLOW_DIFF_IGNORE | {"x", "twitter", "discord", "telegram"}

_EVIDENCE_RANK = {"mentioned": 0, "quoted": 1, "retweeted": 2}

_HANDLE_RE = re.compile(r"@([A-Za-z0-9_]{2,15})")
_ADDRESS_RE = re.compile(r"\b0x[a-fA-F0-9]{40}\b")


def _mint_relevant(text):
    """Does this post read like mint chatter? Returns (is_mint, names_the_chain)."""
    low = text.lower()
    return (any(k in low for k in settings.MINT_KEYWORDS),
            any(k in low for k in settings.CHAIN_KEYWORDS))


def _too_old(created_at, cutoff):
    if not created_at:
        return False  # undated posts are kept; dropping them loses real hits
    try:
        dt = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
    except ValueError:
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt < cutoff


def from_smart_timelines(log=print):
    """
    Read what the smart accounts are actually posting and retweeting.

    This is the primary wide net. X's search endpoint 404s from twitter-cli's
    session, but reading a specific account's timeline still works, and pointing
    it at the curated list is a better instrument than search ever was: a search
    for "free mint" returns every chain's noise, while a timeline hit already
    carries the endorsement of someone whose taste is the reason they are on
    the list.

    Retweets are the highest-value shape. X reports the ORIGINAL author of a
    retweet plus the amplifier separately, so when one of these accounts
    retweets a project announcement, the project's own handle arrives directly
    rather than having to be guessed out of @-mentions.
    """
    if not xcli.available():
        log("twitter-cli unavailable, skipping timeline scan.")
        return {}

    smart = store.load_smart_accounts()
    if not smart:
        log("No smart accounts configured, skipping timeline scan.")
        return {}

    cutoff = datetime.now(timezone.utc) - timedelta(hours=settings.TIMELINE_LOOKBACK_HOURS)
    found = {}
    scanned = 0
    failures = []

    for i, account in enumerate(smart):
        if i:
            time.sleep(settings.XCLI_PAUSE_SECONDS)
        posts, note = xcli.user_posts(account, settings.TIMELINE_POSTS_PER_ACCOUNT)
        if note:
            failures.append(f"@{account}: {note}")
            continue
        scanned += 1

        for post in posts:
            text = f"{post.get('text', '')} {post.get('quoted_text', '')}"
            is_mint, names_chain = _mint_relevant(text)
            if not is_mint:
                continue
            if _too_old(post.get("created_at"), cutoff):
                continue

            # Who is the project? These are NOT equally good evidence, and
            # treating them as if they were is how a person who happened to be
            # tagged in a mint post ends up outranking an actual live mint.
            #
            #   retweeted   the smart account amplified this account's own post
            #   quoted      they quote-tweeted it, so they were talking about it
            #   mentioned   the handle merely appeared in the text, which covers
            #               the artist, the collab, the friend, and the shill
            #
            # The kind is carried through to scoring rather than collapsed here.
            candidates = {}
            for handle in (h.lower() for h in _HANDLE_RE.findall(text)):
                candidates[handle] = "mentioned"
            if post.get("quoted_handle"):
                candidates[post["quoted_handle"].lower()] = "quoted"
            if post.get("is_retweet") and post.get("handle"):
                candidates[post["handle"].lower()] = "retweeted"
            candidates.pop(account.lower(), None)

            contracts = _ADDRESS_RE.findall(text)

            for handle, kind in candidates.items():
                if handle in _NOISE or handle in {a.lower() for a in smart}:
                    continue
                entry = found.setdefault(handle, {
                    "handle": handle,
                    "engagement_likes": 0,
                    "posts": [],
                    "mint_contract": None,
                    "smart_mentions": [],
                    "evidence": "mentioned",
                    "names_chain": False,
                })
                if account not in entry["smart_mentions"]:
                    entry["smart_mentions"].append(account)
                # Keep the strongest shape of evidence seen across all posts.
                if _EVIDENCE_RANK[kind] > _EVIDENCE_RANK[entry["evidence"]]:
                    entry["evidence"] = kind
                entry["engagement_likes"] += post.get("likes", 0)
                entry["names_chain"] = entry["names_chain"] or names_chain
                if post.get("url") and len(entry["posts"]) < 3:
                    entry["posts"].append(post["url"])
                if contracts and not entry["mint_contract"]:
                    entry["mint_contract"] = contracts[0]

    if failures:
        log(f"  {len(failures)} timeline(s) unreadable, e.g. {failures[0]}")
    log(f"Timeline scan read {scanned}/{len(smart)} account(s), "
        f"produced {len(found)} candidate handle(s).")
    return found


def from_x_search(log=print):
    """
    Run the configured searches and pull out (handle -> evidence) candidates.
    """
    if not xcli.available():
        log("twitter-cli unavailable, skipping X search.")
        return {}

    since = (datetime.now(timezone.utc)
             - timedelta(hours=settings.X_SEARCH_LOOKBACK_HOURS)).strftime("%Y-%m-%d")

    found = {}
    for query in settings.X_SEARCH_QUERIES:
        posts, note = xcli.search(query, settings.X_SEARCH_LIMIT,
                                  since=since, min_likes=settings.X_SEARCH_MIN_LIKES)
        if note:
            log(f"  search {query!r}: {note}")
            continue
        log(f"  search {query!r}: {len(posts)} post(s)")

        for post in posts:
            text = post.get("text") or ""
            likes = post.get("likes", 0)

            # The poster themselves, plus anyone they @-mention, are candidate
            # project accounts. Cheap to include, and the screen filters later.
            handles = set()
            if post.get("handle"):
                handles.add(post["handle"].lower())
            handles.update(h.lower() for h in _HANDLE_RE.findall(text))

            contracts = _ADDRESS_RE.findall(text)

            for handle in handles:
                if handle in _NOISE:
                    continue
                entry = found.setdefault(handle, {
                    "handle": handle,
                    "engagement_likes": 0,
                    "posts": [],
                    "mint_contract": None,
                })
                entry["engagement_likes"] += likes
                if post.get("url") and len(entry["posts"]) < 3:
                    entry["posts"].append(post["url"])
                if contracts and not entry["mint_contract"]:
                    entry["mint_contract"] = contracts[0]

    log(f"X search produced {len(found)} candidate handle(s).")
    return found


def from_chain_mints(w3=None, log=print, lookback=200, min_mints=3):
    """
    Ask the chain itself what is being minted right now.

    The only discovery source that depends on nothing but the RPC. X can 404 and
    OpenSea can start demanding an API key (both have), but ERC-721 transfers
    out of the zero address are the definition of a mint and cannot be hidden,
    delayed, or rate-limited away.

    This is the latest of the three signals - by the time tokens are minting,
    the mint is live - but it is the only one that is never wrong, and on a
    chain where drops run for minutes rather than seconds, arriving during the
    mint is still arriving in time. It also catches the entire category the
    other two structurally miss: projects that never announce on X at all.

    `min_mints` filters out one-off test mints and self-mints by a deployer
    checking their own contract, which are otherwise the bulk of the feed.
    """
    import os

    key = os.getenv("ALCHEMY_API_KEY")
    if not key:
        log("  no ALCHEMY_API_KEY, skipping the on-chain mint feed.")
        return {}

    url = f"https://robinhood-mainnet.g.alchemy.com/v2/{key}"
    body = {
        "jsonrpc": "2.0", "id": 1, "method": "alchemy_getAssetTransfers",
        "params": [{
            "fromAddress": "0x" + "0" * 40,   # minted, not traded
            "category": ["erc721"],
            "order": "desc",
            "maxCount": hex(lookback),
            "withMetadata": True,
        }],
    }
    try:
        with httpx.Client(timeout=30.0) as client:
            resp = client.post(url, json=body)
            resp.raise_for_status()
            payload = resp.json()
    except httpx.HTTPError as e:
        log(f"  on-chain mint feed unavailable: {type(e).__name__}")
        return {}
    if "error" in payload:
        log(f"  on-chain mint feed error: {payload['error'].get('message')}")
        return {}

    by_contract = {}
    for transfer in payload.get("result", {}).get("transfers", []):
        address = (transfer.get("rawContract") or {}).get("address")
        if not address:
            continue
        entry = by_contract.setdefault(address.lower(), {
            "mints": 0, "last_seen": None, "asset": transfer.get("asset")})
        entry["mints"] += 1
        stamp = (transfer.get("metadata") or {}).get("blockTimestamp")
        if stamp and (entry["last_seen"] is None or stamp > entry["last_seen"]):
            entry["last_seen"] = stamp

    found = {}
    skipped_infra = 0
    for address, entry in by_contract.items():
        if entry["mints"] < min_mints:
            continue
        name = entry.get("asset") or (_erc721_name(w3, address) if w3 else None)

        # DeFi plumbing mints ERC-721s constantly: Uniswap position receipts and
        # fee-beneficiary tokens alone outnumber every real drop on the chain.
        # They are not collectibles and there is nothing to snipe.
        low = (name or "").lower()
        if low and any(p in low for p in settings.INFRA_NFT_PATTERNS):
            skipped_infra += 1
            continue

        found[f"contract:{address}"] = {
            "handle": None,
            "name": name or f"Unnamed {address[:10]}",
            "url": settings.EXPLORER_ADDRESS + address,
            "mint_contract": address,
            "mint_type": "Direct Contract",
            "mint_open": entry["last_seen"],   # it is demonstrably open now
            "engagement_likes": 0,
            "posts": [],
            "live_mints": entry["mints"],
        }

    log(f"On-chain mint feed: {len(by_contract)} contract(s) minting, "
        f"{len(found)} real drop(s) past the {min_mints}-mint bar "
        f"({skipped_infra} filtered as DeFi plumbing).")
    return found


def _erc721_name(w3, address):
    """Read name() off a contract, or None. Never raises: it is a nicety."""
    try:
        from web3 import Web3
        raw = w3.eth.call({"to": Web3.to_checksum_address(address),
                           "data": "0x06fdde03"})
        text = raw[64:].decode("utf-8", "ignore").strip("\x00").strip()
        return text or None
    except Exception:
        return None


def from_opensea_drops(log=print):
    """
    Ask OpenSea's public API for live and upcoming drops on Robinhood Chain.

    Best-effort: the public API's drop coverage is inconsistent and it may
    return nothing useful. It never blocks a sweep.
    """
    url = "https://api.opensea.io/api/v2/collections"
    params = {"chain": "robinhood", "limit": 50}
    try:
        with httpx.Client(timeout=20.0, headers={"accept": "application/json"}) as client:
            resp = client.get(url, params=params)
            if resp.status_code == 401:
                log("  OpenSea public API needs an API key for collections; skipping.")
                return {}
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPError as e:
        log(f"  OpenSea drops feed unavailable: {type(e).__name__}")
        return {}

    found = {}
    for coll in data.get("collections", []):
        handle = (coll.get("twitter_username") or "").lstrip("@").lower()
        if handle in _NOISE:
            continue

        contracts = coll.get("contracts") or []
        address = contracts[0].get("address") if contracts else None
        slug = coll.get("collection")

        # Most collections on this chain publish no X handle at all, so keying
        # purely on handles would throw away the majority of the feed. Fall back
        # to the OpenSea slug as the identity. The contract address is what the
        # executor actually needs, and that is present either way.
        key = handle or (f"slug:{slug}" if slug else None)
        if not key:
            continue

        name = coll.get("name") or slug or ""
        # A collection whose "name" is just its own contract address is an
        # unnamed placeholder, not a launch worth screening.
        if name.lower().startswith("0x"):
            continue

        found[key] = {
            "handle": handle or None,
            "slug": slug,
            "name": name,
            "url": coll.get("opensea_url") or coll.get("project_url"),
            "mint_contract": address,
            "mint_type": "OpenSea Drop",
            "engagement_likes": 0,
            "posts": [],
        }

    with_handle = sum(1 for v in found.values() if v.get("handle"))
    log(f"OpenSea drops feed produced {len(found)} candidate(s) "
        f"({with_handle} with an X handle).")
    return found
