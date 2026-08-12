"""Small client for OpenSea's documented Drops API."""

from datetime import datetime
from decimal import Decimal, InvalidOperation
import json
import re
import time
from urllib.parse import quote, unquote, urlsplit

import httpx

import config


API_REQUEST_TIMEOUT = 15.0
FIRE_REQUEST_TIMEOUT = 3.0
MAX_429_BACKOFF_SECONDS = 10.0


def _api_key_missing(api_key):
    value = (api_key or "").strip()
    upper = value.upper()
    return not value or "PASTE_" in upper or "YOUR_" in upper


def get_api_client(api_key):
    """Return a reusable HTTP client for OpenSea API calls."""
    if _api_key_missing(api_key):
        raise RuntimeError("OPENSEA_API_KEY is missing or still a placeholder.")
    return httpx.Client(
        headers={"user-agent": config.USER_AGENT},
        timeout=API_REQUEST_TIMEOUT,
        follow_redirects=True,
    )


def _api_headers(api_key, content_type=False):
    headers = {"accept": "application/json", "x-api-key": api_key}
    if content_type:
        headers["content-type"] = "application/json"
    return headers


def _response_message(response):
    """Extract a short, secret-free message from an API error response."""
    try:
        payload = response.json()
    except ValueError:
        return getattr(response, "text", "")[:180].replace("\n", " ")
    if isinstance(payload, dict):
        errors = payload.get("errors")
        if isinstance(errors, list) and errors:
            first = errors[0]
            if isinstance(first, dict):
                return str(first.get("message") or first)[:180]
            return str(first)[:180]
        for key in ("message", "error", "detail"):
            if payload.get(key):
                return str(payload[key])[:180]
    return str(payload)[:180]


def _get_json(client, endpoint, api_key, params=None):
    try:
        response = client.get(
            endpoint,
            headers=_api_headers(api_key),
            params=params,
            timeout=API_REQUEST_TIMEOUT,
        )
    except httpx.HTTPError as exc:
        raise RuntimeError(f"OpenSea API network error ({type(exc).__name__}).") from exc

    if response.status_code != 200:
        detail = _response_message(response)
        suffix = f": {detail}" if detail else ""
        raise RuntimeError(f"OpenSea API request failed (HTTP {response.status_code}){suffix}")
    try:
        return response.json()
    except ValueError as exc:
        raise RuntimeError("OpenSea API returned a non-JSON response.") from exc


def get_drop_details(client, slug, api_key):
    """Return the raw details object for one OpenSea collection slug."""
    if _api_key_missing(api_key):
        raise RuntimeError("OPENSEA_API_KEY is missing or still a placeholder.")
    endpoint = f"{config.OPENSEA_API_BASE_URL.rstrip('/')}/drops/{slug}"
    return _get_json(client, endpoint, api_key)


def prewarm_drop_route(client, slug, api_key):
    """Open the API/TLS connection shortly before launch without minting."""
    if _api_key_missing(api_key):
        raise RuntimeError("OPENSEA_API_KEY is missing or still a placeholder.")
    endpoint = f"{config.OPENSEA_API_BASE_URL.rstrip('/')}/drops/{slug}"
    try:
        response = client.get(
            endpoint,
            headers=_api_headers(api_key),
            timeout=3.0,
        )
    except httpx.HTTPError:
        # Prewarming is an optimization. The real mint request still gets its
        # own bounded retry path if this optional connection setup times out.
        return False
    if response.status_code in (401, 403):
        raise RuntimeError(
            f"OpenSea API authentication failed (HTTP {response.status_code}); check OPENSEA_API_KEY."
        )
    return response.status_code == 200


def drop_detail_route_available(client, slug, api_key):
    """Quickly report whether the detailed drop backend is currently healthy."""
    endpoint = f"{config.OPENSEA_API_BASE_URL.rstrip('/')}/drops/{slug}"
    try:
        response = client.get(
            endpoint,
            headers=_api_headers(api_key),
            timeout=3.0,
        )
    except httpx.HTTPError:
        return False
    return response.status_code == 200


def get_collection_details(client, slug, api_key):
    """Return OpenSea collection metadata used by Telegram detail cards."""
    if _api_key_missing(api_key):
        raise RuntimeError("OPENSEA_API_KEY is missing or still a placeholder.")
    endpoint = f"{config.OPENSEA_API_BASE_URL.rstrip('/')}/collections/{slug}"
    return _get_json(client, endpoint, api_key)


def get_collection_stats(client, slug, api_key):
    """Return OpenSea collection sales, volume, owner, and floor statistics."""
    if _api_key_missing(api_key):
        raise RuntimeError("OPENSEA_API_KEY is missing or still a placeholder.")
    endpoint = f"{config.OPENSEA_API_BASE_URL.rstrip('/')}/collections/{quote(str(slug), safe='-_.~')}/stats"
    return _get_json(client, endpoint, api_key)


def get_collection_floor_prices(client, slug, api_key):
    """Return OpenSea's floor-price history for a collection."""
    if _api_key_missing(api_key):
        raise RuntimeError("OPENSEA_API_KEY is missing or still a placeholder.")
    endpoint = f"{config.OPENSEA_API_BASE_URL.rstrip('/')}/collections/{quote(str(slug), safe='-_.~')}/floor_prices"
    return _get_json(client, endpoint, api_key)


def get_collection_nfts(client, slug, api_key, limit=1, cursor=None):
    """Return a small sample of NFTs from a collection for research cards."""
    if _api_key_missing(api_key):
        raise RuntimeError("OPENSEA_API_KEY is missing or still a placeholder.")
    try:
        limit = max(1, min(200, int(limit)))
    except (TypeError, ValueError):
        limit = 1
    endpoint = f"{config.OPENSEA_API_BASE_URL.rstrip('/')}/collection/{quote(str(slug), safe='-_.~')}/nfts"
    params = {"limit": limit}
    if cursor:
        params["next"] = str(cursor)
    payload = _get_json(client, endpoint, api_key, params=params)
    if not isinstance(payload, dict):
        raise RuntimeError("OpenSea returned an invalid collection NFT response.")
    nfts = payload.get("nfts") or payload.get("items") or payload.get("data") or []
    if not isinstance(nfts, list):
        raise RuntimeError("OpenSea returned an invalid collection NFT list.")
    return nfts, payload.get("next") or payload.get("next_cursor")


def get_account_nfts(client, chain, address, api_key, limit=200, cursor=None):
    """Return NFTs owned by an account on one OpenSea-supported chain."""
    if _api_key_missing(api_key):
        raise RuntimeError("OPENSEA_API_KEY is missing or still a placeholder.")
    chain = str(chain or "").strip().lower()
    if not chain or not re.fullmatch(r"[a-z0-9_-]+", chain):
        raise ValueError("the OpenSea chain slug is invalid")
    address = str(address or "").strip()
    if not re.fullmatch(r"0x[a-fA-F0-9]{40}", address):
        raise ValueError("the wallet address is invalid")
    try:
        limit = max(1, min(200, int(limit)))
    except (TypeError, ValueError):
        limit = 200
    endpoint = (
        f"{config.OPENSEA_API_BASE_URL.rstrip('/')}/chain/{quote(chain, safe='-_.~')}"
        f"/account/{address}/nfts"
    )
    params = {"limit": limit}
    if cursor:
        params["next"] = str(cursor)
    payload = _get_json(client, endpoint, api_key, params=params)
    if not isinstance(payload, dict):
        raise RuntimeError("OpenSea returned an invalid account NFT response.")
    nfts = payload.get("nfts") or payload.get("items") or payload.get("data") or []
    if not isinstance(nfts, list):
        raise RuntimeError("OpenSea returned an invalid account NFT list.")
    return nfts, payload.get("next") or payload.get("next_cursor")


def get_nft(client, chain, contract_address, identifier, api_key):
    """Return one OpenSea NFT record from a parsed asset URL."""
    if _api_key_missing(api_key):
        raise RuntimeError("OPENSEA_API_KEY is missing or still a placeholder.")
    chain = str(chain or "").strip().lower()
    contract_address = str(contract_address or "").strip()
    if not re.fullmatch(r"0x[a-fA-F0-9]{40}", contract_address):
        raise ValueError("the NFT contract address is invalid")
    identifier = str(identifier or "").strip()
    if not identifier or "/" in identifier or "\\" in identifier:
        raise ValueError("the NFT token ID is invalid")
    endpoint = (
        f"{config.OPENSEA_API_BASE_URL.rstrip('/')}/chain/{quote(chain, safe='-_.~')}"
        f"/contract/{contract_address}/nfts/{quote(identifier, safe='-_.~')}"
    )
    payload = _get_json(client, endpoint, api_key)
    if isinstance(payload, dict) and isinstance(payload.get("nft"), dict):
        return payload["nft"]
    if not isinstance(payload, dict):
        raise RuntimeError("OpenSea returned an invalid NFT response.")
    return payload


def get_account_profile(client, identifier, api_key):
    """Return a public OpenSea account profile for an owner/editor identifier."""
    if _api_key_missing(api_key):
        raise RuntimeError("OPENSEA_API_KEY is missing or still a placeholder.")
    identifier = str(identifier or "").strip()
    if not identifier or any(char.isspace() for char in identifier):
        raise ValueError("the OpenSea account identifier is invalid")
    endpoint = f"{config.OPENSEA_API_BASE_URL.rstrip('/')}/accounts/{quote(identifier, safe='-_.@')}"
    payload = _get_json(client, endpoint, api_key)
    if isinstance(payload, dict) and isinstance(payload.get("account"), dict):
        return payload["account"]
    if not isinstance(payload, dict):
        raise RuntimeError("OpenSea returned an invalid account response.")
    return payload


def parse_drop_slug(value):
    """Return a safe collection/drop slug from an OpenSea URL or slug.

    Minting is supported for collection/drop pages, not individual asset URLs.
    Keeping this parser here makes the Telegram and CLI routes use the same
    input rules and prevents arbitrary hosts from being sent to the API.
    """
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("paste an OpenSea collection or drop URL")

    if raw.startswith(("https://", "http://")):
        parsed = urlsplit(raw)
        if parsed.netloc.lower() not in {"opensea.io", "www.opensea.io"}:
            raise ValueError("the URL must point to opensea.io")
        parts = [unquote(part) for part in parsed.path.split("/") if part]
        if len(parts) < 2 or parts[0].lower() not in {
            "collection", "collections", "drop", "drops"
        }:
            raise ValueError(
                "use an OpenSea collection/drop URL, not an individual NFT asset URL"
            )
        raw = parts[-1]

    slug = raw.strip("/")
    if not slug or any(char.isspace() for char in slug) or "/" in slug:
        raise ValueError("the OpenSea collection slug is invalid")
    return slug


def parse_opensea_reference(value):
    """Parse a collection/drop URL or NFT asset URL for the research route.

    Asset references are read-only research targets. They are deliberately not
    accepted by ``parse_drop_slug`` or any mint execution path.
    """
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("paste an OpenSea collection, drop, or NFT asset URL")
    if not raw.startswith(("https://", "http://")):
        return {"kind": "collection", "slug": parse_drop_slug(raw)}
    parsed = urlsplit(raw)
    if parsed.netloc.lower() not in {"opensea.io", "www.opensea.io"}:
        raise ValueError("the URL must point to opensea.io")
    parts = [unquote(part) for part in parsed.path.split("/") if part]
    if len(parts) >= 4 and parts[0].lower() == "assets":
        chain = parts[1].strip().lower()
        contract = parts[2].strip()
        identifier = parts[3].strip()
        if not re.fullmatch(r"0x[a-fA-F0-9]{40}", contract):
            raise ValueError("the NFT asset URL has an invalid contract address")
        if not identifier or any(char.isspace() for char in identifier):
            raise ValueError("the NFT asset URL has an invalid token ID")
        return {
            "kind": "asset",
            "chain": chain,
            "contract_address": contract,
            "identifier": identifier,
            "url": raw,
        }
    return {"kind": "collection", "slug": parse_drop_slug(raw)}


def list_drops(client, api_key, chain_slug, drop_type="upcoming", limit=None, cursor=None):
    """Return one OpenSea drop-calendar page for a chain and feed type."""
    if _api_key_missing(api_key):
        raise RuntimeError("OPENSEA_API_KEY is missing or still a placeholder.")
    drop_type = str(drop_type or "upcoming").strip().lower()
    if drop_type not in {"featured", "upcoming", "recently_minted"}:
        raise ValueError("drop type must be featured, upcoming, or recently_minted")
    requested = limit or config.DISCOVERY_LIMIT_PER_CHAIN
    requested = max(1, min(100, int(requested)))
    endpoint = f"{config.OPENSEA_API_BASE_URL.rstrip('/')}/drops"
    params = {"type": drop_type, "limit": requested}
    if str(chain_slug or "").strip():
        params["chains"] = str(chain_slug).strip()
    if cursor:
        params["cursor"] = cursor
    payload = _get_json(
        client,
        endpoint,
        api_key,
        params=params,
    )
    if not isinstance(payload, dict):
        raise RuntimeError("OpenSea returned an invalid upcoming-drops response.")
    drops = payload.get("drops") or payload.get("data") or []
    if not isinstance(drops, list):
        raise RuntimeError("OpenSea returned an invalid drops list.")
    cursor = payload.get("next") or payload.get("nextCursor") or payload.get("next_cursor")
    return drops, cursor


def list_upcoming_drops(client, api_key, chain_slug, limit=None, cursor=None):
    """Compatibility wrapper for callers that only need the upcoming feed."""
    return list_drops(
        client,
        api_key,
        chain_slug,
        drop_type="upcoming",
        limit=limit,
        cursor=cursor,
    )


def list_top_collections(client, api_key, chain_slug, limit=None):
    """Return OpenSea's one-day top collections for one chain.

    This is a discovery supplement, not proof that a collection has a mint.
    Callers must validate every returned slug with ``get_drop_info``.
    """
    if _api_key_missing(api_key):
        raise RuntimeError("OPENSEA_API_KEY is missing or still a placeholder.")
    requested = limit or config.DISCOVERY_RANKED_FALLBACK_LIMIT
    requested = max(1, min(100, int(requested)))
    endpoint = f"{config.OPENSEA_API_BASE_URL.rstrip('/')}/collections/top"
    payload = _get_json(
        client,
        endpoint,
        api_key,
        params={"chains": chain_slug, "timeframe": "one_day", "limit": requested},
    )
    if not isinstance(payload, dict):
        raise RuntimeError("OpenSea returned an invalid top-collections response.")
    collections = payload.get("collections") or payload.get("data") or []
    if not isinstance(collections, list):
        raise RuntimeError("OpenSea returned an invalid top-collections list.")
    return collections


def _drop_object(payload):
    """Accept the documented direct shape and common response wrappers."""
    if not isinstance(payload, dict):
        return None
    candidates = [payload]
    for key in ("drop", "data", "result"):
        value = payload.get(key)
        if isinstance(value, dict):
            candidates.append(value)
            nested = value.get("drop")
            if isinstance(nested, dict):
                candidates.append(nested)
    for candidate in candidates:
        if "stages" in candidate:
            return candidate
    return None


def _stage_value(stage, *names):
    for name in names:
        if name in stage and stage[name] is not None:
            return stage[name]
    return None


def get_drop_info(client, slug, api_key, include_metadata=True):
    """Return normalized drop metadata for schedule and inspection screens."""
    drop = _drop_object(get_drop_details(client, slug, api_key))
    if not drop:
        raise RuntimeError("OpenSea returned no drop stages for this slug.")
    raw_stages = drop.get("stages")
    if not isinstance(raw_stages, list):
        raise RuntimeError("OpenSea returned an invalid stages list.")

    normalized = []
    for position, stage in enumerate(raw_stages):
        if not isinstance(stage, dict):
            continue
        raw_index = _stage_value(stage, "stageIndex", "stage_index", "index")
        try:
            stage_index = int(raw_index) if raw_index is not None else position
        except (TypeError, ValueError):
            stage_index = position
        normalized.append({
            "stageIndex": stage_index,
            "startTime": _to_epoch(_stage_value(stage, "startTime", "start_time")),
            "endTime": _to_epoch(_stage_value(stage, "endTime", "end_time")),
            "label": _stage_value(stage, "label", "name", "stageLabel"),
            "stageType": _stage_value(stage, "stageType", "stage_type", "type"),
            "price": _stage_value(stage, "price", "mintPrice", "mint_price"),
            "priceCurrencyAddress": _stage_value(
                stage, "priceCurrencyAddress", "price_currency_address"
            ),
            "maxPerWallet": _stage_value(stage, "maxPerWallet", "max_per_wallet"),
            "uuid": _stage_value(stage, "uuid", "id"),
        })

    if not normalized:
        raise RuntimeError("OpenSea returned no usable mint stages.")
    # The drop endpoint provides stages and supply but not always the
    # collection's long-form description or project links. Metadata is
    # best-effort here so scheduling still works if this second request fails.
    if include_metadata:
        try:
            collection_info = get_collection_details(client, slug, api_key)
        except Exception:
            collection_info = {}
    else:
        collection_info = {}
    collection = drop.get("collection")
    collection_name = collection.get("name") if isinstance(collection, dict) else None
    name = (
        drop.get("collectionName")
        or drop.get("collection_name")
        or drop.get("name")
        or collection_info.get("name")
        or collection_name
        or slug
    )
    collection_url = (
        collection_info.get("opensea_url")
        or drop.get("openseaUrl")
        or drop.get("opensea_url")
        or f"https://opensea.io/collection/{slug}"
    )
    contract_address = drop.get("contractAddress") or drop.get("contract_address")
    if not contract_address:
        contracts = collection_info.get("contracts")
        if isinstance(contracts, list):
            for contract in contracts:
                if isinstance(contract, dict) and str(contract.get("chain") or "").lower() == str(drop.get("chain") or "").lower():
                    contract_address = contract.get("address")
                    break
    return {
        "slug": str(drop.get("collectionSlug") or drop.get("collection_slug") or slug),
        "name": str(name),
        "chain": str(drop.get("chain") or "").strip().lower(),
        "contract_address": contract_address,
        "opensea_url": collection_url,
        "metadata": {
            "description": collection_info.get("description") or "",
            "image_url": collection_info.get("image_url") or drop.get("image_url") or "",
            "banner_image_url": collection_info.get("banner_image_url") or "",
            "project_url": collection_info.get("project_url") or "",
            "twitter_url": collection_info.get("twitter_url") or (
                f"https://x.com/{collection_info.get('twitter_username')}"
                if collection_info.get("twitter_username") else ""
            ),
            "discord_url": collection_info.get("discord_url") or "",
            "telegram_url": collection_info.get("telegram_url") or "",
            "wiki_url": collection_info.get("wiki_url") or "",
            "total_supply": collection_info.get("total_supply") or drop.get("total_supply"),
            "max_supply": drop.get("max_supply") or collection_info.get("max_supply"),
            "contracts": collection_info.get("contracts") or [],
            "opensea_url": collection_url,
        },
        "stages": normalized,
    }


def get_drop_schedule(client, slug, api_key):
    """Return ``(name, stages)`` in the format used by the one-drop runner."""
    info = get_drop_info(client, slug, api_key, include_metadata=False)
    return info["name"], info["stages"]


def get_public_drop_schedule(client, slug):
    """Read all public mint stages embedded in an OpenSea collection page.

    This is a read-only fallback for discovery when OpenSea's documented
    per-drop detail endpoint is temporarily unavailable. The official API
    calendar remains the source of collection/chain identity.
    """
    endpoint = f"https://opensea.io/collection/{quote(str(slug), safe='-_.~')}"
    try:
        response = client.get(
            endpoint,
            headers={"accept": "text/html", "user-agent": config.USER_AGENT},
            timeout=API_REQUEST_TIMEOUT,
        )
    except httpx.HTTPError as exc:
        raise RuntimeError(f"OpenSea page network error ({type(exc).__name__}).") from exc
    if response.status_code != 200:
        raise RuntimeError(f"OpenSea collection page failed (HTTP {response.status_code}).")
    text = response.text
    marker = '"dropBySlug":'
    position = text.find(marker)
    if position < 0:
        return []
    try:
        payload, _ = json.JSONDecoder().raw_decode(text, position + len(marker))
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    raw_stages = payload.get("stages") if isinstance(payload, dict) else []
    if not isinstance(raw_stages, list):
        return []
    stages = []
    for position, stage in enumerate(raw_stages):
        if not isinstance(stage, dict):
            continue
        token = ((stage.get("price") or {}).get("token") or {})
        unit = token.get("unit")
        try:
            price_wei = int(Decimal(str(unit)) * Decimal(10 ** 18))
        except (InvalidOperation, TypeError, ValueError):
            price_wei = None
        stages.append({
            "stageIndex": stage.get("stageIndex", position),
            "startTime": _to_epoch(stage.get("startTime")),
            "endTime": _to_epoch(stage.get("endTime")),
            "label": stage.get("label"),
            "stageType": stage.get("stageType"),
            "price": price_wei,
            "priceCurrencyAddress": token.get("contractAddress"),
            "maxPerWallet": stage.get("maxTotalMintableByWallet"),
            "uuid": stage.get("uuid"),
        })
    return stages


def get_mint_calldata(client, slug, stage_index, quantity, address, api_key):
    """Request ready-to-sign calldata for the first eligible active stage."""
    del stage_index
    if _api_key_missing(api_key):
        return None, "STOP: OPENSEA_API_KEY is missing or still a placeholder."
    if not isinstance(quantity, int) or not 1 <= quantity <= 100:
        return None, "STOP: MINT_QUANTITY must be an integer from 1 through 100."

    endpoint = f"{config.OPENSEA_API_BASE_URL.rstrip('/')}/drops/{slug}/mint"
    try:
        response = client.post(
            endpoint,
            headers=_api_headers(api_key, content_type=True),
            json={"minter": address, "quantity": quantity},
            timeout=FIRE_REQUEST_TIMEOUT,
        )
    except httpx.HTTPError as exc:
        return None, f"network hiccup: {type(exc).__name__}"

    status = response.status_code
    if status == 429:
        retry_after = response.headers.get("retry-after")
        try:
            pause = min(float(retry_after), MAX_429_BACKOFF_SECONDS)
        except (TypeError, ValueError):
            pause = 1.0
        time.sleep(pause)
        return None, f"rate limited (HTTP 429), backed off {pause:.1f}s"
    if status in (401, 403):
        return None, f"STOP: OpenSea API authentication failed (HTTP {status}); check OPENSEA_API_KEY."
    if status == 422:
        return None, f"STOP: OpenSea rejected the mint precondition: {_response_message(response)}"
    if status in (400, 404):
        return None, f"STOP: OpenSea rejected the mint request (HTTP {status}): {_response_message(response)}"
    if status == 409:
        return None, "drop is not active yet (HTTP 409)"
    if status >= 500:
        return None, f"OpenSea server error (HTTP {status})"
    if status != 200:
        return None, f"STOP: unexpected OpenSea API response (HTTP {status})"

    try:
        payload = response.json()
    except ValueError:
        return None, "STOP: OpenSea returned a non-JSON mint response"
    transaction = _find_transaction_payload(payload)
    if not transaction or not transaction.get("to") or not transaction.get("data"):
        return None, "STOP: OpenSea returned no usable transaction fields"
    try:
        value = _to_wei_int(transaction.get("value"))
    except (TypeError, ValueError):
        return None, f"STOP: could not parse the mint value {transaction.get('value')!r}"
    return {"to": transaction["to"], "data": transaction["data"], "value": value}, None


def _to_wei_int(value):
    if value is None:
        return 0
    if isinstance(value, int):
        return value
    text = str(value).strip()
    if not text:
        return 0
    if text.lower().startswith("0x"):
        return int(text, 16)
    return int(text)


def _find_transaction_payload(obj):
    """Accept the target/calldata and to/data spellings used in OpenSea docs."""
    if isinstance(obj, dict):
        to = obj.get("to") or obj.get("target")
        data = obj.get("data") or obj.get("calldata")
        if isinstance(to, str) and isinstance(data, str) and data:
            return {"to": to, "data": data, "value": obj.get("value", 0)}
        for value in obj.values():
            found = _find_transaction_payload(value)
            if found:
                return found
    elif isinstance(obj, list):
        for value in obj:
            found = _find_transaction_payload(value)
            if found:
                return found
    return None


def _to_epoch(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    try:
        return int(value)
    except (ValueError, TypeError):
        pass
    try:
        return int(datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp())
    except (ValueError, TypeError, OverflowError):
        return None
