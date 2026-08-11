"""
Talks to OpenSea's internal GraphQL endpoint (the one the real website uses for
minting). Two jobs:

  1. get_drop_schedule() - read WHEN the drop opens and its stage timing.
  2. get_mint_calldata()  - at open time, ask OpenSea for the actual mint
     instructions (the target contract, the encoded calldata that contains
     OpenSea's server-generated salt + signature, and the ETH value which is
     0 for a free mint). You cannot build these yourself - only OpenSea's
     backend can produce the signature, and only right at mint time.

IMPORTANT, PLEASE READ:
The two GraphQL "documents" below (DROP_QUERY and MINT_CALLDATA_QUERY) are the
best reconstruction from the article + a live scan of OpenSea's site on
2026-07-18. OpenSea loads the exact mint query only after you click into a live
drop, so its precise current shape could not be captured without a real drop
open in a browser. If a query below is rejected, the README section
"Capturing the real OpenSea queries" walks you through copying the exact,
current query out of your browser's Network tab in about two minutes and
pasting it here. Everything else in the bot stays the same.
"""

import time

import httpx

import config

# During the fire loop a hung request must not eat the whole race window, so
# mint-calldata calls get a much shorter timeout than the client default.
FIRE_REQUEST_TIMEOUT = 3.0

# If OpenSea rate-limits us (HTTP 429), pause at most this long before the
# next attempt, even if their Retry-After header asks for more - the race
# window is only ~30s total.
MAX_429_BACKOFF_SECONDS = 2.0

# --- Drop schedule query --------------------------------------------------
# Verified against the LIVE endpoint on 2026-07-18: this exact query returns
# real stage data. The drop's display name lives on collection.name (a bare
# "name" field on Drop is rejected by their schema).
DROP_QUERY_NAME = "FreeMintBotDropSchedule"
DROP_QUERY = """
query FreeMintBotDropSchedule($slug: String!) {
  dropBySlug(slug: $slug) {
    __typename
    collection {
      name
    }
    stages {
      stageIndex
      startTime
      endTime
    }
  }
}
"""

# --- Mint calldata query --------------------------------------------------
# In the article this was the swap() operation (aka MintActionTimelineQuery),
# whose result carried transactionSubmissionData. That field name is confirmed
# still present in the live site. The operation NAME and exact variables below
# are the piece most likely to need the 2-minute browser capture described above.
MINT_CALLDATA_QUERY_NAME = "FreeMintBotMintCalldata"
MINT_CALLDATA_QUERY = """
query FreeMintBotMintCalldata($slug: String!, $stageIndex: Int!, $quantity: Int!, $address: Address!) {
  mintActions(slug: $slug, stageIndex: $stageIndex, quantity: $quantity, address: $address) {
    transactionSubmissionData {
      to
      data
      value
      chain
    }
  }
}
"""


def _post(client, operation_name, query, variables, timeout=None):
    body = {"operationName": operation_name, "query": query, "variables": variables}
    kwargs = {"json": body}
    if timeout is not None:
        kwargs["timeout"] = timeout
    resp = client.post(config.GQL_ENDPOINT, **kwargs)
    resp.raise_for_status()
    payload = resp.json()
    if payload.get("errors"):
        # Return the errors to the caller instead of crashing - during the fire
        # loop a "not open yet" error is expected and we simply retry.
        return None, payload["errors"]
    return payload.get("data"), None


def get_drop_schedule(client, slug):
    """
    Returns a list of stages, each a dict with stageIndex, startTime, endTime
    (unix seconds). Raises if OpenSea returns no drop for the slug.
    """
    data, errors = _post(client, DROP_QUERY_NAME, DROP_QUERY, {"slug": slug})
    if errors or not data or not data.get("dropBySlug"):
        raise RuntimeError(f"Could not read drop schedule for '{slug}'. "
                           f"Check the slug in config.py. Details: {errors}")
    drop = data["dropBySlug"]
    name = (drop.get("collection") or {}).get("name") or slug
    stages = drop.get("stages") or []
    normalized = []
    for s in stages:
        normalized.append({
            "stageIndex": s.get("stageIndex"),
            "startTime": _to_epoch(s.get("startTime")),
            "endTime": _to_epoch(s.get("endTime")),
        })
    return name, normalized


def get_mint_calldata(client, slug, stage_index, quantity, address):
    """
    Asks OpenSea for the real mint instructions. Returns (calldata, note):
    calldata is a dict with keys 'to', 'data', 'value' on success, or None if
    OpenSea has not opened the mint / not issued a signature yet / the request
    failed transiently - the fire loop just retries until its deadline either
    way. note is a short human string saying WHY this attempt got nothing
    (so the fire loop can log it without crashing), or None on success.

    This function must never raise: during the fire window we retry ~7x/second
    against servers under their heaviest load, and a single dropped connection
    or 5xx must count as "not ready yet", not kill the run.
    """
    variables = {
        "slug": slug,
        "stageIndex": stage_index,
        "quantity": quantity,
        "address": address,
    }
    try:
        data, errors = _post(client, MINT_CALLDATA_QUERY_NAME, MINT_CALLDATA_QUERY,
                             variables, timeout=FIRE_REQUEST_TIMEOUT)
    except httpx.HTTPStatusError as e:
        status = e.response.status_code
        if status == 429:
            retry_after = e.response.headers.get("retry-after")
            try:
                pause = min(float(retry_after), MAX_429_BACKOFF_SECONDS)
            except (TypeError, ValueError):
                pause = 0.5
            time.sleep(pause)
            return None, f"rate limited (HTTP 429), backed off {pause:.1f}s"
        return None, f"HTTP {status} from OpenSea"
    except httpx.HTTPError as e:
        return None, f"network hiccup: {type(e).__name__}"
    except ValueError:
        return None, "OpenSea sent a non-JSON response"

    if errors:
        first = errors[0] if isinstance(errors, list) and errors else errors
        msg = first.get("message") if isinstance(first, dict) else str(first)
        return None, f"OpenSea says: {str(msg)[:120]}"
    if not data:
        return None, "empty response"
    # Walk to transactionSubmissionData wherever it sits in the response.
    tsd = _find_submission_data(data)
    if not tsd or not tsd.get("to") or not tsd.get("data"):
        return None, "no mint instructions in the response yet"
    try:
        value = _to_wei_int(tsd.get("value"))
    except ValueError:
        return None, f"could not parse the mint value {tsd.get('value')!r}"
    return {
        "to": tsd["to"],
        "data": tsd["data"],
        "value": value,
    }, None


def _to_wei_int(value):
    # OpenSea may express the value as an int, a decimal string, or a 0x hex
    # string - normalize all three so a formatting choice can't crash us at
    # fire time.
    if value is None:
        return 0
    if isinstance(value, int):
        return value
    s = str(value).strip()
    if not s:
        return 0
    if s.lower().startswith("0x"):
        return int(s, 16)
    return int(s)


def _find_submission_data(obj):
    # The mint query nests transactionSubmissionData; find it without hardcoding
    # the exact path, so a small shape change in the wrapper still works.
    if isinstance(obj, dict):
        if "transactionSubmissionData" in obj and obj["transactionSubmissionData"]:
            return obj["transactionSubmissionData"]
        for v in obj.values():
            found = _find_submission_data(v)
            if found:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = _find_submission_data(v)
            if found:
                return found
    return None


def _to_epoch(value):
    # startTime/endTime may come back as unix seconds already or as an ISO
    # string - accept either and return unix seconds (or None).
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    try:
        return int(value)
    except (ValueError, TypeError):
        pass
    from datetime import datetime
    try:
        return int(datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None
