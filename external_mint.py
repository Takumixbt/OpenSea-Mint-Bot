"""Conservative resolver for mints hosted outside OpenSea's Drop API.

The resolver only enables a generic route when a verified ABI exposes a simple
mint function whose arguments can be bound unambiguously and whose transaction
can be simulated (or has an on-chain future start time). Custom proofs,
signatures, puzzle answers, currencies, tuples, and arbitrary bytes require a
dedicated adapter and are never guessed.
"""

from decimal import Decimal
import json
import re
import time

import httpx
from web3 import Web3

import config
import opensea_direct_executor


SOURCIFY_CONTRACT_URL = "https://sourcify.dev/server/v2/contract/{chain_id}/{address}"
BLOCKSCOUT_API_BASES = {
    "ethereum": "https://eth.blockscout.com/api/v2",
    "base": "https://base.blockscout.com/api/v2",
    "polygon": "https://polygon.blockscout.com/api/v2",
    "optimism": "https://optimism.blockscout.com/api/v2",
    "arbitrum": "https://arbitrum.blockscout.com/api/v2",
    "zora": "https://explorer.zora.energy/api/v2",
    "robinhood": "https://robinhoodchain.blockscout.com/api/v2",
}
MINT_NAMES = (
    "publicmint",
    "mintpublic",
    "publicsalemint",
    "mintpublicsale",
    "mint",
    "claim",
)
PRICE_NAMES = (
    "publicmintprice",
    "publicsaleprice",
    "mintprice",
    "saleprice",
    "cost",
    "price",
    "mintfee",
)
START_NAMES = (
    "publicsalestarttime",
    "publicmintstarttime",
    "salestarttime",
    "mintstarttime",
    "starttime",
)
LIMIT_NAMES = (
    "maxmintperwallet",
    "maxperwallet",
    "walletlimit",
    "maxmintamount",
)
QUANTITY_HINTS = ("quantity", "amount", "count", "number", "mintamount", "num")
RECIPIENT_HINTS = ("recipient", "receiver", "account", "owner", "to")
IDENTIFIER_HINTS = ("tokenid", "id", "phase", "index", "claimid", "dropid")


def resolve_collection_mint(collection, slug, alchemy_key, wallet_address, chain_hint=None):
    """Return ``(candidate, note)`` for a safely inferred external mint."""
    collection = collection if isinstance(collection, dict) else {}
    contracts = collection.get("contracts") or []
    records = []
    for contract in contracts if isinstance(contracts, list) else []:
        if not isinstance(contract, dict):
            continue
        chain = str(contract.get("chain") or "").lower()
        address = str(contract.get("address") or "")
        if chain_hint and chain != str(chain_hint).lower():
            continue
        chain_settings = config.chain_config(chain)
        if chain_settings and re.fullmatch(r"0x[a-fA-F0-9]{40}", address):
            records.append((chain, address, chain_settings))
    if not records:
        return None, "OpenSea did not provide a supported EVM collection contract."

    reasons = []
    for chain, address, chain_settings in records:
        # A collection can have a live public SeaDrop stage without appearing
        # in OpenSea's calendar. Resolve that deterministic on-chain route
        # before attempting ABI inference, because the NFT contract itself may
        # not expose a mint function at all—the SeaDrop singleton does.
        try:
            public_stage = opensea_direct_executor.inspect_public_stage(
                config.rpc_url_for_chain(alchemy_key, int(chain_settings["chain_id"])),
                address,
            )
        except Exception:
            public_stage = None
        if public_stage:
            price_wei = int(public_stage["mint_price_wei"])
            start_time = int(public_stage.get("start_time") or int(time.time()))
            end_time = int(public_stage["end_time"]) if public_stage.get("end_time") else None
            max_per_wallet = int(public_stage["max_per_wallet"] or 0) or None
            opensea_url = str(
                collection.get("opensea_url") or f"https://opensea.io/collection/{slug}"
            )
            native = str(chain_settings.get("native") or "native")
            return {
                "slug": str(slug),
                "name": str(collection.get("name") or slug),
                "chain": chain,
                "chain_id": int(chain_settings["chain_id"]),
                "stage_index": 0,
                # The SeaDrop contract has no OpenSea stage UUID. Keep a
                # deterministic route identity so a live-stage refresh does
                # not fail merely because ``time.time()`` advanced by a
                # second between inspect and schedule-arm.
                "stage_id": "seadrop-public",
                "stage_type": "public_seadrop",
                "stage_label": "Public SeaDrop",
                "start_time": start_time,
                "end_time": end_time,
                "price_wei": price_wei,
                "price_display": (
                    "Free" if price_wei == 0
                    else f"Paid · {_format_native(price_wei)} {native}"
                ),
                "access_label": "Public · SeaDrop",
                "is_free": price_wei == 0,
                "is_public": True,
                "max_per_wallet": max_per_wallet,
                "contract_address": address,
                "opensea_url": opensea_url,
                "project_url": str(collection.get("project_url") or ""),
                "mint_url": str(collection.get("mint_url") or ""),
                "description": str(collection.get("description") or ""),
                "image_url": str(collection.get("image_url") or ""),
                "route": "opensea_drop",
                "route_label": "Direct public SeaDrop",
                "route_note": (
                    "Resolved from the collection contract's live SeaDrop stage; "
                    "the final on-chain values are checked again before signing."
                ),
                "url": opensea_url,
            }, (
                "Resolved from the live public SeaDrop contract even though this "
                "collection was not present in OpenSea's drop calendar."
            )
        try:
            abi = fetch_verified_abi(
                int(chain_settings["chain_id"]), address, chain=chain
            )
        except Exception:
            reasons.append(f"{chain}: verified ABI unavailable")
            continue
        route = detect_simple_mint_route(abi)
        if not route:
            reasons.append(f"{chain}: contract uses custom mint arguments")
            continue
        try:
            candidate = inspect_route(
                route,
                abi,
                chain,
                address,
                chain_settings,
                slug,
                collection,
                alchemy_key,
                wallet_address,
            )
        except Exception as exc:
            reasons.append(f"{chain}: {type(exc).__name__}")
            continue
        if candidate:
            return candidate, (
                "Resolved from the verified collection contract. Final simulation "
                "runs again immediately before signing."
            )
    return None, "; ".join(reasons) or "No safe generic mint route was found."


def fetch_verified_abi(chain_id, address, chain=None):
    endpoint = SOURCIFY_CONTRACT_URL.format(
        chain_id=int(chain_id), address=Web3.to_checksum_address(address)
    )
    try:
        response = httpx.get(endpoint, params={"fields": "abi"}, timeout=10.0)
    except httpx.HTTPError as exc:
        raise RuntimeError("verified contract lookup failed") from exc
    abi = None
    if response.status_code == 200:
        try:
            abi = response.json().get("abi")
        except (ValueError, AttributeError):
            abi = None
    if isinstance(abi, list):
        return abi
    base = BLOCKSCOUT_API_BASES.get(str(chain or "").lower())
    if base:
        try:
            fallback = httpx.get(
                f"{base}/smart-contracts/{Web3.to_checksum_address(address)}",
                timeout=10.0,
            )
            if fallback.status_code == 200:
                abi = fallback.json().get("abi")
                if isinstance(abi, str):
                    abi = json.loads(abi)
                if isinstance(abi, list):
                    return abi
        except (httpx.HTTPError, ValueError, AttributeError, json.JSONDecodeError):
            pass
    raise RuntimeError("contract ABI is not available from Sourcify or Blockscout")


def detect_simple_mint_route(abi):
    functions = [item for item in abi if isinstance(item, dict) and item.get("type") == "function"]
    ranked = []
    for item in functions:
        name = str(item.get("name") or "")
        normalized = name.replace("_", "").lower()
        if normalized not in MINT_NAMES:
            continue
        if item.get("stateMutability") not in {"payable", "nonpayable"}:
            continue
        bindings = _argument_bindings(item.get("inputs") or [])
        if bindings is None:
            continue
        ranked.append((MINT_NAMES.index(normalized), len(bindings), item, bindings))
    if not ranked:
        return None
    _, _, function_abi, bindings = sorted(ranked, key=lambda row: (row[0], row[1]))[0]
    return {"function_abi": function_abi, "arg_bindings": bindings}


def _argument_bindings(inputs):
    if len(inputs) > 2:
        return None
    bindings = []
    for item in inputs:
        value_type = str(item.get("type") or "")
        name = str(item.get("name") or "").replace("_", "").lower()
        if value_type.startswith("uint"):
            if any(hint in name for hint in IDENTIFIER_HINTS):
                return None
            if not name or not any(hint in name for hint in QUANTITY_HINTS):
                return None
            bindings.append("quantity")
        elif value_type == "address":
            if not name or not any(hint == name or hint in name for hint in RECIPIENT_HINTS):
                return None
            bindings.append("wallet")
        else:
            return None
    if bindings.count("quantity") > 1 or bindings.count("wallet") > 1:
        return None
    return bindings


def inspect_route(route, abi, chain, address, chain_settings, slug, collection, alchemy_key, wallet):
    rpc_url = config.rpc_url_for_chain(alchemy_key, int(chain_settings["chain_id"]))
    w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 12}))
    if not w3.is_connected() or int(w3.eth.chain_id) != int(chain_settings["chain_id"]):
        raise RuntimeError("RPC unavailable")
    contract = w3.eth.contract(address=Web3.to_checksum_address(address), abi=abi)
    price_function = _find_zero_arg_view(abi, PRICE_NAMES)
    start_function = _find_zero_arg_view(abi, START_NAMES)
    limit_function = _find_zero_arg_view(abi, LIMIT_NAMES)
    price_wei = _call_uint(contract, price_function) if price_function else None
    start_time = _call_uint(contract, start_function) if start_function else 0
    max_per_wallet = _call_uint(contract, limit_function) if limit_function else None
    if "quantity" not in route["arg_bindings"]:
        max_per_wallet = 1
    now = int(time.time())
    args = bind_arguments(route["arg_bindings"], wallet, 1)
    mint_function = contract.get_function_by_signature(_abi_signature(route["function_abi"]))(*args)
    simulated = False
    simulation_error = ""
    if not start_time or start_time <= now:
        try:
            mint_function.call({"from": Web3.to_checksum_address(wallet), "value": int(price_wei or 0)})
            simulated = True
            if price_wei is None:
                price_wei = 0
        except Exception as exc:
            simulation_error = type(exc).__name__
    if price_wei is None:
        raise RuntimeError("exact mint price is not exposed on-chain")
    if not simulated and not (start_time and start_time > now):
        raise RuntimeError(f"mint simulation reverted ({simulation_error or 'unknown reason'})")
    start_time = int(start_time or now)
    native = str(chain_settings.get("native") or "native")
    price_display = "Free" if int(price_wei) == 0 else f"Paid · {_format_native(price_wei)} {native}"
    function_abi = dict(route["function_abi"])
    return {
        "slug": str(slug),
        "name": str(collection.get("name") or slug),
        "chain": chain,
        "chain_id": int(chain_settings["chain_id"]),
        "stage_index": 0,
        "stage_id": f"verified:{_abi_signature(function_abi)}",
        "stage_type": "verified_contract",
        "stage_label": "External public mint",
        "start_time": start_time,
        "end_time": None,
        "price_wei": int(price_wei),
        "price_display": price_display,
        "access_label": "Public contract route",
        "is_free": int(price_wei) == 0,
        "is_public": True,
        "max_per_wallet": max_per_wallet,
        "contract_address": address,
        "opensea_url": str(
            collection.get("opensea_url") or f"https://opensea.io/collection/{slug}"
        ),
        "project_url": str(collection.get("project_url") or ""),
        "mint_url": str(collection.get("mint_url") or ""),
        "description": str(collection.get("description") or ""),
        "image_url": str(collection.get("image_url") or ""),
        "route": "generic_contract",
        "route_label": "Verified contract",
        "generic_function_abi": function_abi,
        "generic_arg_bindings": list(route["arg_bindings"]),
        "generic_price_function": price_function,
        "generic_start_function": start_function,
        "route_simulated": simulated,
        "url": str(collection.get("opensea_url") or f"https://opensea.io/collection/{slug}"),
    }


def build_generic_calldata(candidate, rpc_url, wallet_address, quantity):
    """Refresh and encode a previously verified generic mint route."""
    candidate = dict(candidate or {})
    w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 12}))
    abi = [candidate.get("generic_function_abi")]
    price_name = candidate.get("generic_price_function")
    if price_name:
        # The stored mint ABI alone is insufficient for a price refresh.
        abi.append({
            "type": "function",
            "name": str(price_name),
            "inputs": [],
            "outputs": [{"name": "", "type": "uint256"}],
            "stateMutability": "view",
        })
    contract = w3.eth.contract(
        address=Web3.to_checksum_address(candidate["contract_address"]), abi=abi
    )
    args = bind_arguments(candidate.get("generic_arg_bindings") or [], wallet_address, quantity)
    function_abi = candidate["generic_function_abi"]
    function = contract.get_function_by_signature(_abi_signature(function_abi))(*args)
    unit_price = (
        int(getattr(contract.functions, str(price_name))().call())
        if price_name else int(candidate.get("price_wei") or 0)
    )
    total_value = unit_price * int(quantity)
    data = function._encode_transaction_data()
    # A current eth_call is the strongest possible pre-signing route check.
    function.call({
        "from": Web3.to_checksum_address(wallet_address),
        "value": total_value,
    })
    return {
        "to": candidate["contract_address"],
        "data": data,
        "value": total_value,
    }


def bind_arguments(bindings, wallet, quantity):
    values = []
    for binding in bindings:
        if binding == "wallet":
            values.append(Web3.to_checksum_address(wallet))
        elif binding == "quantity":
            values.append(int(quantity))
        else:
            raise ValueError("generic mint argument binding is invalid")
    return values


def _find_zero_arg_view(abi, names):
    choices = []
    for item in abi:
        if not isinstance(item, dict) or item.get("type") != "function":
            continue
        normalized = str(item.get("name") or "").replace("_", "").lower()
        if normalized not in names or item.get("inputs"):
            continue
        outputs = item.get("outputs") or []
        if len(outputs) != 1 or not str(outputs[0].get("type") or "").startswith("uint"):
            continue
        choices.append((names.index(normalized), str(item.get("name"))))
    return sorted(choices)[0][1] if choices else None


def _call_uint(contract, function_name):
    try:
        return int(getattr(contract.functions, function_name)().call())
    except Exception as exc:
        raise RuntimeError(f"could not read {function_name}") from exc


def _abi_signature(function_abi):
    inputs = ",".join(str(item.get("type")) for item in function_abi.get("inputs") or [])
    return f"{function_abi.get('name')}({inputs})"


def _format_native(value_wei):
    value = Decimal(int(value_wei)) / Decimal(10 ** 18)
    return format(value, ".18f").rstrip("0").rstrip(".") or "0"
