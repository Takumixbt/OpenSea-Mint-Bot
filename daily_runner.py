"""OpenSea discovery, daily/one-time scheduling, state, and guarded execution."""

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
from pathlib import Path
import threading
import time
import uuid

from dotenv import load_dotenv
import httpx

import config
import discovery
import external_mint
import marketplace
import opensea_client
from mint_engine import MintEngine
from minter import Minter
from wallets import load_wallet_profiles, select_wallet_profiles


ROOT = Path(__file__).resolve().parent


def redact_secrets(text):
    """Keep API/RPC/private values out of console and Telegram messages."""
    text = str(text)
    for name in (
        "ALCHEMY_API_KEY", "PRIVATE_KEY", "MINT_WALLETS",
        "OPENSEA_API_KEY", "TELEGRAM_BOT_TOKEN",
    ):
        value = os.getenv(name, "")
        if value and len(value) > 8:
            text = text.replace(value, f"<{name} hidden>")
            if value.startswith("0x"):
                text = text.replace(value[2:], f"<{name} hidden>")
    return text


def _truthy(name):
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _int_env(name, fallback):
    try:
        return max(1, int(os.getenv(name, str(fallback))))
    except (TypeError, ValueError):
        return fallback


def _wei_env(name, fallback):
    try:
        value = Decimal(os.getenv(name, str(fallback))) * (10 ** 18)
        if value < 0 or value != value.to_integral_value():
            raise ValueError
        return int(value)
    except (InvalidOperation, TypeError, ValueError):
        return int(Decimal(str(fallback)) * (10 ** 18))


def _configured_chains():
    # Centralize parsing in config so a present-but-blank .env value cannot
    # accidentally remove every network from Telegram's /scan picker.
    return config.monitored_chain_slugs()


def is_free_public_candidate(candidate):
    """Return whether a discovery record is safe for the free-mint route."""
    return (
        isinstance(candidate, dict)
        and candidate.get("is_free") is True
        and candidate.get("is_public") is True
        and candidate.get("price_wei") == 0
    )


def quantity_limit(candidate):
    """Return the maximum quantity allowed by the API/stage metadata."""
    try:
        stage_limit = int(candidate.get("max_per_wallet") or 0)
    except (TypeError, ValueError):
        stage_limit = 0
    return min(100, stage_limit) if stage_limit > 0 else 100


def validate_quantity(candidate, quantity):
    try:
        quantity = int(quantity)
    except (TypeError, ValueError):
        raise ValueError("quantity must be an integer from 1 through 100")
    limit = quantity_limit(candidate)
    if not 1 <= quantity <= limit:
        if limit < 100:
            raise ValueError(f"quantity must be between 1 and {limit} for this stage")
        raise ValueError("quantity must be an integer from 1 through 100")
    return quantity


class DailyMintService:
    def __init__(self, alchemy_key, private_key, wallet_address, opensea_api_key, notify=None):
        load_dotenv(ROOT / ".env")
        self.engine = MintEngine(alchemy_key, private_key, wallet_address, opensea_api_key)
        self.wallet_profiles = load_wallet_profiles(private_key, wallet_address)
        self._wallet_engines = {"primary": self.engine}
        self.purchase_engine = marketplace.PurchaseEngine(alchemy_key, opensea_api_key)
        self.alchemy_key = alchemy_key
        self.private_key = private_key
        self.wallet_address = wallet_address
        self.api_key = opensea_api_key
        self.notify = notify or (lambda message: print(message, flush=True))
        self.notify_result = None
        self.state_path = ROOT / config.DAILY_STATE_FILE
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.schedule_path = ROOT / config.MINT_SCHEDULES_STATE_FILE
        self.stop_event = threading.Event()
        self.schedule_stop_event = threading.Event()
        self.schedule_wakeup = threading.Event()
        self.scan_lock = threading.Lock()
        self.state_lock = threading.RLock()
        self.execution_lock = threading.Lock()
        self.worker = None
        self.schedule_worker = None
        self.mode = None
        self.last_candidates = []
        self.last_errors = []
        self.last_scan_at = None
        self._state = self._load_state()
        self._schedules = self._load_schedules()
        self.metadata_cache = {}
        self.research_cache = {}
        today = self._today()
        if self._state.get("day") != today:
            self._state = self._empty_day_state(today)
            self._save_state()
        else:
            self.last_candidates = list(self._state.get("candidates") or [])
            self.last_scan_at = self._state.get("last_scan_at")
        self._recover_interrupted_schedules()
        self._start_schedule_worker_if_needed()

    @property
    def live_enabled(self):
        return _truthy("ENABLE_LIVE_MINTS")

    @property
    def max_daily_mints(self):
        return _int_env("MAX_DAILY_MINTS", config.MAX_DAILY_MINTS)

    @property
    def max_daily_gas_wei(self):
        return _wei_env("MAX_DAILY_GAS_NATIVE", config.MAX_DAILY_GAS_NATIVE)

    def configured_chains(self):
        """Return the chain slugs currently selected for daily discovery."""
        return _configured_chains()

    def supported_chains(self):
        """Return configured chains that have an EVM signer/RPC mapping."""
        return [slug for slug in self.configured_chains() if config.chain_config(slug)]

    def public_wallets(self):
        """Return labels and addresses only; private keys never enter UI state."""
        return [profile.public() for profile in self.wallet_profiles]

    def selected_wallets(self, candidate):
        return select_wallet_profiles(
            self.wallet_profiles,
            (candidate or {}).get("wallet_ids") or [],
        )

    def funding_snapshot(self, candidate):
        """Return the wallet balance and read-only gas envelope for one mint."""
        candidate = dict(candidate or {})
        chain_slug = str(candidate.get("chain") or "").lower()
        chain = config.chain_config(chain_slug)
        if not chain:
            raise ValueError(f"chain '{chain_slug or 'unknown'}' has no configured RPC")
        quantity = validate_quantity(
            candidate, candidate.get("quantity") or config.MINT_QUANTITY
        )
        if candidate.get("price_wei") is None:
            raise ValueError("mint price is unknown; refresh the OpenSea stage first")
        mint_value_wei = int(candidate["price_wei"]) * quantity
        wallet_checks = []
        for profile in self.selected_wallets(candidate):
            minter = Minter(
                config.rpc_url_for_chain(self.alchemy_key, int(chain["chain_id"])),
                profile.private_key,
                profile.address,
                int(chain["chain_id"]),
                rpc_urls=config.rpc_urls_for_chain(self.alchemy_key, int(chain["chain_id"])),
            )
            check = minter.funding_preview(mint_value_wei)
            check["wallet"] = profile.public()
            wallet_checks.append(check)
        snapshot = {
            key: sum(int(item.get(key) or 0) for item in wallet_checks)
            for key in (
                "balance_wei", "mint_value_wei", "estimated_gas_wei",
                "maximum_gas_wei", "estimated_total_wei", "maximum_total_wei",
                "estimated_shortfall_wei", "maximum_shortfall_wei",
            )
        }
        snapshot.update({
            "chain": chain_slug,
            "native": chain.get("native") or "native coin",
            "quantity": quantity,
            "wallet_count": len(wallet_checks),
            "wallet_checks": wallet_checks,
        })
        return snapshot

    def wallet_snapshot(self, chain_slug=None, max_pages=5, wallet_id="primary"):
        """Read native balances, OpenSea NFT counts, and recent local mint status."""
        profile = select_wallet_profiles(self.wallet_profiles, [wallet_id])[0]
        if chain_slug:
            chain_slug = str(chain_slug).strip().lower()
            if not config.chain_config(chain_slug):
                raise ValueError(f"chain '{chain_slug}' has no configured RPC")
            chains = [chain_slug]
        else:
            chains = self.supported_chains()
        try:
            max_pages = max(1, min(20, int(max_pages)))
        except (TypeError, ValueError):
            max_pages = 5

        def read_chain(slug):
            chain = config.chain_config(slug) or {}
            entry = {
                "chain": slug,
                "native": chain.get("native") or "native",
                "balance_wei": None,
                "nft_count": None,
                "nft_count_capped": False,
                "samples": [],
                "collections": {},
                "contracts": {},
                "nft_source": None,
                "errors": [],
                "notices": [],
            }
            try:
                minter = Minter(
                    config.rpc_url_for_chain(self.alchemy_key, int(chain["chain_id"])),
                    profile.private_key,
                    profile.address,
                    int(chain["chain_id"]),
                    rpc_urls=config.rpc_urls_for_chain(self.alchemy_key, int(chain["chain_id"])),
                )
                live_chain, _ = minter.warm_up()
                if live_chain != int(chain["chain_id"]):
                    raise RuntimeError("RPC chain mismatch")
                entry["balance_wei"] = int(minter.native_balance())
            except Exception as exc:
                entry["errors"].append(f"balance: {type(exc).__name__}: {redact_secrets(exc)}")
            try:
                count = 0
                cursor = None
                samples = []
                collections = {}
                contracts = {}
                client = opensea_client.get_api_client(self.api_key)
                try:
                    for _ in range(max_pages):
                        nfts, cursor = opensea_client.get_account_nfts(
                            client, slug, profile.address, self.api_key,
                            limit=200, cursor=cursor,
                        )
                        count += len(nfts)
                        for nft in nfts:
                            if not isinstance(nft, dict):
                                continue
                            collection = nft.get("collection")
                            if isinstance(collection, dict):
                                collection = collection.get("slug")
                            collection = str(collection or "").strip().lower()
                            if collection:
                                collections[collection] = collections.get(collection, 0) + 1
                            contract = str(nft.get("contract") or "").strip().lower()
                            if contract:
                                contracts[contract] = contracts.get(contract, 0) + 1
                        if len(samples) < 3:
                            samples.extend(nfts[: 3 - len(samples)])
                        if not cursor:
                            break
                    entry["nft_count"] = count
                    entry["nft_count_capped"] = bool(cursor)
                    entry["samples"] = samples
                    entry["collections"] = collections
                    entry["contracts"] = contracts
                    entry["nft_source"] = "OpenSea"
                finally:
                    client.close()
            except Exception as exc:
                opensea_error = f"{type(exc).__name__}: {redact_secrets(exc)}"
                try:
                    rpc_url = config.rpc_url_for_chain(
                        self.alchemy_key, int(chain["chain_id"])
                    )
                    prefix, separator, key = rpc_url.rpartition("/v2/")
                    if not separator or not key:
                        raise RuntimeError("Alchemy NFT endpoint is unavailable")
                    response = httpx.get(
                        f"{prefix}/nft/v3/{key}/getNFTsForOwner",
                        params={
                            "owner": profile.address,
                            "withMetadata": "true",
                            "pageSize": 100,
                        },
                        timeout=15.0,
                    )
                    response.raise_for_status()
                    payload = response.json()
                    owned = payload.get("ownedNfts") or []
                    total = payload.get("totalCount")
                    if total is None or not isinstance(owned, list):
                        raise RuntimeError("Alchemy returned an invalid NFT response")
                    collections = {}
                    contracts = {}
                    for nft in owned:
                        contract = (nft or {}).get("contract") or {}
                        contract_address = str(contract.get("address") or "").strip().lower()
                        if contract_address:
                            contracts[contract_address] = contracts.get(contract_address, 0) + 1
                        metadata = contract.get("openSeaMetadata") or {}
                        collection = str(
                            metadata.get("collectionSlug")
                            or metadata.get("collectionName")
                            or ""
                        ).strip().lower()
                        if collection:
                            collections[collection] = collections.get(collection, 0) + 1
                    entry["nft_count"] = int(total)
                    entry["nft_count_capped"] = False
                    entry["samples"] = owned[:3]
                    entry["collections"] = collections
                    entry["contracts"] = contracts
                    entry["nft_source"] = "Alchemy fallback"
                    entry["notices"].append(
                        "OpenSea NFT index was unavailable; count shown from Alchemy."
                    )
                except Exception as fallback_exc:
                    entry["errors"].append(
                        f"NFTs unavailable: OpenSea {opensea_error}; "
                        f"Alchemy {type(fallback_exc).__name__}: {redact_secrets(fallback_exc)}"
                    )
            return entry

        results = []
        if chains:
            with ThreadPoolExecutor(
                max_workers=min(6, len(chains)), thread_name_prefix="wallet-status"
            ) as pool:
                futures = {pool.submit(read_chain, slug): slug for slug in chains}
                for future in as_completed(futures):
                    results.append(future.result())
            order = {slug: index for index, slug in enumerate(chains)}
            results.sort(key=lambda item: order.get(item["chain"], 999))
        recent_mints = self.mint_history(limit=8)
        if chain_slug:
            recent_mints = [
                item for item in recent_mints
                if str(item.get("chain") or "").lower() == chain_slug
            ]
        chain_results = {item["chain"]: item for item in results}
        for record in recent_mints:
            slug = str(record.get("slug") or "").lower()
            contract = str(record.get("contract_address") or "").lower()
            chain_result = chain_results.get(str(record.get("chain") or "").lower())
            if (
                (slug or contract) and chain_result
                and chain_result.get("nft_count") is not None
            ):
                owned_count = 0
                if contract:
                    owned_count = int((chain_result.get("contracts") or {}).get(contract, 0))
                if not owned_count and chain_result.get("nft_source") == "OpenSea" and slug:
                    owned_count = int((chain_result.get("collections") or {}).get(slug, 0))
                record["indexed_owned_count"] = owned_count
                record["ownership_source"] = chain_result.get("nft_source")
                record["ownership_scan_capped"] = bool(chain_result.get("nft_count_capped"))
        return {
            "address": profile.address,
            "wallet": profile.public(),
            "available_wallets": self.public_wallets(),
            "chains": results,
            "recent_mints": recent_mints,
            "selected_chain": chain_slug,
            "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }

    def mint_history(self, limit=10):
        """Return recent local execution records without exposing any secret."""
        records = []
        with self.state_lock:
            for key, item in (self._state.get("results") or {}).items():
                record = dict(item or {})
                record.setdefault("candidate_key", key)
                records.append(record)
            for schedule in self._schedules:
                result = schedule.get("result") or {}
                if not result and schedule.get("status") not in {"failed", "completed"}:
                    continue
                candidate = schedule.get("candidate") or {}
                record = dict(result)
                record.update({
                    "name": candidate.get("name") or candidate.get("slug"),
                    "slug": candidate.get("slug"),
                    "contract_address": candidate.get("contract_address"),
                    "chain": candidate.get("chain"),
                    "quantity": candidate.get("quantity") or config.MINT_QUANTITY,
                    "at": schedule.get("updated_at"),
                    "status": result.get("status") or schedule.get("status"),
                    "error": schedule.get("error"),
                })
                records.append(record)
        records.sort(key=lambda item: str(item.get("at") or ""), reverse=True)
        unique = []
        seen = set()
        for record in records:
            identity = record.get("tx_hash") or ":".join(str(record.get(key) or "") for key in (
                "chain", "slug", "status", "at"
            ))
            if identity in seen:
                continue
            seen.add(identity)
            unique.append(record)
        return unique[: max(1, int(limit))]

    def status_snapshot(self):
        """Return UI-safe runner facts without exposing wallet or API secrets."""
        day = self._today()
        with self.state_lock:
            results = self._day_state(day).get("results", {})
            candidate_count = len(self.last_candidates)
            project_count = len({
                (str(item.get("chain") or "").lower(), str(item.get("slug") or "").lower())
                for item in self.last_candidates
                if item.get("slug")
            })
            last_scan_at = self.last_scan_at
            last_error_count = len(self.last_errors)
            active_schedules = [
                item for item in self._schedules
                if item.get("status") in {"armed", "running"}
            ]
            next_schedule = min(
                active_schedules,
                key=lambda item: int(item.get("run_at") or 0),
                default=None,
            )
        worker = self.worker
        worker_alive = bool(worker and worker.is_alive())
        schedule_worker = self.schedule_worker
        return {
            "mode": self.mode or "stopped",
            "live_enabled": self.live_enabled,
            "wallet_count": len(self.wallet_profiles),
            "chains": self.supported_chains(),
            "invalid_chains": [slug for slug in self.configured_chains() if not config.chain_config(slug)],
            "candidate_count": candidate_count,
            "project_count": project_count,
            "free_candidate_count": sum(
                1 for candidate in self.last_candidates
                if is_free_public_candidate(candidate)
            ),
            "last_scan_at": last_scan_at,
            "last_error_count": last_error_count,
            "attempt_count": len(results),
            "max_daily_mints": self.max_daily_mints,
            "daily_gas_cap": os.getenv("MAX_DAILY_GAS_NATIVE", config.MAX_DAILY_GAS_NATIVE),
            "worker_alive": worker_alive,
            "stop_requested": worker_alive and self.stop_event.is_set(),
            "schedule_count": len(active_schedules),
            "schedule_worker_alive": bool(schedule_worker and schedule_worker.is_alive()),
            "next_schedule_at": next_schedule.get("run_at") if next_schedule else None,
            "next_schedule_name": (
                (next_schedule.get("candidate") or {}).get("name")
                if next_schedule else None
            ),
        }

    def status_text(self):
        snapshot = self.status_snapshot()
        return (
            f"Mode: {snapshot['mode']}\n"
            f"Live switch: {'enabled' if snapshot['live_enabled'] else 'disabled'}\n"
            f"Chains: {', '.join(snapshot['chains'])}\n"
            f"Projects found today: {snapshot['project_count']}\n"
            f"Mint options found today: {snapshot['candidate_count']}\n"
            f"Free and public: {snapshot['free_candidate_count']}\n"
            f"Last scan: {snapshot['last_scan_at'] or 'never'}\n"
            f"Today's attempts: {snapshot['attempt_count']}/{snapshot['max_daily_mints']}\n"
            f"Daily gas cap: {snapshot['daily_gas_cap']} native\n"
            f"Armed schedules: {snapshot['schedule_count']}"
        )

    def scan_now(self, chain_slug=None, force_refresh=False):
        """Scan configured chains, or one requested chain, immediately.

        A single-network scan replaces only that network's rows in the saved
        result so switching networks does not discard what the previous scan
        already found.
        """
        if chain_slug and chain_slug != "all":
            chain_slug = str(chain_slug).strip().lower()
            if chain_slug not in _configured_chains():
                raise ValueError(f"chain '{chain_slug}' is not enabled in MONITORED_CHAINS")
            if not config.chain_config(chain_slug):
                raise ValueError(f"chain '{chain_slug}' has no configured EVM RPC mapping")
            chains = [chain_slug]
        else:
            chain_slug = None
            chains = _configured_chains()
        with self.scan_lock:
            client = opensea_client.get_api_client(self.api_key)
            try:
                candidates, errors = discovery.discover_mints(
                    client,
                    self.api_key,
                    chains,
                    config.DISCOVERY_WINDOW_HOURS,
                    today_only=True,
                    # The official three drop feeds are the canonical mint
                    # catalogue. Trending/top collections are trading data and
                    # must never leak into /scan as false mint results.
                    include_ranked_fallback=False,
                    force_refresh=force_refresh,
                )
            finally:
                client.close()
        scanned = [candidate.to_dict() for candidate in candidates]
        with self.state_lock:
            if chain_slug:
                kept = [
                    row for row in (self.last_candidates or [])
                    if str(row.get("chain") or "").strip().lower() != chain_slug
                ]
                self.last_candidates = kept + scanned
            else:
                self.last_candidates = scanned
            self.last_errors = list(errors)
            self.last_scan_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
            self._save_candidates(self.last_candidates)
            return list(scanned), list(self.last_errors)

    def chain_coverage(self, force_refresh=False):
        """Return ``(counts, errors, age_seconds)`` of drops per network.

        This reads only OpenSea's drop calendar, so the network picker can show
        which networks actually have something without expanding any drop.
        """
        client = opensea_client.get_api_client(self.api_key)
        try:
            return discovery.chain_coverage(
                client,
                self.api_key,
                _configured_chains(),
                config.DISCOVERY_WINDOW_HOURS,
                force=force_refresh,
            )
        finally:
            client.close()

    def _hosted_candidates_for_slug(self, client, slug):
        """Load calendar/detail stages without assuming the detail route works."""
        slug = str(slug or "").strip()
        info = None
        detail_error = None
        try:
            info = opensea_client.get_drop_info(
                client, slug, self.api_key, include_metadata=False
            )
        except Exception as exc:
            detail_error = exc
            # A transient detail outage should not make a known calendar drop
            # impossible to arm. The compact Drops feed is still a bounded,
            # official OpenSea source and contains enough stage data to show a
            # candidate while the final transaction is fetched later.
            for drop_type in config.DISCOVERY_DROP_TYPES:
                cursor = None
                seen_cursors = set()
                page_count = 0
                try:
                    while True:
                        page_count += 1
                        cards, next_cursor = opensea_client.list_drops(
                            client,
                            self.api_key,
                            "",
                            drop_type,
                            config.DISCOVERY_LIMIT_PER_CHAIN,
                            cursor,
                        )
                        card = next((
                            row for row in cards
                            if str(discovery._drop_slug(row) or "").lower() == slug.lower()
                        ), None)
                        if card:
                            stages = discovery._calendar_stages(card)
                            chain = str(card.get("chain") or "").strip().lower()
                            if stages and chain:
                                metadata = discovery._calendar_metadata(card)
                                metadata.update({
                                    "drop_type": card.get("drop_type") or "",
                                    "is_minting": card.get("is_minting"),
                                    "details_verified": False,
                                })
                                info = {
                                    "slug": slug,
                                    "name": discovery._drop_name(card, slug),
                                    "chain": chain,
                                    "contract_address": card.get("contract_address") or "",
                                    "opensea_url": card.get("opensea_url") or f"https://opensea.io/collection/{slug}",
                                    "metadata": metadata,
                                    "stages": stages,
                                }
                                break
                        if not next_cursor or str(next_cursor) in seen_cursors:
                            break
                        page_limit = int(config.DISCOVERY_MAX_PAGES_PER_CHAIN or 0)
                        if page_limit > 0 and page_count >= page_limit:
                            break
                        seen_cursors.add(str(next_cursor))
                        cursor = next_cursor
                except Exception as fallback_error:
                    detail_error = fallback_error
                if info:
                    break

        if not isinstance(info, dict):
            return [], None, detail_error
        chain = str(info.get("chain") or "").strip().lower()
        if not chain:
            return [], info, RuntimeError("OpenSea did not return a network for this collection")
        metadata = dict(info.get("metadata") or {})
        metadata.update({
            "total_supply": info.get("total_supply", metadata.get("total_supply")),
            "max_supply": info.get("max_supply", metadata.get("max_supply")),
            "drop_type": info.get("drop_type") or metadata.get("drop_type") or "",
            "is_minting": info.get("is_minting", metadata.get("is_minting")),
            "details_verified": metadata.get("details_verified", True),
        })
        try:
            candidates = discovery.build_drop_candidates(
                info.get("slug") or slug,
                info.get("name") or slug,
                chain,
                info.get("stages") or [],
                now=int(time.time()),
                metadata=metadata,
                contract_address=info.get("contract_address") or "",
                opensea_url=info.get("opensea_url") or "",
            )
        except ValueError:
            return [], info, RuntimeError(
                f"OpenSea reports network '{chain}', but it is not an EVM signer network"
            )
        return [candidate.to_dict() for candidate in candidates], info, detail_error

    @staticmethod
    def _synthetic_asset_slug(reference):
        """Create a stable internal key when OpenSea omits the collection slug."""
        contract = str(reference.get("contract_address") or "").lower().replace("0x", "")
        identifier = str(reference.get("identifier") or "")
        safe_identifier = "".join(
            char if char.isalnum() else "-" for char in identifier
        ).strip("-") or "token"
        return f"asset-{contract[:12] or 'contract'}-{safe_identifier[:32]}"

    def _collection_for_reference(self, client, reference, slug, nft=None):
        """Merge collection metadata with an asset record and its URL identity."""
        collection = {}
        if slug:
            try:
                value = opensea_client.get_collection_details(
                    client, slug, self.api_key
                )
                if isinstance(value, dict):
                    collection = dict(value)
            except Exception:
                collection = {}
        nft = nft if isinstance(nft, dict) else {}
        for key in (
            "name", "description", "image_url", "display_image_url",
            "project_url", "mint_url", "twitter_url", "discord_url",
            "telegram_url", "wiki_url", "opensea_url",
        ):
            if not collection.get(key) and nft.get(key):
                collection[key] = nft.get(key)
        if not collection.get("name"):
            collection["name"] = str(nft.get("collection_name") or slug or "OpenSea asset")
        if not collection.get("opensea_url"):
            collection["opensea_url"] = reference.get("url") or (
                f"https://opensea.io/collection/{slug}" if slug else ""
            )
        contracts = collection.get("contracts")
        contract = str(reference.get("contract_address") or "").strip()
        chain = str(reference.get("chain") or "").strip().lower()
        has_reference_contract = any(
            isinstance(item, dict)
            and str(item.get("chain") or "").strip().lower() == chain
            and str(item.get("address") or "").strip().lower() == contract.lower()
            for item in (contracts if isinstance(contracts, list) else [])
        )
        if contract and chain and not has_reference_contract:
            collection["contracts"] = list(contracts) if isinstance(contracts, list) else []
            collection["contracts"].append({"chain": chain, "address": contract})
        return collection

    @staticmethod
    def _decorate_reference_candidate(candidate, reference, source_url, nft=None):
        result = dict(candidate or {})
        result["source_url"] = source_url
        result["reference_kind"] = reference.get("kind")
        result.setdefault("is_sold_out", False)
        if reference.get("kind") == "asset":
            # Keep the exact pasted asset URL on the candidate so a scheduled
            # refresh resolves the same NFT again instead of silently switching
            # to a similarly named collection.
            result["asset_url"] = source_url
            result["token_id"] = reference.get("identifier")
            result["asset_contract_address"] = reference.get("contract_address")
            result["opensea_url"] = source_url
            result["url"] = source_url
            nft = nft if isinstance(nft, dict) else {}
            if nft.get("name") and not result.get("name"):
                result["name"] = nft["name"]
        return result

    def _resolve_opensea_reference(self, value):
        """Resolve an OpenSea URL into hosted or safely inferred mint routes."""
        reference = opensea_client.parse_opensea_reference(value)
        source_url = reference.get("url") or str(value or "").strip()
        nft = {}
        slug = str(reference.get("slug") or "").strip()
        hosted_error = None

        # API work is serialized with the existing scan lock. The slower
        # verified-ABI/RPC resolver runs after the client is closed so a single
        # broken external contract cannot block normal OpenSea scans.
        with self.scan_lock:
            client = opensea_client.get_api_client(self.api_key)
            try:
                if reference.get("kind") == "asset":
                    try:
                        nft = opensea_client.get_nft(
                            client,
                            reference.get("chain"),
                            reference.get("contract_address"),
                            reference.get("identifier"),
                            self.api_key,
                        )
                    except Exception as exc:
                        raise RuntimeError(
                            "could not resolve the OpenSea asset metadata; "
                            "check the OpenSea API key and asset URL"
                        ) from exc
                    slug = self._nft_collection_slug(nft)
                hosted_candidates = []
                hosted_info = None
                if slug:
                    hosted_candidates, hosted_info, hosted_error = (
                        self._hosted_candidates_for_slug(client, slug)
                    )
                if hosted_candidates:
                    decorated = [
                        self._decorate_reference_candidate(
                            candidate, reference, source_url, nft
                        )
                        for candidate in hosted_candidates
                    ]
                    return {
                        "reference": reference,
                        "source_url": source_url,
                        "slug": slug,
                        "nft": nft,
                        "collection": {},
                        "info": hosted_info or {},
                        "candidates": decorated,
                        "route_note": "Resolved from the OpenSea Drops API.",
                        "hosted_error": hosted_error,
                    }
                collection = self._collection_for_reference(
                    client, reference, slug, nft
                )
                # A drop detail can expose the NFT contract even when the
                # stage list is empty or already ended. Preserve that signal
                # for the external-route fallback instead of requiring a
                # second collection response to repeat it.
                if isinstance(hosted_info, dict):
                    if not collection.get("name") and hosted_info.get("name"):
                        collection["name"] = hosted_info["name"]
                    detail_contract = str(
                        hosted_info.get("contract_address") or ""
                    ).strip()
                    detail_chain = str(
                        hosted_info.get("chain")
                        or reference.get("chain")
                        or ""
                    ).strip().lower()
                    if detail_contract and detail_chain and not collection.get("contracts"):
                        collection["contracts"] = [{
                            "chain": detail_chain,
                            "address": detail_contract,
                        }]
            finally:
                client.close()

        if not slug and reference.get("kind") == "asset":
            slug = self._synthetic_asset_slug(reference)
        if not collection.get("contracts"):
            route_note = (
                "OpenSea returned no supported EVM collection contract for this link. "
                "It cannot be turned into safe mint calldata."
            )
            return {
                "reference": reference,
                "source_url": source_url,
                "slug": slug,
                "nft": nft,
                "collection": collection,
                "info": {},
                "candidates": [],
                "route_note": route_note,
                "hosted_error": hosted_error,
            }

        try:
            external_candidate, route_note = external_mint.resolve_collection_mint(
                collection,
                slug,
                self.alchemy_key,
                self.wallet_address,
                chain_hint=(reference.get("chain") if reference.get("kind") == "asset" else None),
            )
        except Exception as exc:
            external_candidate = None
            route_note = (
                "OpenSea resolved the link, but the verified on-chain mint check "
                f"could not complete ({type(exc).__name__})."
            )
        if external_candidate:
            candidate = self._decorate_reference_candidate(
                external_candidate, reference, source_url, nft
            )
            candidate["route_note"] = route_note
            return {
                "reference": reference,
                "source_url": source_url,
                "slug": slug,
                "nft": nft,
                "collection": collection,
                "info": {},
                "candidates": [candidate],
                "route_note": route_note,
                "hosted_error": hosted_error,
            }

        if reference.get("kind") == "asset":
            route_note = (
                f"OpenSea resolved this asset to {slug or 'a collection'}, but no "
                "active/upcoming OpenSea stage or safe verified mint route was found. "
                "A marketplace listing is not a mint transaction, so nothing was guessed."
            )
        else:
            route_note = (
                "OpenSea recognizes this collection, but it exposes no active/upcoming "
                "mint stage and no safe verified on-chain mint route was found."
            )
        return {
            "reference": reference,
            "source_url": source_url,
            "slug": slug,
            "nft": nft,
            "collection": collection,
            "info": {},
            "candidates": [],
            "route_note": route_note,
            "hosted_error": hosted_error,
        }

    def inspect_drop(self, value):
        """Return mintable stages for any OpenSea collection, drop, or asset URL.

        An OpenSea URL is an input reference, not proof that a mint exists.
        Resolution prefers OpenSea's own stage data, then the existing direct
        SeaDrop and verified-ABI routes. Unknown marketplace links are refused
        instead of becoming guessed transactions.
        """
        context = self._resolve_opensea_reference(value)
        candidates = context.get("candidates") or []
        if candidates:
            return [dict(candidate) for candidate in candidates]
        raise RuntimeError(context.get("route_note") or "no safe OpenSea mint route was found")

    def refresh_candidate(self, candidate, require_same_terms=True):
        """Refresh one stage or verified external route before arming/sending."""
        candidate = dict(candidate or {})
        if not candidate.get("slug") or not candidate.get("chain"):
            raise ValueError("candidate data is incomplete; inspect the OpenSea drop again")
        quantity = validate_quantity(
            candidate, candidate.get("quantity") or config.MINT_QUANTITY
        )
        fresh_stages = self.inspect_drop(
            candidate.get("opensea_url") or candidate.get("url") or candidate.get("slug")
        )
        fresh = next(
            (dict(item) for item in fresh_stages if same_candidate_stage(candidate, item)),
            None,
        )
        if fresh is None:
            raise RuntimeError(
                "the selected OpenSea mint stage or verified route changed; "
                "reopen the OpenSea link and confirm it again"
            )
        if fresh.get("is_sold_out") is True:
            raise RuntimeError("this OpenSea drop sold out; nothing was signed or sent")
        if require_same_terms:
            old_route = str(candidate.get("route") or "opensea_drop").strip().lower()
            new_route = str(fresh.get("route") or "opensea_drop").strip().lower()
            if old_route != new_route:
                raise RuntimeError(
                    "the mint route changed; reopen the OpenSea link and review it again"
                )
            if fresh.get("price_wei") != candidate.get("price_wei"):
                raise RuntimeError(
                    "the OpenSea mint price changed; reopen the drop and approve the new total"
                )
            if fresh.get("access_label") != candidate.get("access_label"):
                raise RuntimeError(
                    "the OpenSea eligibility rule changed; reopen the drop and review it again"
                )
            old_contract = str(candidate.get("contract_address") or "").lower()
            new_contract = str(fresh.get("contract_address") or "").lower()
            if old_contract and new_contract and old_contract != new_contract:
                raise RuntimeError(
                    "the OpenSea drop contract changed; reopen the drop and review it again"
                )
        fresh["quantity"] = validate_quantity(fresh, quantity)
        if candidate.get("wallet_ids"):
            fresh["wallet_ids"] = list(candidate["wallet_ids"])
        # Asset references and generic ABI bindings are part of the selected
        # route identity. Preserve them for older API responses that return a
        # valid stage but omit one of the non-calendar fields.
        for key in (
            "source_url", "asset_url", "reference_kind", "token_id",
            "asset_contract_address", "generic_function_abi",
            "generic_arg_bindings", "generic_price_function",
            "generic_start_function",
        ):
            if candidate.get(key) is not None and fresh.get(key) is None:
                fresh[key] = candidate[key]
        for key in (
            "description", "image_url", "banner_image_url", "project_url",
            "twitter_url", "discord_url", "telegram_url", "wiki_url",
        ):
            if not fresh.get(key) and candidate.get(key):
                fresh[key] = candidate[key]
        return fresh

    def enrich_candidate(self, candidate):
        """Lazy-load collection metadata for a Telegram candidate detail card."""
        if not isinstance(candidate, dict) or not candidate.get("slug"):
            raise ValueError("candidate data is incomplete; run a fresh scan")
        if candidate.get("description") or candidate.get("metadata_loaded"):
            return dict(candidate)
        slug = str(candidate["slug"])
        with self.state_lock:
            cached = getattr(self, "metadata_cache", {}).get(slug)
        if cached is None:
            client = opensea_client.get_api_client(self.api_key)
            try:
                metadata = opensea_client.get_collection_details(client, slug, self.api_key)
            finally:
                client.close()
            metadata = metadata if isinstance(metadata, dict) else {}
            metadata = {
                "description": str(metadata.get("description") or ""),
                "image_url": str(metadata.get("image_url") or ""),
                "banner_image_url": str(metadata.get("banner_image_url") or ""),
                "project_url": str(metadata.get("project_url") or ""),
                "mint_url": str(metadata.get("mint_url") or ""),
                "twitter_url": str(
                    metadata.get("twitter_url")
                    or (
                        f"https://x.com/{metadata.get('twitter_username')}"
                        if metadata.get("twitter_username") else ""
                    )
                ),
                "discord_url": str(metadata.get("discord_url") or ""),
                "telegram_url": str(metadata.get("telegram_url") or ""),
                "wiki_url": str(metadata.get("wiki_url") or ""),
                "total_supply": metadata.get("total_supply"),
                "max_supply": metadata.get("max_supply"),
                "contracts": metadata.get("contracts") or [],
                "opensea_url": str(metadata.get("opensea_url") or ""),
            }
            with self.state_lock:
                if not hasattr(self, "metadata_cache"):
                    self.metadata_cache = {}
                self.metadata_cache[slug] = metadata
                cached = metadata
        enriched = dict(candidate)
        for key, value in cached.items():
            if key != "contracts" and value not in (None, ""):
                enriched[key] = value
        enriched["metadata_loaded"] = True
        if not enriched.get("contract_address"):
            contracts = cached.get("contracts") or []
            for contract in contracts if isinstance(contracts, list) else []:
                if not isinstance(contract, dict):
                    continue
                if str(contract.get("chain") or "").lower() != str(candidate.get("chain") or "").lower():
                    continue
                enriched["contract_address"] = str(contract.get("address") or "")
                break
            if not enriched.get("contract_address"):
                enriched["contract_address"] = str(candidate.get("contract_address") or "")
        # Preserve the enriched record for future buttons and restarts, while
        # keeping the stable candidate token unchanged.
        with self.state_lock:
            for index, saved in enumerate(self.last_candidates):
                if candidate_key(saved) == candidate_key(candidate):
                    self.last_candidates[index] = dict(enriched)
                    self._save_candidates(self.last_candidates)
                    break
        return enriched

    def research_candidate(self, candidate):
        """Return concise official drop info for one saved mint stage."""
        if not isinstance(candidate, dict) or not candidate.get("slug"):
            raise ValueError("candidate data is incomplete; run a fresh scan")
        result = self.research_reference(
            candidate.get("source_url")
            or candidate.get("opensea_url")
            or candidate.get("url")
            or candidate.get("slug")
        )
        matched = next((
            item for item in result.get("mint_candidates", [])
            if same_candidate_stage(candidate, item)
        ), None)
        if matched:
            result["candidate"] = dict(matched)
        return result

    def research_reference(self, value):
        """Return mint information for any pasted OpenSea URL or slug."""
        reference = opensea_client.parse_opensea_reference(value)
        if reference.get("kind") == "asset":
            return self._research_from_resolved_context(
                self._resolve_opensea_reference(value)
            )
        slug = reference.get("slug")
        try:
            return self._drop_research(slug)
        except RuntimeError:
            # Collections that are not in the calendar can still be useful if
            # a live public SeaDrop or a verified simple mint route exists.
            return self._research_from_resolved_context(
                self._resolve_opensea_reference(value)
            )

    def _research_from_resolved_context(self, context):
        """Build a Telegram-friendly research record from URL resolution."""
        context = context if isinstance(context, dict) else {}
        reference = context.get("reference") or {}
        nft = context.get("nft") if isinstance(context.get("nft"), dict) else {}
        collection = (
            context.get("collection")
            if isinstance(context.get("collection"), dict) else {}
        )
        info = context.get("info") if isinstance(context.get("info"), dict) else {}
        candidates = [
            dict(item) for item in (context.get("candidates") or [])
            if isinstance(item, dict)
        ]
        slug = str(context.get("slug") or "").strip()
        chain = str(
            reference.get("chain")
            or info.get("chain")
            or (candidates[0].get("chain") if candidates else "")
            or ""
        ).strip().lower()
        result = {}
        result.update(dict(info.get("metadata") or {}))
        for source in (collection, info, nft):
            for key in (
                "description", "image_url", "banner_image_url", "project_url",
                "mint_url", "twitter_url", "discord_url", "telegram_url",
                "wiki_url", "total_supply", "max_supply",
            ):
                if result.get(key) in (None, "") and source.get(key) not in (None, ""):
                    result[key] = source.get(key)
        name = str(
            nft.get("name")
            or collection.get("name")
            or info.get("name")
            or slug
            or "OpenSea reference"
        )
        opensea_url = str(
            reference.get("url")
            or info.get("opensea_url")
            or collection.get("opensea_url")
            or (f"https://opensea.io/collection/{slug}" if slug else "")
        )
        contract = str(
            reference.get("contract_address")
            or info.get("contract_address")
            or (candidates[0].get("contract_address") if candidates else "")
            or ""
        )
        result.update({
            "slug": slug,
            "name": name,
            "chain": chain,
            "contract_address": contract,
            "opensea_url": opensea_url,
            "total_supply": result.get("total_supply"),
            "max_supply": result.get("max_supply"),
            "is_minting": info.get("is_minting"),
            "drop_type": info.get("drop_type") or "",
            "mint_candidates": candidates,
            "mint_route_note": (
                ""
                if candidates
                else context.get("route_note")
                or "No safe mint route is currently exposed for this OpenSea reference."
            ),
            "source": (
                "OpenSea NFT metadata + verified mint route"
                if reference.get("kind") == "asset"
                else "OpenSea public metadata + verified mint route"
            ),
        })
        if reference.get("kind") == "asset":
            result.update({
                "token_id": reference.get("identifier"),
                "asset_url": reference.get("url"),
                "collection_slug": self._nft_collection_slug(nft),
            })
        try:
            result["is_sold_out"] = (
                int(result.get("max_supply") or 0) > 0
                and int(result.get("total_supply") or 0) >= int(result.get("max_supply") or 0)
            )
        except (TypeError, ValueError):
            result["is_sold_out"] = any(
                item.get("is_sold_out") is True for item in candidates
            )
        if len(candidates) == 1:
            result["candidate"] = dict(candidates[0])
        return result

    def _drop_research(self, slug):
        """Load one official Drops record without marketplace/statistics noise."""
        slug = opensea_client.parse_drop_slug(slug)
        client = opensea_client.get_api_client(self.api_key)
        try:
            try:
                info = opensea_client.get_drop_info(client, slug, self.api_key)
            except Exception as exc:
                raise RuntimeError(
                    "OpenSea does not expose this collection as a hosted mint."
                ) from exc
        finally:
            client.close()
        chain = str(info.get("chain") or "").strip().lower()
        metadata = dict(info.get("metadata") or {})
        metadata.update({
            "total_supply": info.get("total_supply", metadata.get("total_supply")),
            "max_supply": info.get("max_supply", metadata.get("max_supply")),
            "drop_type": info.get("drop_type") or "",
            "is_minting": info.get("is_minting"),
            "details_verified": True,
        })
        candidates = []
        if config.chain_config(chain):
            candidates = [item.to_dict() for item in discovery.build_drop_candidates(
                info.get("slug") or slug,
                info.get("name") or slug,
                chain,
                info.get("stages") or [],
                now=int(time.time()),
                metadata=metadata,
                contract_address=info.get("contract_address") or "",
                opensea_url=info.get("opensea_url") or "",
            )]
        result = dict(metadata)
        result.update({
            "slug": info.get("slug") or slug,
            "name": info.get("name") or slug,
            "chain": chain,
            "contract_address": info.get("contract_address") or "",
            "opensea_url": info.get("opensea_url") or f"https://opensea.io/collection/{slug}",
            "total_supply": info.get("total_supply"),
            "max_supply": info.get("max_supply"),
            "is_minting": info.get("is_minting"),
            "drop_type": info.get("drop_type") or "",
            "mint_candidates": candidates,
            "mint_route_note": (
                "" if candidates
                else "No active or upcoming stage is currently exposed by OpenSea."
            ),
            "source": "OpenSea Drops API",
        })
        try:
            result["is_sold_out"] = (
                int(result.get("max_supply") or 0) > 0
                and int(result.get("total_supply") or 0) >= int(result["max_supply"])
            )
        except (TypeError, ValueError):
            result["is_sold_out"] = any(
                item.get("is_sold_out") is True for item in candidates
            )
        if len(candidates) == 1:
            result["candidate"] = dict(candidates[0])
        return result

    def purchase_preview(self, value):
        """Return the exact cheapest active OpenSea listing without signing."""
        slug = opensea_client.parse_drop_slug(value)
        client = opensea_client.get_api_client(self.api_key)
        try:
            return marketplace.best_listing_preview(
                client, slug, self.api_key, self.wallet_address
            )
        finally:
            client.close()

    def buy_listing(self, preview, wallet_id="primary"):
        """Fulfill one explicitly confirmed listing with one selected wallet."""
        if not self.live_enabled:
            raise RuntimeError("live transactions are disabled; set ENABLE_LIVE_MINTS=true first")
        profiles = select_wallet_profiles(self.wallet_profiles, [wallet_id])
        profile = profiles[0]
        preview = dict(preview or {})
        key = f"buy:{preview.get('chain')}:{preview.get('order_hash')}:{profile.id}"
        with self.execution_lock:
            with self.state_lock:
                results = self._day_state(self._today()).setdefault("results", {})
                if key in results:
                    raise RuntimeError("this exact listing was already attempted with this wallet today")
            try:
                result = self.purchase_engine.execute(preview, profile)
            except Exception as exc:
                with self.state_lock:
                    self._day_state(self._today()).setdefault("results", {})[key] = {
                        "action": "buy",
                        "chain": preview.get("chain"),
                        "name": preview.get("name") or preview.get("slug") or "OpenSea purchase",
                        "slug": preview.get("slug"),
                        "contract_address": preview.get("contract_address"),
                        "token_id": preview.get("token_id"),
                        "wallet": profile.public(),
                        "status": "failed",
                        "error": f"{type(exc).__name__}: {redact_secrets(exc)}",
                        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    }
                    self._save_state()
                raise
            saved = {
                "action": "buy",
                "chain": preview.get("chain"),
                "name": preview.get("name") or preview.get("slug") or "OpenSea purchase",
                "slug": preview.get("slug"),
                "contract_address": preview.get("contract_address"),
                "token_id": preview.get("token_id"),
                "wallet": profile.public(),
                "quantity": 1,
                "mint_value_wei": result.get("summary", {}).get("value_wei", 0),
                "gas_wei": result.get("summary", {}).get("worst_case_fee_wei", 0),
                "actual_gas_wei": result.get("actual_gas_wei"),
                "status": (
                    "confirmed" if result.get("confirmed") is True
                    else "reverted" if result.get("confirmed") is False
                    else "sent"
                ),
                "tx_hash": result.get("tx_hash"),
                "confirmed": result.get("confirmed"),
                "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
            with self.state_lock:
                self._day_state(self._today()).setdefault("results", {})[key] = saved
                self._save_state()
            return result

    def research_collection(self, slug, chain_hint=None):
        """Collect bounded, public OpenSea metadata for a collection."""
        slug = str(slug or "").strip()
        if not slug or any(char.isspace() for char in slug) or "/" in slug:
            raise ValueError("the OpenSea collection slug is invalid")
        cache_key = slug.lower()
        now = time.time()
        with self.state_lock:
            cached = self.research_cache.get(cache_key)
            # Mint progress changes quickly after a stage opens. Keep this
            # cache short so a research card cannot show five-minute-old
            # supply while still avoiding duplicate requests from button taps.
            if cached and now - float(cached.get("cached_at", 0)) < 30:
                return dict(cached.get("value") or {})

        with self.scan_lock:
            client = opensea_client.get_api_client(self.api_key)
            try:
                collection = opensea_client.get_collection_details(client, slug, self.api_key)
                stats = self._optional_research_call(
                    opensea_client.get_collection_stats, client, slug
                )
                floors = self._optional_research_call(
                    opensea_client.get_collection_floor_prices, client, slug
                )
                nfts_payload = self._optional_research_call(
                    opensea_client.get_collection_nfts, client, slug, limit=3
                )
                try:
                    public_drop = opensea_client.get_public_drop_info(client, slug)
                except Exception:
                    public_drop = {}
                profiles = {}
                identifiers = []
                owner = collection.get("owner") if isinstance(collection, dict) else None
                owner_identifier = self._account_identifier(owner)
                if owner_identifier:
                    identifiers.append(owner_identifier)
                editors = collection.get("editors") if isinstance(collection, dict) else []
                if isinstance(editors, list):
                    identifiers.extend(
                        identifier for identifier in (
                            self._account_identifier(item) for item in editors
                        ) if identifier
                    )
                for identifier in identifiers[:3]:
                    try:
                        profiles[identifier] = opensea_client.get_account_profile(
                            client, identifier, self.api_key
                        )
                    except Exception:
                        continue
            finally:
                client.close()

        collection = collection if isinstance(collection, dict) else {}
        public_drop = public_drop if isinstance(public_drop, dict) else {}
        contracts = collection.get("contracts") or []
        contract_address = self._contract_for_chain(contracts, chain_hint)
        twitter_username = str(collection.get("twitter_username") or "").strip()
        twitter_url = str(
            collection.get("twitter_url")
            or (f"https://x.com/{twitter_username}" if twitter_username else "")
        )
        stats_total = stats.get("total") if isinstance(stats, dict) else {}
        if not isinstance(stats_total, dict):
            stats_total = {}
        stats_currency = str(stats_total.get("floor_price_symbol") or "").strip().upper()
        intervals = stats.get("intervals") if isinstance(stats, dict) else {}
        if isinstance(intervals, list):
            interval_map = {
                str(item.get("interval") or "").strip().lower(): item
                for item in intervals
                if isinstance(item, dict)
            }
        elif isinstance(intervals, dict):
            interval_map = intervals
        else:
            interval_map = {}
        one_day = interval_map.get("one_day") or interval_map.get("1d") or {}
        if not isinstance(one_day, dict):
            one_day = {}
        floor_rows = floors.get("floor_prices") if isinstance(floors, dict) else []
        if not isinstance(floor_rows, list):
            floor_rows = []
        latest_floor = max(
            (row for row in floor_rows if isinstance(row, dict)),
            key=lambda row: self._safe_epoch(row.get("time")),
            default={},
        )
        sample_nfts = nfts_payload[0] if isinstance(nfts_payload, tuple) else []
        if not isinstance(sample_nfts, list):
            sample_nfts = []
        owner_value = self._account_identifier(collection.get("owner"))
        editor_values = [
            identifier for identifier in (
                self._account_identifier(item) for item in (collection.get("editors") or [])
            ) if identifier
        ] if isinstance(collection.get("editors") or [], list) else []
        research_chain = str(chain_hint or "").strip().lower()
        if not research_chain:
            for contract in contracts if isinstance(contracts, list) else []:
                if isinstance(contract, dict) and contract.get("chain"):
                    research_chain = str(contract.get("chain")).strip().lower()
                    break
        if not stats_currency:
            chain_settings = config.CHAIN_CONFIGS.get(research_chain) or {}
            stats_currency = str(chain_settings.get("native") or "").strip().upper()
        live_total_supply = public_drop.get("total_supply")
        total_supply = (
            live_total_supply
            if live_total_supply is not None
            else collection.get("total_supply")
        )
        value = {
            "slug": slug,
            "name": str(collection.get("name") or slug),
            "chain": research_chain,
            "description": str(collection.get("description") or ""),
            "image_url": str(collection.get("image_url") or ""),
            "banner_image_url": str(collection.get("banner_image_url") or ""),
            "project_url": str(collection.get("project_url") or ""),
            "mint_url": str(collection.get("mint_url") or ""),
            "twitter_username": twitter_username,
            "twitter_url": twitter_url,
            "instagram_username": str(collection.get("instagram_username") or ""),
            "discord_url": str(collection.get("discord_url") or ""),
            "telegram_url": str(collection.get("telegram_url") or ""),
            "wiki_url": str(collection.get("wiki_url") or ""),
            "opensea_url": str(
                collection.get("opensea_url")
                or f"https://opensea.io/collection/{slug}"
            ),
            "category": str(collection.get("category") or ""),
            "created_date": str(collection.get("created_date") or ""),
            "safelist_status": str(collection.get("safelist_status") or ""),
            "is_disabled": bool(collection.get("is_disabled")),
            "is_nsfw": bool(collection.get("is_nsfw")),
            "total_supply": total_supply,
            "max_supply": public_drop.get("max_supply"),
            "supply_source": "live_drop" if live_total_supply is not None else "collection_index",
            "unique_item_count": collection.get("unique_item_count"),
            "owner": owner_value,
            "editors": editor_values,
            "owner_profiles": profiles,
            "contracts": contracts if isinstance(contracts, list) else [],
            "contract_address": contract_address,
            "stats_total": stats_total,
            "stats_one_day": one_day,
            "stats_currency": stats_currency,
            "latest_floor": latest_floor,
            "sample_nfts": sample_nfts[:3],
            "source": "OpenSea public collection metadata and statistics",
            "developer_note": (
                "Owner/editor wallets are OpenSea attributions, not proof of the "
                "smart-contract deployer or project developer."
            ),
        }
        with self.state_lock:
            self.research_cache[cache_key] = {"cached_at": now, "value": dict(value)}
        return value

    def _optional_research_call(self, function, client, *args, **kwargs):
        try:
            # The helper is only used by ``research_collection`` where the
            # service API key is the final documented client argument.
            return function(client, *args, self.api_key, **kwargs)
        except Exception:
            return {}

    @staticmethod
    def _contract_for_chain(contracts, chain_hint=None):
        if not isinstance(contracts, list):
            return ""
        hint = str(chain_hint or "").strip().lower()
        for contract in contracts:
            if not isinstance(contract, dict):
                continue
            if hint and str(contract.get("chain") or "").lower() != hint:
                continue
            address = str(contract.get("address") or "").strip()
            if address:
                return address
        return ""

    @staticmethod
    def _account_identifier(value):
        if isinstance(value, dict):
            value = value.get("address") or value.get("username") or value.get("user")
        return str(value or "").strip()

    @staticmethod
    def _safe_epoch(value):
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _nft_collection_slug(nft):
        if not isinstance(nft, dict):
            return ""
        collection = nft.get("collection")
        if isinstance(collection, dict):
            collection = (
                collection.get("slug")
                or collection.get("collection_slug")
                or collection.get("collectionSlug")
            )
        return str(
            collection
            or nft.get("collection_slug")
            or nft.get("collectionSlug")
            or ""
        ).strip()

    @staticmethod
    def _minimal_asset_research(reference, nft):
        nft = nft if isinstance(nft, dict) else {}
        return {
            "slug": "",
            "name": str(nft.get("name") or "NFT asset"),
            "description": str(nft.get("description") or ""),
            "image_url": str(nft.get("image_url") or nft.get("display_image_url") or ""),
            "opensea_url": str(nft.get("opensea_url") or reference.get("url") or ""),
            "contract_address": str(reference.get("contract_address") or ""),
            "source": "OpenSea public NFT metadata",
            "developer_note": "No collection slug was supplied by OpenSea for this asset.",
        }

    def add_schedule(self, candidate):
        """Persist and arm one one-time mint schedule."""
        if not isinstance(candidate, dict) or not candidate.get("chain") or not candidate.get("slug"):
            raise ValueError("schedule data is incomplete; inspect the OpenSea URL again")
        if not self.live_enabled:
            raise RuntimeError("live minting is disabled; set ENABLE_LIVE_MINTS=true first")
        if candidate.get("is_sold_out") is True:
            raise RuntimeError("this OpenSea drop is sold out; a schedule was not armed")
        candidate = self.refresh_candidate(candidate, require_same_terms=True)
        # Resolve aliases now so a typo or removed wallet can never create an
        # apparently armed schedule that has no signer at launch.
        self.selected_wallets(candidate)
        if candidate.get("price_wei") is not None:
            try:
                quantity = validate_quantity(
                    candidate, candidate.get("quantity") or config.MINT_QUANTITY
                )
                known_price_over_cap = (
                    int(candidate["price_wei"]) * quantity > config.MAX_MINT_VALUE_WEI
                )
            except (TypeError, ValueError):
                known_price_over_cap = False
            if known_price_over_cap:
                raise RuntimeError(
                    "this schedule's known mint price exceeds MAX_MINT_PRICE_NATIVE; "
                    "raise the cap deliberately before arming live mode"
                )
        chain = str(candidate.get("chain")).strip().lower()
        if not config.chain_config(chain):
            raise ValueError(f"chain '{chain}' has no configured EVM RPC mapping")
        quantity = validate_quantity(
            candidate, candidate.get("quantity") or config.MINT_QUANTITY
        )
        try:
            run_at = int(candidate.get("start_time") or 0)
        except (TypeError, ValueError):
            run_at = 0
        if run_at <= 0:
            raise ValueError("OpenSea did not provide a valid mint opening time")
        end_at = candidate.get("end_time")
        if end_at is not None:
            try:
                if int(end_at) < int(time.time()):
                    raise ValueError("this mint stage has already ended")
            except (TypeError, ValueError) as exc:
                if str(exc) == "this mint stage has already ended":
                    raise
        candidate = dict(candidate)
        candidate["chain"] = chain
        candidate["quantity"] = quantity
        schedule = {
            "id": f"sch_{uuid.uuid4().hex[:10]}",
            "status": "armed",
            "mode": "live",
            "run_at": run_at,
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "candidate": candidate,
        }
        with self.state_lock:
            wanted_key = candidate_key(candidate)
            for existing in self._schedules:
                if existing.get("status") not in {"armed", "running"}:
                    continue
                existing_candidate = existing.get("candidate") or {}
                if candidate_key(existing_candidate) == wanted_key:
                    raise RuntimeError("this drop already has an armed schedule")
            self._schedules.append(schedule)
            self._save_schedules()
        self._start_schedule_worker_if_needed()
        self.schedule_wakeup.set()
        return dict(schedule)

    def schedules(self, include_finished=True):
        """Return a stable, newest-first copy of local one-time schedules."""
        with self.state_lock:
            items = [dict(item) for item in self._schedules]
        if not include_finished:
            items = [item for item in items if item.get("status") in {"armed", "running"}]
        return sorted(
            items,
            key=lambda item: (int(item.get("run_at") or 0), item.get("id", "")),
        )

    def schedule_by_id(self, schedule_id):
        schedule_id = str(schedule_id or "")
        with self.state_lock:
            for item in self._schedules:
                if item.get("id") == schedule_id:
                    return dict(item)
        raise ValueError("schedule not found or expired")

    def cancel_schedule(self, schedule_id):
        schedule_id = str(schedule_id or "")
        with self.state_lock:
            for item in self._schedules:
                if item.get("id") != schedule_id:
                    continue
                if item.get("status") in {"completed", "failed", "cancelled"}:
                    raise RuntimeError(f"schedule is already {item.get('status')}")
                item["status"] = "cancelled"
                item["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
                self._save_schedules()
                self.schedule_wakeup.set()
                return dict(item)
        raise ValueError("schedule not found or expired")

    def start_daily(self):
        if not self.live_enabled:
            raise RuntimeError("live minting is disabled; set ENABLE_LIVE_MINTS=true first")
        if self.worker and self.worker.is_alive():
            if self.stop_event.is_set():
                raise RuntimeError("daily runner is stopping; wait for it to finish before restarting")
            raise RuntimeError("automatic minting is already running")
        self.stop_event.clear()
        self.mode = "live"
        self.worker = threading.Thread(target=self._daily_loop, name="daily-mints", daemon=True)
        self.worker.start()

    def stop(self):
        """Stop the broad daily runner; individual schedules remain armed."""
        self.stop_event.set()
        if not (self.worker and self.worker.is_alive()):
            self.mode = None

    def shutdown(self):
        """Stop background workers during process shutdown."""
        self.stop()
        self.schedule_stop_event.set()
        self.schedule_wakeup.set()

    def _start_schedule_worker_if_needed(self):
        with self.state_lock:
            armed = any(
                item.get("status") in {"armed", "running"}
                for item in self._schedules
            )
        if not armed or self.schedule_stop_event.is_set():
            return
        if self.schedule_worker and self.schedule_worker.is_alive():
            return
        self.schedule_worker = threading.Thread(
            target=self._schedule_loop,
            name="mint-schedules",
            daemon=True,
        )
        self.schedule_worker.start()

    def _schedule_loop(self):
        """Start warm-up before launch, then fire at the exact scheduled second."""
        while not self.schedule_stop_event.is_set():
            due = None
            next_run = None
            now = time.time()
            with self.state_lock:
                for item in self._schedules:
                    if item.get("status") != "armed":
                        continue
                    run_at = int(item.get("run_at") or 0)
                    warm_at = max(0, run_at - config.WARMUP_LEAD_SECONDS)
                    if warm_at <= now:
                        item["status"] = "running"
                        item["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
                        self._save_schedules()
                        due = dict(item)
                        break
                    if next_run is None or warm_at < next_run:
                        next_run = warm_at

            if due is not None:
                self._run_schedule(due)
                continue

            wait_seconds = config.SCHEDULE_POLL_SECONDS
            if next_run is not None:
                wait_seconds = min(wait_seconds, max(0.01, next_run - time.time()))
            self.schedule_wakeup.wait(wait_seconds)
            self.schedule_wakeup.clear()

    def _run_schedule(self, schedule):
        candidate = dict(schedule.get("candidate") or {})
        try:
            # OpenSea can reorder stages or move an opening after a schedule is
            # armed. Refresh by the stable stage UUID at warm-up so the stored
            # index can never select a different stage and a delayed opening is
            # automatically re-armed instead of firing stale calldata.
            candidate = self.refresh_candidate(candidate, require_same_terms=True)
            run_at = int(candidate.get("start_time") or 0)
            if run_at <= 0:
                raise RuntimeError("OpenSea no longer exposes a valid opening time")

            rearmed = False
            with self.state_lock:
                stored = self._find_schedule_locked(schedule.get("id"))
                if not stored or stored.get("status") != "running":
                    # The user may have cancelled while the OpenSea refresh was
                    # in flight. Never execute a schedule that is no longer live.
                    return
                stored["candidate"] = dict(candidate)
                stored["run_at"] = run_at
                stored["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
                if run_at - config.WARMUP_LEAD_SECONDS > time.time():
                    stored["status"] = "armed"
                    rearmed = True
                self._save_schedules()

            schedule = dict(schedule)
            schedule["candidate"] = dict(candidate)
            schedule["run_at"] = run_at
            if rearmed:
                self.notify(
                    f"SCHEDULE UPDATED: {candidate.get('name', candidate.get('slug', 'mint'))} "
                    f"now opens at {datetime.fromtimestamp(run_at, timezone.utc).isoformat(timespec='seconds')}."
                )
                self.schedule_wakeup.set()
                return

            result = self._execute_candidate(
                candidate,
                self._today(),
                scheduled_at=float(run_at),
            )
        except Exception as exc:
            error = f"{type(exc).__name__}: {redact_secrets(exc)}"
            recorded = False
            with self.state_lock:
                stored = self._find_schedule_locked(schedule.get("id"))
                if stored and stored.get("status") == "running":
                    stored["status"] = "failed"
                    stored["error"] = error
                    stored["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
                    self._save_schedules()
                    recorded = True
            if not recorded:
                return
            self.notify(
                f"SCHEDULE FAILED: {candidate.get('name', candidate.get('slug', 'mint'))} — {error}"
            )
            return

        result_summary = {
            "status": (
                "confirmed" if result.get("confirmed") is True
                else "reverted" if result.get("confirmed") is False
                else "sent" if result.get("tx_hash")
                else result.get("status")
            ),
            "tx_hash": result.get("tx_hash"),
            "confirmed": result.get("confirmed"),
            "launch_delay_ms": result.get("launch_delay_ms"),
            "mint_value_wei": result.get("summary", {}).get("value_wei", 0),
            "gas_wei": result.get("worst_case_gas_wei", 0),
            "actual_gas_wei": result.get("actual_gas_wei"),
            "execution_route": result.get("execution_route"),
            "broadcast_rpc_count": result.get("broadcast_rpc_count"),
            "wallet_results": [
                {
                    "wallet": item.get("wallet"),
                    "status": item.get("status"),
                    "tx_hash": item.get("tx_hash"),
                    "confirmed": item.get("confirmed"),
                    "error": item.get("error"),
                }
                for item in result.get("wallet_results", [])
            ],
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        with self.state_lock:
            stored = self._find_schedule_locked(schedule.get("id"))
            if stored:
                stored["status"] = "completed"
                stored["result"] = result_summary
                stored["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
                self._save_schedules()
        if callable(self.notify_result):
            self.notify_result(schedule, result)
        else:
            self.notify(self._schedule_result_message(schedule, result))

    def _schedule_result_message(self, schedule, result):
        candidate = schedule.get("candidate") or {}
        name = candidate.get("name", candidate.get("slug", "mint"))
        speed = (
            f" Broadcast delay: {result['launch_delay_ms']} ms."
            if result.get("launch_delay_ms") is not None else ""
        )
        if result.get("tx_hash") and result.get("confirmed") is True:
            return f"SCHEDULE LIVE: {name} confirmed.{speed} Tx: {result['tx_hash']}"
        if result.get("tx_hash"):
            return f"SCHEDULE LIVE: {name} was sent but did not confirm.{speed} Tx: {result['tx_hash']}"
        return f"SCHEDULE LIVE: {name} finished without a transaction hash."

    def _find_schedule_locked(self, schedule_id):
        for item in self._schedules:
            if item.get("id") == schedule_id:
                return item
        return None

    def _recover_interrupted_schedules(self):
        changed = False
        with self.state_lock:
            for item in self._schedules:
                if item.get("mode") != "live" and item.get("status") in {"armed", "running"}:
                    item["status"] = "cancelled"
                    item["error"] = "legacy non-live schedule was cancelled during the live-only upgrade"
                    item["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
                    changed = True
                    continue
                if item.get("status") != "running":
                    continue
                item["status"] = "failed"
                item["error"] = (
                    "previous process stopped while this schedule was running; "
                    "inspect the wallet before scheduling it again"
                )
                item["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
                changed = True
            if changed:
                self._save_schedules()

    def candidate_at(self, index):
        """Return a stable copy of a saved candidate for a UI action."""
        with self.state_lock:
            if index < 1 or index > len(self.last_candidates):
                raise ValueError("candidate number is out of range; run /scan first")
            return dict(self.last_candidates[index - 1])

    def mint_index(self, index, quantity=None):
        if not self.live_enabled:
            raise RuntimeError("live minting is disabled; set ENABLE_LIVE_MINTS=true first")
        candidate = self.candidate_at(index)
        if quantity is not None:
            candidate["quantity"] = quantity
        return self.mint_candidate(candidate)

    def mint_candidate(self, candidate, quantity=None):
        """Execute a stable candidate snapshot, preventing index drift after rescans."""
        if not isinstance(candidate, dict) or not candidate.get("chain") or not candidate.get("slug"):
            raise ValueError("candidate data is incomplete; run a fresh scan")
        if not self.live_enabled:
            raise RuntimeError("live minting is disabled; set ENABLE_LIVE_MINTS=true first")
        candidate = dict(candidate)
        if candidate.get("is_sold_out") is True:
            raise RuntimeError("this OpenSea drop is sold out; nothing was signed or sent")
        if quantity is not None:
            candidate["quantity"] = quantity
        candidate["quantity"] = validate_quantity(
            candidate, candidate.get("quantity") or config.MINT_QUANTITY
        )
        candidate = self.refresh_candidate(candidate, require_same_terms=True)
        return self._execute_candidate(candidate, self._today())

    def _engine_for_wallet(self, profile):
        if profile.id == "primary":
            return self.engine
        engine = self._wallet_engines.get(profile.id)
        if engine is None:
            engine = MintEngine(
                self.alchemy_key,
                profile.private_key,
                profile.address,
                self.api_key,
            )
            self._wallet_engines[profile.id] = engine
        return engine

    def _execute_wallet_batch(
        self, candidate, gas_used, quantity, scheduled_at=None
    ):
        profiles = self.selected_wallets(candidate)
        batch_cap = self.max_daily_gas_wei
        if batch_cap > 0:
            remaining = max(0, batch_cap - int(gas_used))
            share = remaining // len(profiles)
            if share <= 0:
                raise RuntimeError("daily gas cap has no remaining multi-wallet allowance")
            per_wallet_cap = int(gas_used) + share
        else:
            per_wallet_cap = 0

        def execute_one(profile):
            result = self._engine_for_wallet(profile).execute(
                candidate,
                daily_gas_used_wei=gas_used,
                # Divide the remaining daily envelope before any parallel
                # broadcast so all wallets combined cannot exceed the cap.
                daily_gas_cap_wei=per_wallet_cap,
                quantity=quantity,
                scheduled_at=scheduled_at,
            )
            result["wallet"] = profile.public()
            return result

        if len(profiles) == 1:
            result = execute_one(profiles[0])
            result["wallet_results"] = [dict(result)]
            return result

        successes = []
        failures = []
        # Every wallet has independent nonce space. Parallel preparation and
        # broadcast keeps a multi-wallet launch within the same block window.
        with ThreadPoolExecutor(
            max_workers=min(len(profiles), 10), thread_name_prefix="wallet-mint"
        ) as pool:
            futures = {pool.submit(execute_one, profile): profile for profile in profiles}
            for future in as_completed(futures):
                profile = futures[future]
                try:
                    successes.append(future.result())
                except Exception as exc:
                    failures.append({
                        "wallet": profile.public(),
                        "status": "failed",
                        "error": f"{type(exc).__name__}: {redact_secrets(exc)}",
                    })
        order = {profile.id: index for index, profile in enumerate(profiles)}
        wallet_results = sorted(
            successes + failures,
            key=lambda item: order.get((item.get("wallet") or {}).get("id"), 999),
        )
        if not successes:
            details = "; ".join(
                f"{item['wallet']['label']}: {item['error']}" for item in failures
            )
            raise RuntimeError(f"all selected wallets failed: {details}")
        confirmed_values = [item.get("confirmed") for item in successes]
        result = {
            "candidate": candidate,
            "status": "partial" if failures else "sent",
            "wallet_results": wallet_results,
            "tx_hashes": [item.get("tx_hash") for item in successes if item.get("tx_hash")],
            "tx_hash": next((item.get("tx_hash") for item in successes if item.get("tx_hash")), None),
            "confirmed": (
                True if confirmed_values and all(value is True for value in confirmed_values)
                else False if any(value is False for value in confirmed_values)
                else None
            ),
            "worst_case_gas_wei": sum(
                int(item.get("worst_case_gas_wei") or 0) for item in successes
            ),
            "actual_gas_wei": sum(
                int(item.get("actual_gas_wei") or 0) for item in successes
            ),
            "execution_route": (
                "direct_seadrop"
                if successes and all(item.get("execution_route") == "direct_seadrop" for item in successes)
                else "mixed" if any(item.get("execution_route") == "direct_seadrop" for item in successes)
                else "opensea_drop"
            ),
            "broadcast_rpc_count": max(
                int(item.get("broadcast_rpc_count") or 0) for item in successes
            ),
            "summary": {
                "value_wei": sum(
                    int((item.get("summary") or {}).get("value_wei") or 0)
                    for item in successes
                )
            },
        }
        delays = [
            int(item["launch_delay_ms"])
            for item in successes if item.get("launch_delay_ms") is not None
        ]
        if delays:
            result["launch_delay_ms"] = max(delays)
        return result

    def _daily_loop(self):
        try:
            while not self.stop_event.is_set():
                candidates, errors = self.scan_now()
                self.notify(self._scan_message(candidates, errors))
                self._process_candidates(candidates)
                self.stop_event.wait(config.DAILY_SCAN_INTERVAL_SECONDS)
        except Exception as exc:
            self.notify(f"Daily runner stopped: {type(exc).__name__}: {redact_secrets(exc)}")
        finally:
            self.mode = None

    def _process_candidates(self, candidates):
        for candidate in candidates:
            if self.stop_event.is_set():
                return
            # Broad automatic mode stays free + public. Paid or gated mints
            # remain fully available through explicit manual scheduling where
            # the user reviews the exact price/access terms first.
            if not is_free_public_candidate(candidate) or candidate.get("is_sold_out") is True:
                continue
            day = self._today()
            key = candidate_key(candidate)
            with self.state_lock:
                state = self._day_state(day)
                results = state.setdefault("results", {})
                already_attempted = key in results
                at_limit = len(results) >= self.max_daily_mints
            if already_attempted:
                continue
            if at_limit:
                self.notify("Daily mint limit reached; remaining candidates were not attempted.")
                return
            try:
                while True:
                    start = int(candidate.get("start_time") or 0)
                    warm_at = max(0, start - config.WARMUP_LEAD_SECONDS)
                    while warm_at > time.time() and not self.stop_event.is_set():
                        self.stop_event.wait(
                            min(60, max(0.01, warm_at - time.time()))
                        )
                    if self.stop_event.is_set():
                        return
                    candidate = self.refresh_candidate(
                        candidate, require_same_terms=True
                    )
                    if not is_free_public_candidate(candidate):
                        raise RuntimeError(
                            "the automatic candidate is no longer free and public"
                        )
                    refreshed_start = int(candidate.get("start_time") or 0)
                    if refreshed_start - config.WARMUP_LEAD_SECONDS <= time.time():
                        start = refreshed_start
                        break
                day = self._today()
                result = self._execute_candidate(candidate, day, scheduled_at=start)
                if callable(self.notify_result):
                    self.notify_result(None, result)
                else:
                    self.notify(self._result_message(result))
            except Exception as exc:
                failure_day = self._today()
                with self.state_lock:
                    failure_state = self._day_state(failure_day)
                    failure_results = failure_state.setdefault("results", {})
                    if key in failure_results:
                        # A manual Telegram action may have completed while
                        # this worker was preparing the same candidate. Keep
                        # the existing success/sent record intact.
                        already_recorded = True
                    else:
                        already_recorded = False
                        failure_results[key] = {
                            "chain": candidate.get("chain"),
                            "status": "failed",
                            "error": f"{type(exc).__name__}: {redact_secrets(exc)}",
                            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                        }
                        self._save_state()
                if already_recorded:
                    self.notify(
                        f"Skipped {candidate.get('name', candidate.get('slug'))}: "
                        "another action already recorded an attempt."
                    )
                else:
                    self.notify(
                        f"Skipped {candidate.get('name', candidate.get('slug'))}: "
                        f"{type(exc).__name__}: {redact_secrets(exc)}"
                    )

    def _execute_candidate(self, candidate, day, scheduled_at=None):
        # A manual Telegram action and the daily worker share one wallet and
        # nonce space. Serialize the complete build/send path so they cannot
        # both pass the duplicate check and race with the same nonce.
        day = self._today()
        with self.execution_lock:
            with self.state_lock:
                state = self._day_state(day)
                results = state.setdefault("results", {})
                key = candidate_key(candidate)
                existing = results.get(key)
                if existing:
                    raise RuntimeError("this candidate was already attempted today")
                if len(results) >= self.max_daily_mints:
                    raise RuntimeError("daily mint limit reached")
                gas_used = sum(
                    int(item.get("gas_wei", 0))
                    for item in results.values()
                    if item.get("chain") == candidate.get("chain")
                )
            try:
                result = self._execute_wallet_batch(
                    candidate,
                    gas_used,
                    validate_quantity(
                        candidate, candidate.get("quantity") or config.MINT_QUANTITY
                    ),
                    scheduled_at,
                )
            except Exception as exc:
                # Record every failed/uncertain execution attempt before the
                # error leaves this method. This prevents a fast second click
                # from reusing the same candidate after an ambiguous RPC error.
                with self.state_lock:
                    state = self._day_state(self._today())
                    state.setdefault("results", {})[key] = {
                        "chain": candidate.get("chain"),
                        "name": candidate.get("name") or candidate.get("slug"),
                        "slug": candidate.get("slug"),
                        "contract_address": candidate.get("contract_address"),
                        "quantity": candidate.get("quantity") or config.MINT_QUANTITY,
                        "status": "failed",
                        "error": f"{type(exc).__name__}: {redact_secrets(exc)}",
                        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    }
                    self._save_state()
                raise
            result_state = {
                "chain": candidate.get("chain"),
                "name": candidate.get("name") or candidate.get("slug"),
                "slug": candidate.get("slug"),
                "contract_address": candidate.get("contract_address"),
                "quantity": candidate.get("quantity") or config.MINT_QUANTITY,
                "mint_value_wei": result.get("summary", {}).get("value_wei", 0),
                "status": (
                    "confirmed" if result.get("tx_hash") and result.get("confirmed") is True
                    else "reverted" if result.get("tx_hash") and result.get("confirmed") is False
                    else "sent" if result.get("tx_hash")
                    else result["status"]
                ),
                "gas_wei": result.get("worst_case_gas_wei", 0),
                "actual_gas_wei": result.get("actual_gas_wei"),
                "execution_route": result.get("execution_route"),
                "broadcast_rpc_count": result.get("broadcast_rpc_count"),
                "wallet_results": [
                    {
                        "wallet": item.get("wallet"),
                        "status": (
                            "confirmed" if item.get("confirmed") is True
                            else "reverted" if item.get("confirmed") is False
                            else item.get("status", "sent")
                        ),
                        "tx_hash": item.get("tx_hash"),
                        "error": item.get("error"),
                    }
                    for item in result.get("wallet_results", [])
                ],
                "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
            if result.get("tx_hash"):
                result_state["tx_hash"] = result["tx_hash"]
                result_state["confirmed"] = result.get("confirmed")
            with self.state_lock:
                state = self._day_state(self._today())
                results = state.setdefault("results", {})
                if key in results:
                    raise RuntimeError("this candidate was attempted while the transaction was being prepared")
                results[key] = result_state
                self._save_state()
            return result

    def _scan_message(self, candidates, errors):
        if not candidates:
            text = "No OpenSea mint stages found for today (configured timezone)."
        else:
            free_count = sum(1 for candidate in candidates if is_free_public_candidate(candidate))
            lines = [
                f"Found {len(candidates)} OpenSea mint stage(s) for today (configured timezone).",
                f"Free/public candidates: {free_count}",
            ]
            for index, candidate in enumerate(candidates, 1):
                when = datetime.fromtimestamp(
                    candidate["start_time"], config.discovery_timezone()
                ).strftime("%Y-%m-%d %H:%M %z")
                lines.append(
                    f"{index}. {candidate['name']} | {candidate['chain']} | "
                    f"{candidate.get('price_display', 'Price unknown')} | "
                    f"{candidate.get('access_label', candidate.get('stage_label', 'Unknown'))} | "
                    f"{when} | {candidate['url']}"
                )
            text = "\n".join(lines)
        if errors:
            text += "\n\nSkipped checks: " + ", ".join(errors[:8])
        return text

    def _result_message(self, result):
        candidate = result["candidate"]
        if result.get("tx_hash") and result.get("confirmed") is True:
            return f"LIVE: {candidate['name']} confirmed. Tx: {result['tx_hash']}"
        if result.get("tx_hash"):
            return f"LIVE: {candidate['name']} was sent but did not confirm successfully. Tx: {result['tx_hash']}"
        return f"LIVE: {candidate['name']} finished without a transaction hash."

    def _today(self):
        return config.discovery_day_bounds()[2]

    def _day_state(self, day):
        with self.state_lock:
            if self._state.get("day") != day:
                self._state = self._empty_day_state(day)
                self.last_candidates = []
                self.last_errors = []
                self.last_scan_at = None
                self._save_state()
            return self._state

    @staticmethod
    def _empty_day_state(day):
        return {"day": day, "results": {}, "candidates": [], "last_scan_at": None}

    def _load_state(self):
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return self._empty_day_state(self._today())
            if not isinstance(data.get("results"), dict):
                data["results"] = {}
            if not isinstance(data.get("candidates"), list):
                data["candidates"] = []
            return data
        except (OSError, ValueError):
            return self._empty_day_state(self._today())

    def _load_schedules(self):
        try:
            data = json.loads(self.schedule_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        if isinstance(data, dict):
            data = data.get("schedules")
        if not isinstance(data, list):
            return []
        return [item for item in data if isinstance(item, dict) and item.get("id")]

    def _save_candidates(self, candidates):
        state = self._day_state(self._today())
        state["candidates"] = candidates
        state["last_scan_at"] = self.last_scan_at
        self._save_state()

    def _save_state(self):
        with self.state_lock:
            temporary = self.state_path.with_suffix(".tmp")
            temporary.write_text(json.dumps(self._state, indent=2), encoding="utf-8")
            temporary.replace(self.state_path)

    def _save_schedules(self):
        with self.state_lock:
            temporary = self.schedule_path.with_suffix(".tmp")
            temporary.write_text(
                json.dumps({"schedules": self._schedules}, indent=2),
                encoding="utf-8",
            )
            temporary.replace(self.schedule_path)


def candidate_stage_identity(candidate):
    """Return OpenSea's stable stage UUID, with a legacy-record fallback."""
    candidate = candidate if isinstance(candidate, dict) else {}
    stage_id = str(candidate.get("stage_id") or "").strip().lower()
    if stage_id:
        return f"uuid:{stage_id}"
    return "legacy:" + ":".join(str(value) for value in (
        candidate.get("stage_index", 0),
        candidate.get("start_time", 0),
        candidate.get("stage_type", ""),
    ))


def same_candidate_stage(left, right):
    """Match the same stage/route across refreshed OpenSea response shapes."""
    if not isinstance(left, dict) or not isinstance(right, dict):
        return False
    if str(left.get("chain") or "").lower() != str(right.get("chain") or "").lower():
        return False
    if str(left.get("slug") or "").lower() != str(right.get("slug") or "").lower():
        return False
    if str(left.get("route") or "opensea_drop").lower() != str(
        right.get("route") or "opensea_drop"
    ).lower():
        return False
    left_id = str(left.get("stage_id") or "").strip().lower()
    right_id = str(right.get("stage_id") or "").strip().lower()
    if left_id and right_id:
        return left_id == right_id
    return candidate_stage_identity(left) == candidate_stage_identity(right)


def candidate_key(candidate):
    return ":".join(str(value) for value in (
        candidate["chain"],
        candidate["slug"],
        candidate_stage_identity(candidate),
        candidate.get("route", "opensea_drop"),
        candidate.get("contract_address", ""),
    ))
