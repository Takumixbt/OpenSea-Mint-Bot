"""
The follow-graph diff. This is the actual edge in the whole system.

Speed does not win a first-come-first-served free mint: thousands of bots clear
the same block. Knowing about the mint EARLIER does win, and the earliest
public trace a project leaves is that people with good taste start following its
X account, days before any mint is announced.

X exposes no "followed at" timestamp and twitter-cli returns following-lists as
snapshots only. So the timing is recovered here: snapshot every curated smart
account's following-list on a schedule, and diff each sweep against the last.
Anything newly followed by MIN_SMART_FOLLOWS distinct smart accounts becomes a
candidate.

The first sweep produces no candidates by design - it only lays down the
baseline. Signal starts on sweep two.
"""

import os
import time
from datetime import datetime, timezone

from . import settings, store, xcli


def _snapshot_path(handle):
    safe = "".join(c for c in handle if c.isalnum() or c in "-_")
    return os.path.join(settings.FOLLOW_SNAPSHOT_DIR, f"{safe}.json")


def _load_previous(handle):
    """
    Return the cumulative set of accounts this smart account has EVER been seen
    following, or None if there is no baseline yet.

    Cumulative, not last-seen, and that distinction is load-bearing. twitter-cli
    returns a single page of at most 200 follows no matter what limit is asked
    for, so each sweep sees a window rather than the whole list. Diffing window
    against window would report anything that scrolled back into view as a brand
    new follow, and the board would fill with years-old follows. Diffing against
    the union of everything ever seen cannot do that: a handle is new exactly
    once, the first time it appears.

    It also makes the system correct without having to know how X orders that
    window. If it is most-recent-first, new follows land at the top and are
    caught. If it is a stable arbitrary page, nothing new ever appears and the
    account simply contributes no signal. Either way, never a false positive.
    """
    data = store.read_json(_snapshot_path(handle), None)
    if not isinstance(data, dict):
        return None
    known = data.get("known")
    if isinstance(known, list):
        return set(known)
    # Snapshots written before the cumulative baseline existed carry only the
    # last page. Promote it rather than discarding a baseline already paid for.
    following = data.get("following")
    if isinstance(following, list):
        return set(following)
    return None


def _save_snapshot(handle, following, known):
    store.write_json(_snapshot_path(handle), {
        "handle": handle,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "count": len(following),
        "known_count": len(known),
        "following": sorted(following),   # the latest window, for eyeballing
        "known": sorted(known),           # the cumulative baseline, for diffing
    })


def sweep(log=print):
    """
    Pull each smart account's following-list, diff against the stored snapshot,
    and return the aggregated new-follow signal.

    Returns (candidates, stats) where candidates is a dict:
        {handle: {"followed_by": [smart_handles], "count": n}}
    covering only handles meeting MIN_SMART_FOLLOWS.
    """
    store.ensure_dirs()
    smart_accounts = store.load_smart_accounts()
    if not smart_accounts:
        log("No smart accounts configured. Run radar_bootstrap.py first, or fill "
            "in radar/smart_accounts.txt by hand.")
        return {}, {"smart_accounts": 0}

    if not xcli.available():
        log("twitter-cli is not installed or not on PATH - cannot read the follow graph.")
        return {}, {"smart_accounts": len(smart_accounts), "error": "no twitter-cli"}

    log(f"Sweeping the following-lists of {len(smart_accounts)} smart account(s)...")

    new_follows = {}       # candidate handle -> set of smart accounts that added it
    baselined = 0
    swept = 0
    failed = []

    for i, smart in enumerate(smart_accounts, 1):
        current, note = xcli.following(smart, settings.FOLLOWING_FETCH_LIMIT)
        if note:
            log(f"  [{i}/{len(smart_accounts)}] @{smart}: {note}")
            failed.append(smart)
            time.sleep(settings.XCLI_PAUSE_SECONDS)
            continue

        previous = _load_previous(smart)
        if previous is None:
            # First time we have ever seen this account: record the baseline and
            # deliberately emit nothing. Treating an entire existing following
            # list as "new follows" would bury the real signal in noise.
            _save_snapshot(smart, current, current)
            baselined += 1
            log(f"  [{i}/{len(smart_accounts)}] @{smart}: baseline captured "
                f"({len(current)} follows). No diff on a first sweep.")
            time.sleep(settings.XCLI_PAUSE_SECONDS)
            continue

        added = current - previous
        # The baseline only ever grows. Unfollows are not tracked on purpose:
        # with a 200-entry window, a handle dropping out of view is far more
        # likely to mean "it scrolled off the page" than "they unfollowed", and
        # forgetting it would let it be re-reported as new later.
        _save_snapshot(smart, current, previous | current)
        swept += 1

        interesting = {h for h in added if h not in settings.FOLLOW_DIFF_IGNORE
                       and h not in smart_accounts}
        for handle in interesting:
            new_follows.setdefault(handle, set()).add(smart)

        if interesting:
            log(f"  [{i}/{len(smart_accounts)}] @{smart}: {len(interesting)} new follow(s)")
        else:
            log(f"  [{i}/{len(smart_accounts)}] @{smart}: no new follows")

        time.sleep(settings.XCLI_PAUSE_SECONDS)

    candidates = {
        handle: {"followed_by": sorted(followers), "count": len(followers)}
        for handle, followers in new_follows.items()
        if len(followers) >= settings.MIN_SMART_FOLLOWS
    }

    stats = {
        "smart_accounts": len(smart_accounts),
        "swept": swept,
        "baselined": baselined,
        "failed": failed,
        "handles_with_any_new_follow": len(new_follows),
        "candidates": len(candidates),
    }

    if baselined and not swept:
        log("Baseline sweep complete. Run the radar again later - the follow-graph "
            "signal only exists once there are two snapshots to compare.")
    else:
        log(f"Follow graph: {len(new_follows)} handle(s) newly followed, "
            f"{len(candidates)} cleared the {settings.MIN_SMART_FOLLOWS}+ bar.")

    return candidates, stats


def rank_smart_candidates(candidates):
    """Most-followed first, so the queue reads in order of conviction."""
    return sorted(candidates.items(), key=lambda kv: kv[1]["count"], reverse=True)
