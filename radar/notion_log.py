"""
Reads and writes the Notion watchlist.

Two-way on purpose:
  - the radar WRITES candidates it discovers,
  - the watcher READS back which rows you ticked "Armed".

That tick is the whole safety model. Notion is the control surface: the radar
proposes, you authorize, the executor fires.

Auth uses a Notion internal integration token and a user-owned database ID from
.env. If either is missing, writes go to a local queue file instead of being
lost, so an unattended sweep never silently drops a find.
"""

import os
from datetime import datetime, timezone

import httpx

from . import settings, store

API = "https://api.notion.com/v1"

def _token():
    return os.getenv("NOTION_TOKEN")


def _database_id():
    return settings.notion_database_id()


def _headers():
    return {
        "Authorization": f"Bearer {_token()}",
        "Notion-Version": settings.NOTION_VERSION,
        "Content-Type": "application/json",
    }


def enabled():
    return bool(_token() and _database_id())


# --- property builders ------------------------------------------------------

def _text(value, limit=2000):
    if value in (None, ""):
        return {"rich_text": []}
    # Notion rejects rich_text content over 2000 chars per block.
    return {"rich_text": [{"text": {"content": str(value)[:limit]}}]}


def _title(value):
    return {"title": [{"text": {"content": str(value)[:200]}}]}


def _select(value):
    return {"select": {"name": str(value)} if value else None}


def _multi(values):
    return {"multi_select": [{"name": str(v)} for v in (values or [])]}


def _number(value):
    try:
        return {"number": float(value)}
    except (TypeError, ValueError):
        return {"number": None}


def _date(iso_or_epoch):
    if not iso_or_epoch:
        return {"date": None}
    if isinstance(iso_or_epoch, (int, float)):
        iso = datetime.fromtimestamp(iso_or_epoch, tz=timezone.utc).isoformat()
    else:
        iso = str(iso_or_epoch)
    return {"date": {"start": iso}}


def _links(sources):
    """
    Render the source links as a Notion rich_text run of real hyperlinks.

    Written as linked text rather than a bare URL dump so the row stays readable
    at a glance: "Post . X . Explorer . OpenSea", each one clickable.
    """
    if not sources:
        return {"rich_text": []}
    runs = []
    for i, source in enumerate(sources[:8]):
        if i:
            runs.append({"text": {"content": "  ·  "}})
        runs.append({"text": {"content": source["label"],
                              "link": {"url": source["url"]}}})
    return {"rich_text": runs}


def build_properties(candidate):
    """Turn an assembled candidate dict into Notion page properties."""
    price = candidate.get("mint_price_eth")
    return {
        "Sources": _links(candidate.get("sources")),
        "Mint Price": _number(price if price is not None else None),
        "Project": _title(candidate.get("name") or candidate.get("handle") or "unknown"),
        "X Handle": _text(candidate.get("handle")),
        "Project URL": ({"url": candidate["url"]} if candidate.get("url") else {"url": None}),
        "Chain": _select(settings.CHAIN_NAME),
        "Status": _select(candidate.get("status") or "Candidate"),
        "Score": _number(candidate.get("score")),
        "Smart Follows": _number(candidate.get("smart_follow_count") or 0),
        "Smart Followers": _text(", ".join(candidate.get("smart_followers") or [])),
        "Mint Type": _select(candidate.get("mint_type") or "Unknown"),
        "Mint Contract": _text(candidate.get("mint_contract")),
        "Mint Open": _date(candidate.get("mint_open")),
        "Deployer": _text(candidate.get("deployer")),
        "Risk Flags": _multi(candidate.get("flags")),
        "OSINT Notes": _text(candidate.get("notes")),
        "Source": _select(candidate.get("source") or "Follow Graph"),
        "Result": _select("Pending"),
    }


# --- writes -----------------------------------------------------------------

def upsert(candidate, log=print):
    """
    Create the row, or update it if this handle is already on the board.

    Never overwrites Armed, Result, or Tx Hash: those are yours and the
    executor's, and a re-scan must not silently disarm a drop you authorized.
    """
    if not enabled():
        store.append_jsonl(settings.NOTION_QUEUE_FILE, {
            "queued_at": datetime.now(timezone.utc).isoformat(),
            "candidate": candidate,
        })
        local_upsert(candidate)
        log(f"  offline board: {candidate.get('handle') or candidate.get('name')} "
            f"(score {candidate.get('score')})")
        return None

    existing = find_by_handle(candidate.get("handle"))
    props = build_properties(candidate)

    try:
        with httpx.Client(timeout=30.0) as client:
            if existing:
                # Preserve the human-owned fields on an update.
                for owned in ("Result",):
                    props.pop(owned, None)
                resp = client.patch(f"{API}/pages/{existing}",
                                    headers=_headers(), json={"properties": props})
            else:
                resp = client.post(f"{API}/pages", headers=_headers(), json={
                    "parent": {"database_id": _database_id()},
                    "properties": props,
                })
            resp.raise_for_status()
            page = resp.json()
    except httpx.HTTPStatusError as e:
        body = e.response.text[:300]
        log(f"  Notion write failed ({e.response.status_code}): {body}")
        store.append_jsonl(settings.NOTION_QUEUE_FILE, {
            "queued_at": datetime.now(timezone.utc).isoformat(),
            "error": body,
            "candidate": candidate,
        })
        return None
    except httpx.HTTPError as e:
        log(f"  Notion write failed: {type(e).__name__}")
        return None

    log(f"  {'updated' if existing else 'added'}: {candidate.get('handle')} "
        f"(score {candidate.get('score')})")
    return page.get("id")


def mark_result(page_id, result, tx_hash=None, status=None, log=print):
    """
    Write back what actually happened after a mint attempt.

    A result must never be lost: it is the only record that a given row has
    already been fired at, and losing it means the executor could re-fire the
    same drop. So when Notion is unreachable this lands on the offline board
    and in a replay queue instead of evaporating.
    """
    if not page_id:
        return False
    if not enabled():
        _record_result_locally(page_id, result, tx_hash, status)
        log(f"  result recorded on the offline board: {result}")
        return True
    props = {"Result": _select(result)}
    if tx_hash:
        props["Tx Hash"] = _text(tx_hash)
    if status:
        props["Status"] = _select(status)
    try:
        with httpx.Client(timeout=30.0) as client:
            resp = client.patch(f"{API}/pages/{page_id}", headers=_headers(),
                                json={"properties": props})
            resp.raise_for_status()
    except httpx.HTTPError as e:
        log(f"Could not write result back to Notion: {type(e).__name__}")
        return False
    return True


# --- reads ------------------------------------------------------------------

def _query(filter_body):
    with httpx.Client(timeout=30.0) as client:
        resp = client.post(f"{API}/databases/{_database_id()}/query",
                           headers=_headers(), json=filter_body)
        resp.raise_for_status()
        return resp.json().get("results", [])


def find_by_handle(handle):
    """Return the page id for an existing row with this X handle, or None."""
    if not enabled() or not handle:
        return None
    try:
        results = _query({
            "filter": {"property": "X Handle", "rich_text": {"equals": handle}},
            "page_size": 1,
        })
    except httpx.HTTPError:
        return None
    return results[0]["id"] if results else None


def _plain(prop):
    """Pull a plain string out of whatever property shape Notion returned."""
    if not isinstance(prop, dict):
        return None
    kind = prop.get("type")
    if kind == "rich_text":
        return "".join(t.get("plain_text", "") for t in prop.get("rich_text", [])) or None
    if kind == "title":
        return "".join(t.get("plain_text", "") for t in prop.get("title", [])) or None
    if kind == "select":
        return (prop.get("select") or {}).get("name")
    if kind == "number":
        return prop.get("number")
    if kind == "checkbox":
        return prop.get("checkbox")
    if kind == "url":
        return prop.get("url")
    if kind == "date":
        return (prop.get("date") or {}).get("start")
    if kind == "multi_select":
        return [o.get("name") for o in prop.get("multi_select", [])]
    return None


def local_armed_rows(log=print):
    """
    Read armed rows from the offline board instead of Notion.

    Connecting Notion requires an integration, a database ID, and a share step,
    and the executor should not be dead in the water until that happens. So
    `radar/state/board.json` is a full stand-in: same rows, same fields, and
    `"armed": true` means exactly what a ticked checkbox means.

    The safety model is unchanged. Nothing in this codebase writes that flag;
    scan.py writes rows with armed=false and preserves whatever is already set.
    """
    board = store.read_json(settings.LOCAL_BOARD_FILE) or {}
    rows = []
    for row in board.get("rows", []):
        if not row.get("armed"):
            continue
        if (row.get("result") or "Pending") != "Pending":
            continue
        rows.append({
            "page_id": row.get("page_id") or f"local:{row.get('handle') or row.get('name')}",
            "name": row.get("name") or row.get("handle") or "unknown",
            "handle": row.get("handle"),
            "mint_contract": row.get("mint_contract"),
            "mint_open": row.get("mint_open"),
            "mint_type": row.get("mint_type"),
            "score": row.get("score"),
            "flags": row.get("flags") or [],
        })
    return rows


def armed_rows(log=print):
    """
    Every row you have ticked Armed that has not already been minted.

    This is what the watcher acts on. An unticked row is never fired at, no
    matter how high it scored. Falls back to the offline board when Notion is
    not connected, so the executor is usable on day one.
    """
    if not enabled():
        rows = local_armed_rows(log=log)
        log(f"Notion is not configured, reading the offline board: {len(rows)} armed row(s).")
        return rows
    try:
        results = _query({
            "filter": {"and": [
                {"property": "Armed", "checkbox": {"equals": True}},
                {"property": "Result", "select": {"equals": "Pending"}},
            ]},
            "page_size": 100,
        })
    except httpx.HTTPError as e:
        log(f"Could not read the watchlist: {type(e).__name__}")
        return []

    rows = []
    for page in results:
        props = page.get("properties", {})
        rows.append({
            "page_id": page["id"],
            "name": _plain(props.get("Project")),
            "handle": _plain(props.get("X Handle")),
            "mint_contract": _plain(props.get("Mint Contract")),
            "mint_open": _plain(props.get("Mint Open")),
            "mint_type": _plain(props.get("Mint Type")),
            "score": _plain(props.get("Score")),
            "flags": _plain(props.get("Risk Flags")) or [],
        })
    return rows


# --- the offline board ------------------------------------------------------

def _board_key(row):
    """Identity of a row. Handle when there is one, otherwise the name."""
    return (row.get("handle") or row.get("name") or "").lower()


def local_upsert(candidate):
    """
    Add or refresh one row on the offline board.

    Refreshing must never clobber the two fields a human or the executor owns:
    `armed` and `result`. A re-scan that silently disarmed a drop you authorized
    would be the single worst bug this system could have.
    """
    board = store.read_json(settings.LOCAL_BOARD_FILE) or {"rows": []}
    rows = board.get("rows", [])
    key = _board_key(candidate)

    row = {
        "name": candidate.get("name") or candidate.get("handle") or "unknown",
        "handle": candidate.get("handle"),
        "url": candidate.get("url"),
        "status": candidate.get("status") or "Candidate",
        "score": candidate.get("score"),
        "smart_follow_count": candidate.get("smart_follow_count") or 0,
        "smart_followers": candidate.get("smart_followers") or [],
        "smart_mentions": candidate.get("smart_mentions") or [],
        "mint_type": candidate.get("mint_type") or "Unknown",
        "mint_contract": candidate.get("mint_contract"),
        "mint_open": candidate.get("mint_open"),
        "deployer": candidate.get("deployer"),
        "flags": candidate.get("flags") or [],
        "notes": candidate.get("notes"),
        "source": candidate.get("source"),
        "sources": candidate.get("sources") or [],
        "mint_price_eth": candidate.get("mint_price_eth"),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }

    for i, existing in enumerate(rows):
        if _board_key(existing) == key and key:
            row["armed"] = existing.get("armed", False)
            row["result"] = existing.get("result", "Pending")
            row["tx_hash"] = existing.get("tx_hash")
            row["page_id"] = existing.get("page_id")
            row["first_seen"] = existing.get("first_seen") or row["updated_at"]
            rows[i] = row
            break
    else:
        row["armed"] = False       # only a human ever changes this
        row["result"] = "Pending"
        row["tx_hash"] = None
        row["first_seen"] = row["updated_at"]
        rows.append(row)

    rows.sort(key=lambda r: (r.get("score") or 0), reverse=True)
    board["rows"] = rows
    board["updated_at"] = datetime.now(timezone.utc).isoformat()
    store.write_json(settings.LOCAL_BOARD_FILE, board)
    return row


def _record_result_locally(page_id, result, tx_hash, status):
    """Persist a mint outcome to the offline board and the Notion replay queue."""
    store.append_jsonl(settings.RESULT_QUEUE_FILE, {
        "queued_at": datetime.now(timezone.utc).isoformat(),
        "page_id": page_id, "result": result,
        "tx_hash": tx_hash, "status": status,
    })
    board = store.read_json(settings.LOCAL_BOARD_FILE) or {"rows": []}
    target = str(page_id).replace("local:", "").lower()
    for row in board.get("rows", []):
        if _board_key(row) == target or row.get("page_id") == page_id:
            row["result"] = result
            row["tx_hash"] = tx_hash
            if status:
                row["status"] = status
            break
    store.write_json(settings.LOCAL_BOARD_FILE, board)


def flush_queue(log=print):
    """
    Push anything that was queued while Notion was not configured.
    """
    if not enabled():
        log("Notion is not configured - nothing flushed.")
        return 0
    queued = store.read_jsonl(settings.NOTION_QUEUE_FILE)
    sent = 0
    for record in queued:
        if upsert(record.get("candidate", {}), log=log):
            sent += 1
    if sent:
        # Clear the queue only for what actually landed.
        os.replace(settings.NOTION_QUEUE_FILE, settings.NOTION_QUEUE_FILE + ".flushed")

    # Results matter more than rows: they are the record that a drop was already
    # fired at. Replay them after the rows exist, so the page ids resolve.
    results = store.read_jsonl(settings.RESULT_QUEUE_FILE)
    replayed = 0
    for record in results:
        page_id = record.get("page_id") or ""
        if str(page_id).startswith("local:"):
            # An offline-board id has no Notion page behind it. Re-resolve it
            # through the handle now that the row has been pushed.
            page_id = find_by_handle(str(page_id).replace("local:", ""))
        if page_id and mark_result(page_id, record.get("result"),
                                   record.get("tx_hash"),
                                   record.get("status"), log=log):
            replayed += 1
    if replayed:
        os.replace(settings.RESULT_QUEUE_FILE, settings.RESULT_QUEUE_FILE + ".flushed")

    log(f"Flushed {sent} queued row(s) and {replayed} result(s) to Notion.")
    return sent
