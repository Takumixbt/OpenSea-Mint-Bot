"""Discover OpenSea drop stages and classify their free/access eligibility."""

from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from concurrent.futures import ThreadPoolExecutor, as_completed
import re
import threading
import time

import config
import opensea_client


@dataclass(frozen=True)
class DropCandidate:
    slug: str
    name: str
    chain: str
    chain_id: int
    stage_index: int
    stage_label: str
    start_time: int
    end_time: int | None
    price_wei: int | None
    price_display: str
    access_label: str
    is_free: bool
    is_public: bool
    url: str
    description: str = ""
    image_url: str = ""
    banner_image_url: str = ""
    project_url: str = ""
    twitter_url: str = ""
    discord_url: str = ""
    telegram_url: str = ""
    wiki_url: str = ""
    contract_address: str = ""
    opensea_url: str = ""
    total_supply: str | int | None = None
    max_supply: str | int | None = None
    max_per_wallet: int | None = None
    # UUID is OpenSea's stable stage identity. Array position is only a display
    # fallback and can change between the compact calendar and full drop detail.
    stage_id: str = ""
    stage_type: str = ""
    drop_type: str = ""
    is_minting: bool | None = None
    is_sold_out: bool = False
    details_verified: bool = False
    # OpenSea occasionally supplies a dedicated drop/mint URL. Keep it
    # separate from the collection URL; never guess one from a project site.
    mint_url: str = ""
    route: str = "opensea_drop"
    route_label: str = "OpenSea drop transaction"

    def key(self):
        identity = self.stage_id or f"{self.stage_index}:{self.start_time}"
        return f"{self.chain}:{self.slug}:{identity}"

    def to_dict(self):
        return asdict(self)


# Backwards-compatible name for callers that imported the old class. The
# discovery route now intentionally returns all stage types, not just free ones.
FreeMintCandidate = DropCandidate


def _nested_value(item, *names):
    if not isinstance(item, dict):
        return None
    for name in names:
        value = item.get(name)
        if value is not None:
            return value
    return None


def _drop_slug(item):
    if not isinstance(item, dict):
        return None
    nested = item.get("collection") if isinstance(item.get("collection"), dict) else {}
    return (
        _nested_value(item, "collectionSlug", "collection_slug", "slug")
        or _nested_value(nested, "slug", "collectionSlug", "collection_slug")
    )


def _drop_name(item, fallback):
    if not isinstance(item, dict):
        return fallback
    nested = item.get("collection") if isinstance(item.get("collection"), dict) else {}
    return (
        _nested_value(item, "collectionName", "collection_name", "name")
        or _nested_value(nested, "name", "collectionName")
        or fallback
    )


def _ranked_collection_slug(item):
    if not isinstance(item, dict):
        return None
    collection = item.get("collection")
    if isinstance(collection, str):
        return collection.strip() or None
    return _drop_slug(item)


def _calendar_stages(item):
    """Normalize stage summaries already included in OpenSea's drop calendar."""
    if not isinstance(item, dict):
        return []
    raw_stages = []
    for key in ("active_stage", "activeStage", "next_stage", "nextStage"):
        stage = item.get(key)
        if isinstance(stage, dict) and stage not in raw_stages:
            raw_stages.append(stage)
    stages = []
    for position, stage in enumerate(raw_stages):
        raw_index = _nested_value(stage, "stageIndex", "stage_index", "index")
        stages.append({
            "stageIndex": position if raw_index is None else raw_index,
            "startTime": opensea_client._to_epoch(
                _nested_value(stage, "startTime", "start_time")
            ),
            "endTime": opensea_client._to_epoch(
                _nested_value(stage, "endTime", "end_time")
            ),
            "label": _nested_value(stage, "label", "name", "stageLabel"),
            "stageType": _nested_value(stage, "stageType", "stage_type", "type"),
            "price": _nested_value(stage, "price", "mintPrice", "mint_price"),
            "priceCurrencyAddress": _nested_value(
                stage, "priceCurrencyAddress", "price_currency_address"
            ),
            "maxPerWallet": _nested_value(stage, "maxPerWallet", "max_per_wallet"),
            "uuid": _nested_value(stage, "uuid", "id"),
        })
    return stages


def _calendar_has_stage_fields(item):
    return isinstance(item, dict) and any(
        key in item for key in ("active_stage", "activeStage", "next_stage", "nextStage")
    )


def _calendar_metadata(item):
    """Return the useful collection fields included with a calendar card."""
    if not isinstance(item, dict):
        return {}
    return {
        "image_url": item.get("image_url") or item.get("imageUrl") or "",
        "opensea_url": item.get("opensea_url") or item.get("openseaUrl") or "",
        "mint_url": (
            item.get("mint_url") or item.get("mintUrl")
            or item.get("drop_url") or item.get("dropUrl") or ""
        ),
    }


def _stage_touches_day(stages, day_start, day_end):
    """Return whether a summary stage starts or ends during the selected day."""
    for stage in stages or []:
        for key in ("startTime", "endTime"):
            try:
                value = int(stage.get(key))
            except (AttributeError, TypeError, ValueError):
                continue
            if day_start <= value <= day_end:
                return True
    return False


def _price_number(value):
    """Return a decimal display price, or None when the price is unknown."""
    if isinstance(value, dict):
        value = _nested_value(value, "amount", "value", "price")
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float, Decimal)):
        try:
            return Decimal(str(value))
        except InvalidOperation:
            return None
    text = str(value).strip().replace(",", "")
    if not text:
        return None
    if text.lower().startswith("0x"):
        try:
            return Decimal(int(text, 16))
        except ValueError:
            return None
    match = re.search(r"[-+]?\d+(?:\.\d+)?", text)
    if not match:
        return None
    try:
        return Decimal(match.group(0))
    except InvalidOperation:
        return None


def _is_public_label(label):
    text = (label or "").strip().lower()
    if not text:
        return True
    # OpenSea stage types commonly use underscores (for example,
    # ``signed_presale``). Treat separators as spaces before applying the
    # word-boundary rules below so gated stages are not mislabeled public.
    text = re.sub(r"[_-]+", " ", text)
    # Treat explicitly gated/reserved stages as restricted. A raw substring
    # check would reject harmless text such as "newly announced" because it
    # contains "wl", so keep the terms word-bounded and conservative.
    restricted = re.compile(
        r"\b(?:allow\s*list|white\s*list|wl|private|presale|pre-sale|early|"
        r"holder(?:s)?|team|staff|partner(?:s)?|member(?:s)?|"
        r"invite(?:d)?|community|reserved|token-gated|token\s+gated)\b"
    )
    return restricted.search(text) is None


def _stage_access(stage):
    """Return ``(label, is_public, access_label)`` for a raw stage."""
    label = str(stage.get("label") or "").strip()
    stage_type = str(
        stage.get("stageType") or stage.get("stage_type") or ""
    ).strip()
    display = label or stage_type or "Public/unknown"
    # OpenSea can expose a neutral label while stage_type carries the gate.
    # Classify both fields together so holder/allowlist stages are not shown as
    # public merely because their display label is generic.
    is_public = _is_public_label(" ".join(part for part in (label, stage_type) if part))
    if not is_public:
        access_label = f"Restricted · {display}"
    else:
        access_label = display
    return display, is_public, access_label


def _stage_price_wei(stage):
    """Return an integer price when the API supplied one in wei."""
    raw = stage.get("price")
    number = _price_number(raw)
    if number is None or number < 0 or number != number.to_integral_value():
        return None
    return int(number)


def _format_decimal(number):
    text = format(number, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _stage_price_info(stage, native):
    """Return ``(wei_or_none, display, is_free)`` without guessing unknown prices."""
    number = _price_number(stage.get("price"))
    if number is None or number < 0:
        return None, "Price unknown", False
    if number == 0:
        return 0, "Free", True

    # OpenSea's drop schedule normally reports wei. Keep the raw numeric value
    # safe even when a provider returns a fractional/native display amount.
    if number == number.to_integral_value():
        amount = number / Decimal(10 ** 18)
        return int(number), f"Paid · {_format_decimal(amount)} {native}", False
    return None, f"Paid · {_format_decimal(number)} {native}", False


def build_drop_candidates(
    slug,
    name,
    chain_slug,
    stages,
    now=None,
    include_expired=False,
    metadata=None,
    contract_address="",
    opensea_url="",
):
    """Normalize all usable stages for one drop, including restricted stages."""
    chain_slug = str(chain_slug or "").strip().lower()
    chain_info = config.chain_config(chain_slug)
    if not chain_info:
        raise ValueError(f"{chain_slug}: no configured EVM RPC mapping")
    now = int(time.time() if now is None else now)
    metadata = metadata if isinstance(metadata, dict) else {}
    total_supply = metadata.get("total_supply")
    max_supply = metadata.get("max_supply")
    try:
        sold_out = int(max_supply) > 0 and int(total_supply) >= int(max_supply)
    except (TypeError, ValueError):
        sold_out = False
    candidates = []
    for stage in stages or []:
        if not isinstance(stage, dict):
            continue
        start = stage.get("startTime")
        if start is None:
            continue
        try:
            start = int(start)
        except (TypeError, ValueError):
            continue
        if start <= 0:
            continue
        end = stage.get("endTime")
        try:
            end = int(end) if end is not None else None
        except (TypeError, ValueError):
            end = None
        if not include_expired and end is not None and end < now:
            continue
        try:
            stage_index = int(stage.get("stageIndex", 0))
        except (TypeError, ValueError):
            continue
        try:
            max_per_wallet = int(stage.get("maxPerWallet")) if stage.get("maxPerWallet") is not None else None
        except (TypeError, ValueError):
            max_per_wallet = None
        stage_label, is_public, access_label = _stage_access(stage)
        stage_type = str(stage.get("stageType") or stage.get("stage_type") or "").strip()
        stage_id = str(stage.get("uuid") or stage.get("id") or "").strip()
        price_wei, price_display, is_free = _stage_price_info(
            stage, chain_info["native"]
        )
        candidates.append(FreeMintCandidate(
            slug=str(slug),
            name=str(name or slug),
            chain=chain_slug,
            chain_id=chain_info["chain_id"],
            stage_index=stage_index,
            stage_label=stage_label,
            start_time=start,
            end_time=end,
            price_wei=price_wei,
            price_display=price_display,
            access_label=access_label,
            is_free=is_free,
            is_public=is_public,
            url=f"https://opensea.io/collection/{slug}",
            description=str(metadata.get("description") or ""),
            image_url=str(metadata.get("image_url") or ""),
            banner_image_url=str(metadata.get("banner_image_url") or ""),
            project_url=str(metadata.get("project_url") or ""),
            twitter_url=str(metadata.get("twitter_url") or ""),
            discord_url=str(metadata.get("discord_url") or ""),
            telegram_url=str(metadata.get("telegram_url") or ""),
            wiki_url=str(metadata.get("wiki_url") or ""),
            contract_address=str(contract_address or ""),
            opensea_url=str(opensea_url or metadata.get("opensea_url") or f"https://opensea.io/collection/{slug}"),
            total_supply=total_supply,
            max_supply=max_supply,
            max_per_wallet=max_per_wallet,
            stage_id=stage_id,
            stage_type=stage_type,
            drop_type=str(metadata.get("drop_type") or ""),
            is_minting=(
                bool(metadata.get("is_minting"))
                if metadata.get("is_minting") is not None else None
            ),
            is_sold_out=sold_out,
            details_verified=bool(metadata.get("details_verified")),
            mint_url=str(metadata.get("mint_url") or ""),
        ))
    return sorted(candidates, key=lambda item: (item.start_time, item.stage_index))


def _stage_is_relevant(stage, now, horizon):
    """Return whether a compact calendar stage is live or opens by horizon."""
    try:
        start = int(stage.get("startTime") or 0)
    except (AttributeError, TypeError, ValueError):
        return False
    try:
        raw_end = stage.get("endTime")
        end = int(raw_end) if raw_end is not None else None
    except (AttributeError, TypeError, ValueError):
        end = None
    return start > 0 and start <= horizon and (end is None or end >= now)


def _feed_pages(client, api_key, chain_filter, drop_type):
    """Read every cursor for one official OpenSea drop feed."""
    rows = []
    cursor = None
    seen_cursors = set()
    page_count = 0
    while True:
        page_count += 1
        page, next_cursor = opensea_client.list_drops(
            client,
            api_key,
            chain_filter,
            drop_type,
            config.DISCOVERY_LIMIT_PER_CHAIN,
            cursor,
        )
        for row in page:
            if not isinstance(row, dict):
                continue
            item = dict(row)
            item["_feed_types"] = [drop_type]
            rows.append(item)
        if not next_cursor or str(next_cursor) in seen_cursors:
            break
        page_limit = int(config.DISCOVERY_MAX_PAGES_PER_CHAIN or 0)
        if page_limit > 0 and page_count >= page_limit:
            break
        seen_cursors.add(str(next_cursor))
        cursor = next_cursor
    return rows


# ---------------------------------------------------------------------------
# Shared drop-calendar cache
# ---------------------------------------------------------------------------
# OpenSea's global /drops cursor already contains every chain, so the whole
# calendar can be read once and filtered locally. Scanning one network then
# another used to repeat the same multi-page cursor walk; caching the merged
# calendar for a short window turns the second scan into local work and lets
# the chain picker show real per-network counts without extra API calls.

_CALENDAR_LOCK = threading.Lock()
_CALENDAR_CACHE = {"cards": None, "errors": [], "fetched_at": 0.0}


def _fetch_calendar(client, api_key):
    """Read all configured drop feeds once and merge them by (chain, slug)."""
    rows = []
    errors = []
    with ThreadPoolExecutor(
        max_workers=max(1, min(len(config.DISCOVERY_DROP_TYPES), 3)),
        thread_name_prefix="opensea-feeds",
    ) as executor:
        futures = {
            executor.submit(_feed_pages, client, api_key, "", drop_type): drop_type
            for drop_type in config.DISCOVERY_DROP_TYPES
        }
        for future in as_completed(futures):
            drop_type = futures[future]
            try:
                rows.extend(future.result())
            except Exception as exc:
                errors.append(f"{drop_type} feed unavailable ({type(exc).__name__})")

    cards = {}
    for card in rows:
        slug = _drop_slug(card)
        if not slug:
            continue
        # An empty chain is kept rather than discarded: the per-drop detail
        # endpoint is authoritative and will resolve the network later.
        chain = str(card.get("chain") or "").strip().lower()
        key = (chain, str(slug).lower())
        cards[key] = _merge_card(cards.get(key), card)
    return cards, errors


def load_calendar(client, api_key, max_age_seconds=None, force=False):
    """Return ``(cards, errors, age_seconds)`` for the merged drop calendar.

    A cached calendar is reused while it is younger than ``max_age_seconds``.
    Every feed failing is reported as an error rather than silently returning
    an empty catalogue, so ``/scan`` can tell "OpenSea has no drops here" apart
    from "OpenSea did not answer".
    """
    if max_age_seconds is None:
        max_age_seconds = config.DISCOVERY_CALENDAR_TTL_SECONDS
    with _CALENDAR_LOCK:
        cached = _CALENDAR_CACHE["cards"]
        age = time.time() - float(_CALENDAR_CACHE["fetched_at"] or 0)
        if not force and cached is not None and age <= float(max_age_seconds):
            return dict(cached), list(_CALENDAR_CACHE["errors"]), age

        cards, errors = _fetch_calendar(client, api_key)
        if not cards and errors:
            # Every feed failed. Keep the previous calendar rather than
            # reporting an empty catalogue as though OpenSea had no drops.
            if cached is not None:
                return dict(cached), errors + ["showing the last good calendar"], age
            return {}, errors, 0.0
        _CALENDAR_CACHE.update({
            "cards": cards,
            "errors": errors,
            "fetched_at": time.time(),
        })
        return dict(cards), list(errors), 0.0


def invalidate_calendar():
    """Drop the cached drop calendar so the next scan re-reads OpenSea."""
    with _CALENDAR_LOCK:
        _CALENDAR_CACHE.update({"cards": None, "errors": [], "fetched_at": 0.0})


def chain_coverage(client, api_key, chain_slugs=None, window_hours=None, force=False):
    """Return ``{chain: card_count}`` for networks with drops in the window.

    This is a calendar-only read. It answers "which networks are worth
    scanning right now?" without expanding a single drop detail, so the chain
    picker can be honest instead of listing 27 equally clickable networks.
    """
    cards, errors, age = load_calendar(client, api_key, force=force)
    allowed = _requested_chains(chain_slugs)[0]
    now = int(time.time())
    horizon = now + int((window_hours or config.DISCOVERY_WINDOW_HOURS) * 3600)
    counts = {}
    for (chain, _slug), card in cards.items():
        # Unattributed cards cannot be counted against a network. They are
        # still scanned; they just cannot appear in a per-network total.
        if not chain or (allowed and chain not in allowed):
            continue
        if _card_is_relevant(card, now, horizon):
            counts[chain] = counts.get(chain, 0) + 1
    return counts, errors, age


def _requested_chains(chain_slugs):
    """Return ``(chains, errors)`` for a requested network selection."""
    requested = chain_slugs or config.monitored_chain_slugs()
    chains = []
    errors = []
    for value in requested:
        chain = str(value or "").strip().lower()
        if not chain or chain in chains:
            continue
        if not config.chain_config(chain):
            errors.append(f"{chain}: unsupported EVM signer network")
            continue
        chains.append(chain)
    return chains, errors


def _card_is_relevant(card, now, horizon):
    """Return whether a calendar card can hold a stage inside the window."""
    summaries = _calendar_stages(card)
    if any(_stage_is_relevant(stage, now, horizon) for stage in summaries):
        return True
    if card.get("is_minting") or card.get("isMinting"):
        return True
    drop_type = str(card.get("drop_type") or card.get("dropType") or "").lower()
    feed_types = {str(value or "").lower() for value in (card.get("_feed_types") or [])}
    # A card with no stage summary at all is only worth expanding when the feed
    # says a schedule is still being published. Sampling the discarded cards
    # against /drops/{slug} shows they carry no live stage.
    return not summaries and (drop_type == "upcoming" or "upcoming" in feed_types)


def _merge_card(existing, incoming):
    """Keep the richest version of a calendar card seen across three feeds."""
    merged = dict(existing or {})
    for key, value in (incoming or {}).items():
        if key == "_feed_types":
            merged[key] = sorted(set(merged.get(key) or []) | set(value or []))
            continue
        if value not in (None, "", [], {}):
            merged[key] = value
        elif key not in merged:
            merged[key] = value
    return merged


def _candidate_sort_key(candidate, now):
    end = candidate.end_time if candidate.end_time is not None else 2 ** 62
    if candidate.start_time <= now:
        return (0, end, candidate.chain, candidate.name.lower(), candidate.stage_index)
    return (1, candidate.start_time, candidate.chain, candidate.name.lower(), candidate.stage_index)


def discover_mints(
    client,
    api_key,
    chain_slugs=None,
    window_hours=None,
    today_only=False,
    free_only=False,
    include_ranked_fallback=False,
    force_refresh=False,
):
    """Return the actionable OpenSea drop catalogue for the requested window.

    Discovery uses only the official Drops catalogue. All feeds are read once
    globally, cached, and filtered locally, so scanning a second network is
    local work rather than another multi-page cursor walk. Relevant cards are
    expanded through the official per-drop endpoint so every stage UUID stays
    stable.

    ``today_only`` keeps at least ``DISCOVERY_MIN_WINDOW_HOURS`` of forward
    view. Anchoring the horizon strictly to midnight made a late-evening scan
    return almost nothing, which read as a broken scan rather than a narrow
    window.

    ``include_ranked_fallback`` is accepted for old callers but intentionally
    ignored. Top/trending collections are secondary-market data, not a drop
    index, and were the source of false /scan results.
    """
    del include_ranked_fallback
    chains, errors = _requested_chains(chain_slugs)
    if not chains:
        return [], errors or ["no OpenSea EVM networks are enabled"]

    hours = window_hours or config.DISCOVERY_WINDOW_HOURS
    now = int(time.time())
    horizon = now + int(float(hours) * 3600)
    if today_only:
        _, day_end, _ = config.discovery_day_bounds(now)
        floor = now + int(float(config.DISCOVERY_MIN_WINDOW_HOURS) * 3600)
        horizon = min(horizon, max(day_end, floor))

    cards, feed_errors, _age = load_calendar(client, api_key, force=force_refresh)
    errors.extend(feed_errors)

    allowed = set(chains)
    relevant = {
        key: card
        for key, card in cards.items()
        if (key[0] in allowed or not key[0])
        and _card_is_relevant(card, now, horizon)
    }

    details = {}
    detail_failures = 0

    def load_detail(key, card):
        return key, opensea_client.get_drop_info(
            client, _drop_slug(card), api_key, include_metadata=False
        )

    if relevant:
        workers = max(1, min(16, int(config.DISCOVERY_DETAIL_WORKERS)))
        with ThreadPoolExecutor(
            max_workers=min(workers, len(relevant)),
            thread_name_prefix="opensea-drops",
        ) as executor:
            futures = {
                executor.submit(load_detail, key, card): key
                for key, card in relevant.items()
            }
            for future in as_completed(futures):
                key = futures[future]
                try:
                    loaded_key, info = future.result()
                    details[loaded_key] = info
                except Exception:
                    # The compact official card is still safe to display. Keep
                    # it without dumping transient backend noise into Telegram.
                    detail_failures += 1

    candidates = {}
    for key, card in relevant.items():
        chain, slug = key
        info = details.get(key) or {}
        reported_chain = str(info.get("chain") or "").strip().lower()
        if reported_chain and reported_chain != chain:
            # The detail endpoint is authoritative about which chain a drop
            # settles on. Silently discarding the drop used to hide real mints
            # whenever the calendar and detail records disagreed; adopt the
            # verified chain instead, and only skip when we cannot sign it.
            if reported_chain not in allowed or not config.chain_config(reported_chain):
                continue
            chain = reported_chain
        elif not chain:
            # The feed never said which network this drop is on and the detail
            # read did not either, so there is no RPC to sign against.
            if len(allowed) != 1:
                continue
            chain = next(iter(allowed))
        stages = info.get("stages") or _calendar_stages(card)
        metadata = _calendar_metadata(card)
        if isinstance(info.get("metadata"), dict):
            metadata.update(info["metadata"])
        metadata.update({
            "total_supply": info.get("total_supply", metadata.get("total_supply")),
            "max_supply": info.get("max_supply", metadata.get("max_supply")),
            "drop_type": info.get("drop_type") or card.get("drop_type") or "",
            "is_minting": info.get("is_minting", card.get("is_minting")),
            "details_verified": bool(info),
        })
        drop_candidates = build_drop_candidates(
            info.get("slug") or slug,
            info.get("name") or _drop_name(card, slug),
            chain,
            stages,
            now=now,
            metadata=metadata,
            contract_address=str(
                info.get("contract_address")
                or card.get("contract_address")
                or card.get("contractAddress")
                or ""
            ),
            opensea_url=str(
                info.get("opensea_url")
                or card.get("opensea_url")
                or card.get("openseaUrl")
                or ""
            ),
        )
        for candidate in drop_candidates:
            if candidate.start_time > horizon:
                continue
            if free_only and (
                not candidate.is_free
                or (config.DISCOVERY_PUBLIC_ONLY and not candidate.is_public)
            ):
                continue
            candidates[candidate.key()] = candidate

    if detail_failures:
        errors.append(
            f"{detail_failures} relevant drop detail record(s) used compact fallback"
        )
    return sorted(
        candidates.values(), key=lambda item: _candidate_sort_key(item, now)
    ), errors


def discover_free_mints(client, api_key, chain_slugs=None, window_hours=None):
    """Compatibility wrapper for the original free-only discovery behavior."""
    return discover_mints(
        client,
        api_key,
        chain_slugs=chain_slugs,
        window_hours=window_hours,
        today_only=False,
        free_only=True,
    )
