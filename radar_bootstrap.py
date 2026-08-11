"""
Build the smart-account list empirically, instead of guessing at famous names.

    python radar_bootstrap.py --seed-from stonkbrokers --top 60

The logic: take a free mint that demonstrably worked on this chain, pull the
accounts that follow it, and keep the ones that look like real early movers.
Anyone who was following a winner is, by revealed preference, someone whose
follows are worth watching. A hand-picked celebrity list has no such evidence
behind it.

Re-run this after every mint you win or miss, seeding from the newest winner.
The list should keep getting sharper, not sit frozen.
"""

import argparse
import sys

from dotenv import load_dotenv

from radar import settings, store, xcli


def log(msg):
    print(msg, flush=True)


def main():
    parser = argparse.ArgumentParser(description="Seed the smart-account list from a proven winner")
    parser.add_argument("--seed-from", required=True,
                        help="X handle of a collection whose mint already paid off, e.g. stonkbrokers")
    parser.add_argument("--top", type=int, default=60,
                        help="How many handles to keep (40-80 is the sweet spot)")
    parser.add_argument("--min-followers", type=int, default=500,
                        help="Drop accounts smaller than this - they are usually bots or dead")
    parser.add_argument("--max-following", type=int, default=5000,
                        help="Drop accounts that follow everything; they generate noise, not signal")
    parser.add_argument("--append", action="store_true",
                        help="Add to the existing list instead of replacing it")
    args = parser.parse_args()

    load_dotenv()

    if not xcli.available():
        log("twitter-cli is not installed or not on PATH. Install it first "
            "(pipx install twitter-cli) and make sure `twitter user someone` works.")
        sys.exit(1)

    seed = args.seed_from.lstrip("@")

    # Preferred source: who FOLLOWS a proven winner. That is the strongest
    # evidence of an early mover.
    log(f"Pulling followers of @{seed}...")
    raw, note = xcli.followers(seed, max(args.top * 40, 2000))

    if note:
        # X's follower endpoint breaks periodically (twitter-cli rides its
        # private GraphQL). Fall back to who the project FOLLOWS: a weaker but
        # genuinely useful proxy, since teams follow their collabs and the
        # ecosystem players around them. Say plainly which one was used.
        log(f"  follower list unavailable: {note}")
        log(f"  falling back to who @{seed} follows (weaker signal, but live)")
        log("  if you want the stronger source, try `pipx upgrade twitter-cli` "
            "and re-run - these 404s are usually a stale endpoint.")
        handles, note2 = xcli.following(seed, max(args.top * 40, 2000))
        if note2:
            log(f"Could not read the following list either: {note2}")
            log("Both X endpoints are down. Wait, upgrade twitter-cli, or fill in "
                "radar/smart_accounts.txt by hand.")
            sys.exit(1)
        # following() returns bare handles, so per-account stats are not
        # available; keep them all and let the sweep prune by behaviour.
        raw = [{"screen_name": h} for h in handles]
        log(f"Got {len(raw)} handle(s) from the following list.")
        _write_list(raw, seed, args, log, filtered=False)
        return

    log(f"Got {len(raw)} follower record(s). Filtering...")

    scored = []
    for user in raw:
        handle = xcli._handle_of(user)
        if not handle or handle in settings.FOLLOW_DIFF_IGNORE:
            continue
        followers = xcli._first_int(user, "followers_count", "followers", "followersCount")
        following = xcli._first_int(user, "friends_count", "following_count", "followingCount")

        if followers < args.min_followers:
            continue
        if following and following > args.max_following:
            continue

        # Prefer accounts that are followed far more than they follow. A high
        # ratio means people opted in to their taste, which is exactly the
        # property that makes their follows informative.
        ratio = followers / max(following, 1)
        scored.append((ratio, followers, handle))

    scored.sort(reverse=True)
    picked = [h for _, _, h in scored[:args.top]]

    if not picked:
        log("Nothing survived the filters. Loosen --min-followers, or check that "
            "the follower pull actually returned user objects.")
        sys.exit(1)

    _write_list(picked, seed, args, log, filtered=True, scored=scored, pool=len(raw))


def _write_list(picked, seed, args, log, filtered, scored=None, pool=None):
    """
    Write the curated list out, preserving the explanatory header.

    `picked` is either a list of handles or a list of user dicts, depending on
    which X endpoint was available.
    """
    import datetime

    handles = []
    for item in picked:
        handles.append(item if isinstance(item, str) else (xcli._handle_of(item) or ""))
    handles = [h for h in handles if h and h not in settings.FOLLOW_DIFF_IGNORE]
    handles = list(dict.fromkeys(handles))[:args.top]

    if not handles:
        log("Nothing survived the filters. Loosen --min-followers, or check that "
            "the pull actually returned accounts.")
        sys.exit(1)

    existing = store.load_smart_accounts() if args.append else []
    combined = list(dict.fromkeys(existing + handles))

    source = (f"followers of @{seed}" if filtered
              else f"accounts @{seed} follows (fallback: follower list was unavailable)")

    with open(settings.SMART_ACCOUNTS_FILE, "w", encoding="utf-8") as f:
        f.write(_read_header())
        f.write(f"\n# Seeded from {source} on {datetime.date.today().isoformat()}"
                f" ({len(handles)} kept{f' from {pool}' if pool else ''}).\n\n")
        for handle in combined:
            f.write(handle + "\n")

    log(f"Wrote {len(combined)} handle(s) to {settings.SMART_ACCOUNTS_FILE}")
    if scored:
        log("")
        log("Top of the list:")
        for ratio, followers, handle in scored[:10]:
            log(f"    @{handle:<20} {followers:>9,} followers   ratio {ratio:.1f}")
    log("")
    log("Review that list before trusting it - prune anyone who follows hundreds "
        "of accounts a week, they produce noise rather than signal.")
    log("")
    log("Next: run `python radar_scan.py` twice, a few hours apart. The first run "
        "only captures a baseline; the follow-graph signal appears on the second.")


def _read_header():
    """Keep the explanatory comment block at the top of the file across re-seeds."""
    try:
        with open(settings.SMART_ACCOUNTS_FILE, encoding="utf-8") as f:
            lines = []
            for line in f:
                if line.startswith("#") or not line.strip():
                    lines.append(line)
                else:
                    break
            # Drop any previous "Seeded from" note; a fresh one is appended.
            return "".join(l for l in lines if "Seeded from" not in l).rstrip() + "\n"
    except OSError:
        return "# Curated smart accounts, one X handle per line.\n"


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
        sys.exit(130)
