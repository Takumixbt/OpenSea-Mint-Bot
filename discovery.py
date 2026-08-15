"""Discover OpenSea drop stages and classify their free/access eligibility."""

from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from concurrent.futures import ThreadPoolExecutor, as_completed
import re
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

    def key(self):
        # Keep every scheduled stage visible. The daily runner separately
        # deduplicates execution attempts by collection and chain.
        return f"{self.chain}:{self.slug}:{self.stage_index}"

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
        stages.append({
            "stageIndex": _nested_value(stage, "stageIndex", "stage_index", "index") or position,
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
        r"\b(?:allowlist|whitelist|wl|private|presale|pre-sale|early|"
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
            total_supply=metadata.get("total_supply"),
            max_supply=metadata.get("max_supply"),
            max_per_wallet=max_per_wallet,
        ))
    return sorted(candidates, key=lambda item: (item.start_time, item.stage_index))


def discover_mints(
    client,
    api_key,
    chain_slugs=None,
    window_hours=None,
    today_only=False,
    free_only=False,
    include_ranked_fallback=False,
):
    """Return scheduled OpenSea stages and errors for configured EVM chains.

    ``today_only`` uses the current configured offset calendar day. ``free_only`` is kept for
    the one-drop compatibility route; the Telegram scanner deliberately keeps
    all stages visible and filters execution separately.
    """
    chains = chain_slugs or config.monitored_chain_slugs()
    hours = window_hours or config.DISCOVERY_WINDOW_HOURS
    now = int(time.time())
    if today_only:
        day_start, horizon, _ = config.discovery_day_bounds(now)
    else:
        day_start = None
        horizon = now + int(hours * 60 * 60)
    candidates = {}
    errors = []

    for chain_slug in chains:
        chain_slug = chain_slug.strip().lower()
        chain_info = config.chain_config(chain_slug)
        if not chain_info:
            errors.append(f"{chain_slug}: no configured EVM RPC mapping")
            continue
        seen_slugs = set()
        public_stage_cache = {}
        for drop_type in config.DISCOVERY_DROP_TYPES:
            cursor = None
            seen_cursors = set()
            page_count = 0
            while True:
                page_count += 1
                try:
                    cards, next_cursor = opensea_client.list_drops(
                        client,
                        api_key,
                        chain_slug,
                        drop_type,
                        config.DISCOVERY_LIMIT_PER_CHAIN,
                        cursor,
                    )
                except Exception as exc:
                    errors.append(f"{chain_slug}/{drop_type}: {type(exc).__name__}")
                    break

                page_stage_map = {}
                if today_only and day_start is not None:
                    page_targets = []
                    for card in cards:
                        slug = _drop_slug(card)
                        summaries = _calendar_stages(card)
                        if (
                            slug
                            and slug not in public_stage_cache
                            and _stage_touches_day(summaries, day_start, horizon)
                        ):
                            page_targets.append(slug)
                    if page_targets:
                        with ThreadPoolExecutor(
                            max_workers=min(6, len(page_targets))
                        ) as page_executor:
                            page_futures = {
                                page_executor.submit(
                                    opensea_client.get_public_drop_schedule, client, slug
                                ): slug
                                for slug in page_targets
                            }
                            for future in as_completed(page_futures):
                                try:
                                    stages = future.result()
                                except Exception:
                                    stages = []
                                slug = page_futures[future]
                                page_stage_map[slug] = stages
                                public_stage_cache[slug] = stages

                for card in cards:
                    slug = _drop_slug(card)
                    if not slug:
                        continue
                    seen_slugs.add(slug)
                    stages = _calendar_stages(card)
                    name = _drop_name(card, slug)
                    # The calendar intentionally summarizes only the active
                    # stage and one next stage. Read the collection page for
                    # the complete stage list so later same-day public stages
                    # are not omitted when the detail API is unavailable.
                    page_stages = (
                        page_stage_map.get(slug)
                        or public_stage_cache.get(slug)
                        or []
                    )
                    if page_stages:
                        stages = page_stages
                    # Current calendar responses include active_stage and
                    # next_stage. Only use the detail route for older/partial
                    # response shapes that omit both summaries.
                    if not stages and not _calendar_has_stage_fields(card):
                        try:
                            name, stages = opensea_client.get_drop_schedule(client, slug, api_key)
                        except Exception:
                            # A calendar card with no stage summary is not a
                            # usable mint option. Keep this out of user-facing
                            # scan errors; later feeds may include richer data.
                            continue

                    drop_candidates = build_drop_candidates(
                        slug,
                        _drop_name(card, name),
                        chain_slug,
                        stages,
                        now=now,
                        metadata=_calendar_metadata(card),
                        contract_address=str(
                            card.get("contract_address") or card.get("contractAddress") or ""
                        ),
                        opensea_url=str(
                            card.get("opensea_url") or card.get("openseaUrl") or ""
                        ),
                    )
                    for candidate in drop_candidates:
                        if candidate.start_time > horizon:
                            continue
                        # A useful "today" scan includes stages opening today
                        # and stages that are already live. build_drop_candidates
                        # has already removed stages whose end time has passed.
                        if free_only and (
                            not candidate.is_free
                            or (config.DISCOVERY_PUBLIC_ONLY and not candidate.is_public)
                        ):
                            continue
                        existing = candidates.get(candidate.key())
                        if existing is None or candidate.start_time < existing.start_time:
                            candidates[candidate.key()] = candidate

                    if config.DISCOVERY_REQUEST_DELAY_SECONDS and not stages:
                        time.sleep(config.DISCOVERY_REQUEST_DELAY_SECONDS)

                if not next_cursor or str(next_cursor) in seen_cursors:
                    break
                page_limit = int(config.DISCOVERY_MAX_PAGES_PER_CHAIN or 0)
                if page_limit > 0 and page_count >= page_limit:
                    break
                seen_cursors.add(str(next_cursor))
                cursor = next_cursor

        if include_ranked_fallback:
            # The ranked fallback depends on the individual drop-detail route.
            # OpenSea may temporarily return 503 there while its drop calendar
            # remains healthy. Probe once instead of issuing up to 100 doomed
            # detail requests and making Telegram wait several minutes.
            probe_slug = next(iter(seen_slugs), None)
            if probe_slug:
                if not opensea_client.drop_detail_route_available(
                    client, probe_slug, api_key
                ):
                    continue
            try:
                ranked = opensea_client.list_top_collections(
                    client,
                    api_key,
                    chain_slug,
                    config.DISCOVERY_RANKED_FALLBACK_LIMIT,
                )
            except Exception as exc:
                errors.append(f"{chain_slug}/active-mints: {type(exc).__name__}")
                ranked = []

            ranked_rows = []
            for row in ranked:
                slug = _ranked_collection_slug(row)
                if not slug or slug in seen_slugs:
                    continue
                seen_slugs.add(slug)
                ranked_rows.append((slug, row))

            def inspect_ranked_drop(slug, row):
                try:
                    info = opensea_client.get_drop_info(
                        client, slug, api_key, include_metadata=False
                    )
                except Exception:
                    # Most top collections are secondary-market collections,
                    # not OpenSea drops. A 404 here is expected and not a scan
                    # warning.
                    return []
                if str(info.get("chain") or "").strip().lower() != chain_slug:
                    return []
                metadata = dict(row) if isinstance(row, dict) else {}
                username = str(metadata.get("twitter_username") or "").strip()
                if username and not metadata.get("twitter_url"):
                    metadata["twitter_url"] = f"https://x.com/{username}"
                return build_drop_candidates(
                    info.get("slug") or slug,
                    info.get("name") or _drop_name(row, slug),
                    chain_slug,
                    info.get("stages") or [],
                    now=now,
                    metadata=metadata,
                    contract_address=info.get("contract_address") or "",
                    opensea_url=info.get("opensea_url") or "",
                )

            workers = max(1, min(16, int(config.DISCOVERY_RANKED_FALLBACK_WORKERS)))
            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = [
                    executor.submit(inspect_ranked_drop, slug, row)
                    for slug, row in ranked_rows
                ]
                for future in as_completed(futures):
                    for candidate in future.result():
                        if candidate.start_time > horizon:
                            continue
                        if today_only and day_start is not None and candidate.start_time < day_start:
                            continue
                        if free_only and (
                            not candidate.is_free
                            or (config.DISCOVERY_PUBLIC_ONLY and not candidate.is_public)
                        ):
                            continue
                        existing = candidates.get(candidate.key())
                        if existing is None or candidate.start_time < existing.start_time:
                            candidates[candidate.key()] = candidate

    return sorted(candidates.values(), key=lambda item: (item.start_time, item.chain, item.slug)), errors


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
