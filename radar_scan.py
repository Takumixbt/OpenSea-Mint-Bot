"""
One full radar sweep. Run this on a schedule.

    python radar_scan.py                 full sweep, writes to Notion
    python radar_scan.py --no-notion     sweep and print, write nothing
    python radar_scan.py --graph-only    only the follow-graph diff (fast, quiet)
    python radar_scan.py --flush         push anything queued while offline

Order of operations:
  1. Diff the smart-account follow graph      <- the leading signal
  2. Widen with X search + OpenSea drops      <- the lagging signal
  3. Screen every candidate for malice        <- rejects scams, flags the rest
  4. Score and rank                           <- decides reading order
  5. Write to the Notion watchlist            <- you tick Armed there

Nothing here ever mints. Execution is radar_watch.py, and only on armed rows.
"""

import argparse
import sys
import time
from datetime import datetime, timezone

from dotenv import load_dotenv

from radar import (chain, discover, notion_log, osint, score, seadrop, settings,
                   smart_graph, store)


def log(msg=""):
    if msg:
        print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)
    else:
        print(flush=True)


def assemble(key, sources, smart_hits, wide_hits):
    """
    Merge everything known about one candidate into a single record.

    `key` is an X handle when we have one. When there is none, and on this chain
    most projects publish none, it is a prefixed identity instead:
    "slug:<opensea-slug>" or "contract:<address>". Anything with a colon is
    therefore not a handle.
    """
    is_handle = ":" not in key
    candidate = {
        "handle": key if is_handle else None,
        "name": None,
        "url": None,
        "source": sources[0],
        "smart_follow_count": 0,
        "smart_followers": [],
        "smart_mentions": [],
        "evidence": "mentioned",
        "names_chain": False,
        "engagement_likes": 0,
        "mint_contract": None,
        "mint_type": "Unknown",
        "mint_open": None,
        "live_mints": 0,
        "posts": [],
    }

    if key in smart_hits:
        hit = smart_hits[key]
        candidate["smart_follow_count"] = hit["count"]
        candidate["smart_followers"] = hit["followed_by"]

    if key in wide_hits:
        hit = wide_hits[key]
        for field in ("name", "url", "mint_contract", "mint_type",
                      "engagement_likes", "posts", "handle",
                      "smart_mentions", "names_chain", "evidence",
                      "mint_open", "live_mints"):
            if hit.get(field):
                candidate[field] = hit[field]

    if not candidate["name"]:
        candidate["name"] = candidate["handle"] or key.split(":", 1)[-1]
    if not candidate["url"]:
        candidate["url"] = (f"https://x.com/{candidate['handle']}"
                            if candidate["handle"] else None)

    return candidate


def build_sources(candidate, key):
    """
    Every link needed to check this row by hand, in order of usefulness.

    A row that cannot be verified is a row you cannot act on. The score says how
    interesting something is; these say why, and let you disagree in ten seconds
    instead of trusting the number.
    """
    links = []

    for url in (candidate.get("posts") or [])[:3]:
        links.append(("Post", url))

    if candidate.get("handle"):
        links.append(("X", f"https://x.com/{candidate['handle']}"))

    contract = candidate.get("mint_contract")
    if contract:
        links.append(("Explorer", settings.EXPLORER_ADDRESS + contract))
        # OpenSea indexes this chain natively, so the collection page is the
        # fastest way to see art, supply, and whether it is already sold out.
        links.append(("OpenSea", f"https://opensea.io/assets/robinhood/{contract}"))

    if candidate.get("deployer"):
        links.append(("Deployer", settings.EXPLORER_ADDRESS + candidate["deployer"]))

    if key.startswith("slug:"):
        links.append(("OpenSea", f"https://opensea.io/collection/{key.split(':', 1)[1]}"))

    # Deduplicate on URL, keeping the first (most specific) label for each.
    seen = set()
    unique = []
    for label, url in links:
        if url and url not in seen:
            seen.add(url)
            unique.append({"label": label, "url": url})
    return unique


def main():
    parser = argparse.ArgumentParser(description="Free Mint Radar sweep")
    parser.add_argument("--no-notion", action="store_true", help="Print results, write nothing")
    parser.add_argument("--graph-only", action="store_true", help="Only run the follow-graph diff")
    parser.add_argument("--skip-graph", action="store_true",
                        help="Skip the follow-graph pass. Much faster, but drops the "
                             "leading signal - for testing, not for scheduled sweeps.")
    parser.add_argument("--flush", action="store_true", help="Flush the offline queue to Notion and exit")
    parser.add_argument("--limit", type=int, default=25, help="Max candidates to screen this sweep")
    args = parser.parse_args()

    load_dotenv()
    store.ensure_dirs()

    if args.flush:
        notion_log.flush_queue(log=log)
        return

    log(f"Free Mint Radar - {settings.CHAIN_NAME} Chain (id {settings.CHAIN_ID})")
    log("")

    # --- 1. The leading signal -------------------------------------------
    if args.skip_graph:
        log("STEP 1: skipped (--skip-graph). No leading signal this sweep.")
        smart_hits, stats = {}, {"skipped": True}
    else:
        log("STEP 1: smart-account follow graph")
        smart_hits, stats = smart_graph.sweep(log=log)
    log("")

    if args.graph_only:
        if smart_hits:
            log("Newly followed by multiple smart accounts:")
            for handle, hit in smart_graph.rank_smart_candidates(smart_hits):
                log(f"    @{handle:<22} {hit['count']} follows  ({', '.join(hit['followed_by'][:4])})")
        else:
            log("No handle cleared the smart-follow threshold this sweep.")
        return

    # --- 2. The wide net --------------------------------------------------
    log("STEP 2: public discovery")
    wide_hits = {}

    def merge(hits):
        for key, hit in hits.items():
            if key in wide_hits:
                wide_hits[key].update({k: v for k, v in hit.items() if v})
            else:
                wide_hits[key] = hit

    # What the smart accounts are posting and retweeting. This is the main wide
    # net; X's own search endpoint is dead, and this is better anyway.
    timeline_hits = discover.from_smart_timelines(log=log)
    merge(timeline_hits)

    # Kept live in case X search comes back. It degrades to a note when it 404s.
    x_hits = discover.from_x_search(log=log)
    merge(x_hits)

    # Chain truth. Latest of the three, but the only one nothing can suppress,
    # and the only one that sees projects which never announce on X.
    w3, chain_note = chain.connect()
    if chain_note:
        log(f"  on-chain screening disabled: {chain_note}")
    chain_hits = discover.from_chain_mints(w3=w3, log=log)
    merge(chain_hits)

    opensea_hits = discover.from_opensea_drops(log=log)
    merge(opensea_hits)

    # A social candidate with no contract cannot be priced, screened, or minted.
    # OpenSea publishes a twitter_username for a minority of collections, which
    # is enough to rescue some of them from being permanently unprovable.
    linked = 0
    by_handle = {v["handle"]: v for v in opensea_hits.values() if v.get("handle")}
    for key, hit in wide_hits.items():
        if hit.get("mint_contract"):
            continue
        match = by_handle.get(hit.get("handle"))
        if match and match.get("mint_contract"):
            hit["mint_contract"] = match["mint_contract"]
            hit["mint_type"] = match.get("mint_type") or hit.get("mint_type")
            linked += 1
    if linked:
        log(f"  linked {linked} social candidate(s) to a contract via OpenSea.")
    log("")

    # --- Build the candidate set -----------------------------------------
    # Source order matters: the first label recorded is the one written to the
    # board, so the strongest evidence should claim the row.
    sources = {}
    for handle in smart_hits:
        sources.setdefault(handle, []).append("Follow Graph")
    for handle in timeline_hits:
        sources.setdefault(handle, []).append("Smart Timeline")
    for handle in x_hits:
        sources.setdefault(handle, []).append("X Search")
    for key in chain_hits:
        sources.setdefault(key, []).append("On-Chain Mint")
    for key in wide_hits:
        if key not in timeline_hits and key not in x_hits and key not in chain_hits:
            sources.setdefault(key, []).append("OpenSea Drops")

    if not sources:
        log("Nothing found this sweep. That is normal, especially early on.")
        return

    # Screen the strongest evidence first: the screening budget should never be
    # spent on OpenSea noise before a handle several smart accounts just
    # followed. Follows outrank posts, posts outrank a bare marketplace listing.
    def priority(key):
        hit = wide_hits.get(key, {})
        return (
            key not in smart_hits,
            -smart_hits.get(key, {}).get("count", 0),
            -len(hit.get("smart_mentions") or []),
            # A contract with tokens coming out of it right now is actionable
            # this minute, so it outranks a merely-listed collection.
            -(hit.get("live_mints") or 0),
        )

    # Contracts that are minting right now get guaranteed budget rather than
    # competing for it. They are the only rows that are actionable this minute,
    # there are never many of them, and letting a pile of handles each mentioned
    # once crowd them out is exactly backwards.
    live_now = [k for k in sources if k in chain_hits]
    rest = [k for k in sorted(sources, key=priority) if k not in chain_hits]
    ordered = live_now[:args.limit] + rest[:max(0, args.limit - len(live_now))]

    log(f"STEP 3: screening {len(ordered)} candidate(s) of {len(sources)} found")

    results = []
    for key in ordered:
        candidate = assemble(key, sources[key], smart_hits, wide_hits)
        # Pass the real handle, never the "slug:" key - the X half of the screen
        # would otherwise go looking for an account named "slug:something".
        screen = osint.screen_project(candidate["handle"],
                                     candidate.get("mint_contract"), w3=w3, log=log)
        candidate.update({
            "flags": screen["flags"],
            "blocked": screen["blocked"],
            "notes": screen["notes"],
            "deployer": screen["deployer"],
            "account_age": screen["account_age"],
        })

        # SeaDrop publishes its schedule on-chain, so for those collections the
        # exact open second and the real price are knowable in advance rather
        # than inferred after the fact. Prefer it whenever it is available.
        drop = seadrop.public_drop(w3, candidate.get("mint_contract"))
        if drop:
            candidate["mint_type"] = "OpenSea Drop"
            candidate["mint_open"] = datetime.fromtimestamp(
                drop["start"], tz=timezone.utc).isoformat()
            candidate["mint_price_wei"] = drop["price_wei"]
            candidate["mint_price_eth"] = drop["price_eth"]
            candidate["drop_state"] = drop["state"]
            candidate["notes"] += "\n" + seadrop.describe(drop)
            if drop["price_wei"] > 0:
                candidate["flags"] = sorted(set(candidate["flags"]) | {"paid-mint"})

        # What does it actually cost? Read it off real transactions. A "free
        # mint" that charges 0.013 ETH is the single most expensive thing this
        # board could get wrong, and the executor's value cap would silently
        # refuse it later anyway - better to say so on the row now.
        # Skipped when SeaDrop already gave an authoritative answer above.
        if candidate.get("mint_contract") and not drop:
            from radar import mint_direct
            low, high, seen = mint_direct.observed_mint_price(candidate["mint_contract"])
            if seen:
                candidate["mint_price_wei"] = high
                candidate["mint_price_eth"] = high / 1e18
                if high == 0:
                    candidate["notes"] += (
                        f"\nFREE: all {seen} recent mints paid 0 ETH, gas only.")
                else:
                    candidate["flags"] = sorted(set(candidate["flags"]) | {"paid-mint"})
                    candidate["notes"] += (
                        f"\nPAID: recent mints cost {low / 1e18:.5f} to "
                        f"{high / 1e18:.5f} ETH. This is not a free mint, and the "
                        f"executor will refuse it while MAX_MINT_PRICE_NATIVE is 0.")

        # If we have a contract, confirm it actually exposes a callable mint.
        if candidate.get("mint_contract") and w3 is not None:
            from radar import mint_direct
            import os
            wallet = os.getenv("WALLET_ADDRESS")
            if wallet and mint_direct.probe_readonly(w3, candidate["mint_contract"], wallet, 1):
                candidate["mint_type"] = "Direct Contract"
                candidate["notes"] += "\nContract exposes a callable public mint right now."

        candidate["sources"] = build_sources(candidate, key)

        value, breakdown = score.score_candidate(candidate)
        candidate["score"] = value
        candidate["status"] = score.status_for(candidate, value)
        candidate["breakdown"] = breakdown
        results.append(candidate)

    results.sort(key=lambda c: c["score"], reverse=True)
    log("")

    # --- 4 & 5. Report and log -------------------------------------------
    log("STEP 4: ranked results")
    for candidate in results:
        marker = "BLOCKED" if candidate["blocked"] else f"{candidate['score']:>6.1f}"
        # Most collections on this chain publish no X handle, so the label falls
        # back to the collection name rather than printing an empty column.
        label = f"@{candidate['handle']}" if candidate["handle"] else candidate["name"]
        evidence = []
        if candidate["smart_follow_count"]:
            evidence.append(f"{candidate['smart_follow_count']} follow(s)")
        if candidate["smart_mentions"]:
            evidence.append(f"{len(candidate['smart_mentions'])} mention(s)")
        log(f"  {marker}  {label[:24]:<24} {', '.join(evidence) or '-':<22} "
            f"[{', '.join(candidate['flags'])}]")
    log("")

    if args.no_notion:
        log("--no-notion set, nothing written.")
        return

    log("STEP 5: writing the watchlist to Notion")
    written = 0
    for candidate in results:
        if notion_log.upsert(candidate, log=log) or not notion_log.enabled():
            written += 1
    log("")
    log(f"Sweep complete. {written} row(s) recorded.")
    log(f"Watchlist: {settings.NOTION_DATABASE_URL}")
    log("Tick 'Armed' on anything you want minted automatically, then leave "
        "radar_watch.py running.")

    store.write_json(settings.CANDIDATES_FILE, {
        "swept_at": datetime.now(timezone.utc).isoformat(),
        "stats": stats,
        "candidates": results,
    })


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
        sys.exit(130)
