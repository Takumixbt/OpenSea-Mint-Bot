"""
A thin wrapper around the installed `twitter` CLI (twitter-cli).

Everything X-related in the radar goes through here so there is exactly one
place to fix when the CLI's output shape changes - which it does, because it
rides on X's private GraphQL endpoints.

Design rule: these functions NEVER raise on a bad X response. The radar runs
unattended on a schedule; one rate-limited call must degrade that single data
point, not kill the sweep. Every function returns a value plus a note.
"""

import json
import os
import shutil
import subprocess
from datetime import datetime, timezone

# The CLI is installed via pipx; on Windows it lands in ~/.local/bin.
_DEFAULT_BIN = os.path.join(os.path.expanduser("~"), ".local", "bin", "twitter.exe")


def _binary():
    found = shutil.which("twitter")
    if found:
        return found
    if os.path.exists(_DEFAULT_BIN):
        return _DEFAULT_BIN
    return None


def available():
    return _binary() is not None


def _run(args, timeout=90):
    """
    Run the CLI and parse JSON. Returns (parsed_or_None, note).
    """
    exe = _binary()
    if not exe:
        return None, "twitter-cli not found (expected `twitter` on PATH or ~/.local/bin/twitter.exe)"
    try:
        proc = subprocess.run(
            [exe] + args + ["--json"],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return None, f"twitter-cli timed out after {timeout}s"
    except OSError as e:
        return None, f"could not launch twitter-cli: {e}"

    out = (proc.stdout or "").strip()
    if not out:
        err = (proc.stderr or "").strip()
        return None, f"twitter-cli returned nothing (exit {proc.returncode}): {err[:200]}"
    try:
        return _unwrap(json.loads(out))
    except json.JSONDecodeError:
        # Some subcommands print a human line before the JSON body; salvage the
        # first well-formed JSON value rather than losing the whole call.
        for opener, closer in (("[", "]"), ("{", "}")):
            start, end = out.find(opener), out.rfind(closer)
            if start != -1 and end > start:
                try:
                    return _unwrap(json.loads(out[start:end + 1]))
                except json.JSONDecodeError:
                    continue
        return None, "twitter-cli output was not JSON"


def _unwrap(payload):
    """
    twitter-cli reports failures as a JSON envelope with ok=false rather than a
    non-zero exit, e.g.

        {"ok": false, "error": {"code": "not_found", "message": "HTTP 404"}}

    That envelope must never reach the parsers: a dict with no recognised list
    key gets treated as a single result, so an outage would silently read as
    "one post found" and quietly poison the sweep. Catch it here instead.
    """
    if isinstance(payload, dict) and payload.get("ok") is False:
        error = payload.get("error") or {}
        message = error.get("message") or error.get("code") or "unknown error"
        return None, f"twitter-cli error: {message}"
    return payload, None


def _as_list(payload):
    """
    Normalize the several shapes the CLI uses for list results into a plain list.
    """
    if payload is None:
        return []
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("data", "users", "results", "tweets", "items", "posts"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
        # A single object is a one-item list as far as callers care.
        return [payload]
    return []


def _handle_of(user):
    """Pull a screen name out of a user object whatever the CLI called the field."""
    if not isinstance(user, dict):
        return None
    for key in ("screen_name", "username", "handle", "screenName", "user_name"):
        value = user.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().lstrip("@").lower()
    nested = user.get("user") or user.get("author") or user.get("core")
    if isinstance(nested, dict):
        return _handle_of(nested)
    return None


def following(handle, limit):
    """
    Who does `handle` follow right now? Returns (set_of_handles, note).

    A snapshot only - X exposes no "followed at" timestamp, which is exactly why
    smart_graph.py has to diff consecutive snapshots to recover the timing.
    """
    payload, note = _run(["following", handle.lstrip("@"), "-n", str(limit)])
    if payload is None:
        return set(), note
    handles = {h for h in (_handle_of(u) for u in _as_list(payload)) if h}
    if not handles:
        return set(), "no accounts parsed from the following list"
    return handles, None


def followers(handle, limit):
    """Who follows `handle`? Returns (list_of_user_dicts, note)."""
    payload, note = _run(["followers", handle.lstrip("@"), "-n", str(limit)])
    if payload is None:
        return [], note
    return _as_list(payload), None


def profile(handle):
    """
    Fetch a user's profile. Returns (dict_or_None, note). The dict is normalized
    to the few fields the radar's screening actually reads.
    """
    payload, note = _run(["user", handle.lstrip("@")])
    if payload is None:
        return None, note
    raw = payload
    if isinstance(payload, dict):
        for key in ("data", "user", "result"):
            if isinstance(payload.get(key), dict):
                raw = payload[key]
                break
    if not isinstance(raw, dict):
        return None, "unexpected profile shape"

    return {
        "handle": _handle_of(raw) or handle.lstrip("@").lower(),
        "name": _first(raw, "name", "display_name", "displayName"),
        "bio": _first(raw, "description", "bio", "note"),
        "followers": _first_int(raw, "followers_count", "followers", "followersCount"),
        "following": _first_int(raw, "friends_count", "following_count", "followingCount"),
        "created_at": _first(raw, "created_at", "createdAt", "joined", "created"),
        "verified": bool(_first(raw, "verified", "is_blue_verified", "isVerified")),
        "url": _first(raw, "url", "expanded_url", "website"),
    }, None


def user_posts(handle, limit):
    """
    Read one account's own timeline. Returns (list_of_normalized_posts, note).

    This is the workhorse now that X's search endpoint is dead: instead of
    searching all of X for mint chatter and wading through global noise, the
    radar reads the timelines of the curated smart accounts directly. Narrower
    by construction, and every hit already carries the endorsement of someone on
    the list.

    Retweets matter as much as original posts here. When a smart account
    retweets a project, X reports the ORIGINAL author in `author` and the
    amplifier in `retweetedBy`, so a retweet hands over the project's handle
    directly - which is exactly the thing being hunted.
    """
    payload, note = _run(["user-posts", handle.lstrip("@"), "-n", str(limit)])
    if payload is None:
        return [], note

    posts = []
    for item in _as_list(payload):
        if not isinstance(item, dict):
            continue
        metrics = item.get("metrics") if isinstance(item.get("metrics"), dict) else {}
        quoted = item.get("quotedTweet") if isinstance(item.get("quotedTweet"), dict) else {}
        author = _handle_of(item)

        # The CLI returns no permalink, only an id and an author, so build it.
        # Without this every candidate lands on the board with no way to see the
        # post it came from, which makes the row unverifiable by hand.
        url = _first(item, "url", "permalink", "link")
        if not url and author and item.get("id"):
            url = f"https://x.com/{author}/status/{item['id']}"

        posts.append({
            "handle": author,
            "text": _first(item, "full_text", "text", "content") or "",
            "likes": _first_int(metrics, "likes", "favorite_count") or
                     _first_int(item, "favorite_count", "likes", "like_count"),
            "url": url,
            "created_at": _first(item, "createdAtISO", "created_at", "createdAt"),
            "is_retweet": bool(item.get("isRetweet")),
            "retweeted_by": (item.get("retweetedBy") or "").lstrip("@").lower() or None,
            "quoted_handle": _handle_of(quoted) if quoted else None,
            "quoted_text": (quoted.get("text") or "") if quoted else "",
            "urls": [u.get("expanded_url") or u.get("url")
                     for u in (item.get("urls") or []) if isinstance(u, dict)],
        })
    return posts, None


def search(query, limit, since=None, min_likes=None):
    """
    Search recent posts. Returns (list_of_normalized_posts, note).
    """
    args = ["search", query, "-n", str(limit), "-t", "latest", "--exclude", "retweets"]
    if since:
        args += ["--since", since]
    if min_likes:
        args += ["--min-likes", str(min_likes)]
    payload, note = _run(args)
    if payload is None:
        return [], note

    posts = []
    for item in _as_list(payload):
        if not isinstance(item, dict):
            continue
        posts.append({
            "handle": _handle_of(item),
            "text": _first(item, "full_text", "text", "content") or "",
            "likes": _first_int(item, "favorite_count", "likes", "like_count"),
            "url": _first(item, "url", "permalink", "link"),
            "created_at": _first(item, "created_at", "createdAt", "date"),
        })
    return posts, None


def _first(d, *keys):
    for k in keys:
        v = d.get(k)
        if v not in (None, ""):
            return v
    return None


def _first_int(d, *keys):
    v = _first(d, *keys)
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def account_age_days(created_at):
    """
    Turn whatever date format the CLI hands back into an age in days, or None.
    """
    if not created_at:
        return None
    text = str(created_at)
    parsers = (
        lambda s: datetime.strptime(s, "%a %b %d %H:%M:%S %z %Y"),
        lambda s: datetime.fromisoformat(s.replace("Z", "+00:00")),
    )
    for parse in parsers:
        try:
            dt = parse(text)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return max(0, (datetime.now(timezone.utc) - dt).days)
        except (ValueError, TypeError):
            continue
    return None
