"""Guarded OpenSea floor-listing previews and Seaport calldata encoding."""

from decimal import Decimal
import time

from web3 import Web3

import config
import opensea_client
from minter import Minter


_BASIC_ORDER_TUPLE = (
    "(address,uint256,uint256,address,address,address,uint256,uint256,uint8,"
    "uint256,uint256,bytes32,uint256,bytes32,bytes32,uint256,"
    "(uint256,address)[],bytes)"
)
_ORDER_PARAMETERS_TUPLE = (
    "(address,address,(uint8,address,uint256,uint256,uint256)[],"
    "(uint8,address,uint256,uint256,uint256,address)[],uint8,uint256,uint256,"
    "bytes32,uint256,bytes32,uint256)"
)
_ADVANCED_ORDER_TUPLE = f"({_ORDER_PARAMETERS_TUPLE},uint120,uint120,bytes,bytes)"
_CRITERIA_RESOLVER_ARRAY = "(uint256,uint8,uint256,uint256,bytes32[])[]"


def listing_price_wei(listing):
    try:
        return int(listing["price"]["current"]["value"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("OpenSea listing has no exact current price") from exc


def best_listing_preview(client, slug, api_key, fulfiller_address=None):
    listings, _ = opensea_client.get_best_collection_listings(
        client, slug, api_key, limit=20
    )
    now = int(time.time())
    for listing in listings:
        if not isinstance(listing, dict) or listing.get("status") != "ACTIVE":
            continue
        price = listing_price_wei(listing)
        parameters = (listing.get("protocol_data") or {}).get("parameters") or {}
        try:
            end_time = int(parameters.get("endTime") or 0)
        except (TypeError, ValueError):
            end_time = 0
        if end_time and end_time <= now:
            continue
        asset = listing.get("asset") or {}
        chain = str(listing.get("chain") or "").lower()
        if not config.chain_config(chain):
            continue
        currency = str(
            ((listing.get("price") or {}).get("current") or {}).get("currency")
            or config.chain_config(chain).get("native")
            or "native"
        )
        preview = {
            "slug": str(slug),
            "chain": chain,
            "chain_id": int(config.chain_config(chain)["chain_id"]),
            "order_hash": str(listing.get("order_hash") or ""),
            "protocol_address": str(listing.get("protocol_address") or ""),
            "contract_address": str(asset.get("contract") or ""),
            "token_id": str(asset.get("identifier") or ""),
            "price_wei": price,
            "currency": currency,
            "price_display": _format_price(price, currency),
            "end_time": end_time or None,
            "listing": listing,
            "opensea_url": (
                f"https://opensea.io/assets/{chain}/{asset.get('contract')}/"
                f"{asset.get('identifier')}"
            ),
        }
        if fulfiller_address:
            try:
                calldata = build_fulfillment_calldata(
                    client, listing, fulfiller_address, api_key
                )
            except Exception:
                # OpenSea's sorted feed can briefly retain a cancelled or
                # concurrently-filled order. Continue to the next exact item.
                continue
            preview["fulfillment_function"] = calldata.get("function")
        return preview
    raise RuntimeError("this collection has no active native-coin listing right now")


def build_fulfillment_calldata(client, listing, wallet_address, api_key):
    """Encode OpenSea's exact native-currency basic-order fulfillment."""
    payload = opensea_client.get_listing_fulfillment_data(
        client, listing, wallet_address, api_key
    )
    transaction = ((payload.get("fulfillment_data") or {}).get("transaction") or {})
    function = str(transaction.get("function") or "")
    if function.startswith("fulfillAdvancedOrder"):
        return _encode_advanced_order(transaction, function)
    if not function.startswith("fulfillBasicOrder"):
        raise RuntimeError(
            "this listing needs an advanced Seaport route that the safe buyer does not support"
        )
    parameters = ((transaction.get("input_data") or {}).get("parameters") or {})
    try:
        additional = [
            (int(item["amount"]), Web3.to_checksum_address(item["recipient"]))
            for item in parameters.get("additionalRecipients", [])
        ]
        values = (
            Web3.to_checksum_address(parameters["considerationToken"]),
            int(parameters["considerationIdentifier"]),
            int(parameters["considerationAmount"]),
            Web3.to_checksum_address(parameters["offerer"]),
            Web3.to_checksum_address(parameters["zone"]),
            Web3.to_checksum_address(parameters["offerToken"]),
            int(parameters["offerIdentifier"]),
            int(parameters["offerAmount"]),
            int(parameters["basicOrderType"]),
            int(parameters["startTime"]),
            int(parameters["endTime"]),
            bytes.fromhex(str(parameters["zoneHash"])[2:]),
            int(parameters["salt"]),
            bytes.fromhex(str(parameters["offererConduitKey"])[2:]),
            bytes.fromhex(str(parameters["fulfillerConduitKey"])[2:]),
            int(parameters["totalOriginalAdditionalRecipients"]),
            additional,
            bytes.fromhex(str(parameters["signature"])[2:]),
        )
        selector = Web3.keccak(text=function)[:4]
        encoded = Web3().codec.encode([_BASIC_ORDER_TUPLE], [values])
        suffix_text = str(transaction.get("calldata_suffix") or "0x")
        suffix = bytes.fromhex(suffix_text[2:]) if suffix_text.startswith("0x") else b""
        value = int(transaction["value"])
        chain_id = int(transaction["chain"])
        to = Web3.to_checksum_address(transaction["to"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("OpenSea returned malformed basic-order fulfillment data") from exc
    return {
        "to": to,
        "data": Web3.to_hex(selector + encoded + suffix),
        "value": value,
        "chain_id": chain_id,
        "function": function.split("(", 1)[0],
    }


def _encode_advanced_order(transaction, function):
    input_data = transaction.get("input_data") or {}
    advanced = input_data.get("advancedOrder") or {}
    parameters = advanced.get("parameters") or {}
    try:
        offer = [
            (
                int(item["itemType"]),
                Web3.to_checksum_address(item["token"]),
                int(item["identifierOrCriteria"]),
                int(item["startAmount"]),
                int(item["endAmount"]),
            )
            for item in parameters.get("offer", [])
        ]
        consideration = [
            (
                int(item["itemType"]),
                Web3.to_checksum_address(item["token"]),
                int(item["identifierOrCriteria"]),
                int(item["startAmount"]),
                int(item["endAmount"]),
                Web3.to_checksum_address(item["recipient"]),
            )
            for item in parameters.get("consideration", [])
        ]
        order_parameters = (
            Web3.to_checksum_address(parameters["offerer"]),
            Web3.to_checksum_address(parameters["zone"]),
            offer,
            consideration,
            int(parameters["orderType"]),
            int(parameters["startTime"]),
            int(parameters["endTime"]),
            bytes.fromhex(str(parameters["zoneHash"])[2:]),
            int(parameters["salt"]),
            bytes.fromhex(str(parameters["conduitKey"])[2:]),
            int(parameters["totalOriginalConsiderationItems"]),
        )
        advanced_order = (
            order_parameters,
            int(advanced["numerator"]),
            int(advanced["denominator"]),
            bytes.fromhex(str(advanced["signature"])[2:]),
            bytes.fromhex(str(advanced.get("extraData") or "0x")[2:]),
        )
        criteria = [
            (
                int(item["orderIndex"]),
                int(item["side"]),
                int(item["index"]),
                int(item["identifier"]),
                [bytes.fromhex(str(proof)[2:]) for proof in item.get("criteriaProof", [])],
            )
            for item in input_data.get("criteriaResolvers", [])
        ]
        values = [
            advanced_order,
            criteria,
            bytes.fromhex(str(input_data["fulfillerConduitKey"])[2:]),
            Web3.to_checksum_address(input_data["recipient"]),
        ]
        selector = Web3.keccak(text=function)[:4]
        encoded = Web3().codec.encode(
            [_ADVANCED_ORDER_TUPLE, _CRITERIA_RESOLVER_ARRAY, "bytes32", "address"],
            values,
        )
        suffix_text = str(transaction.get("calldata_suffix") or "0x")
        suffix = bytes.fromhex(suffix_text[2:]) if suffix_text.startswith("0x") else b""
        value = int(transaction["value"])
        chain_id = int(transaction["chain"])
        to = Web3.to_checksum_address(transaction["to"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("OpenSea returned malformed advanced-order fulfillment data") from exc
    return {
        "to": to,
        "data": Web3.to_hex(selector + encoded + suffix),
        "value": value,
        "chain_id": chain_id,
        "function": function.split("(", 1)[0],
    }


def _format_price(value_wei, currency):
    amount = Decimal(int(value_wei)) / Decimal(10 ** 18)
    value = format(amount, ".18f").rstrip("0").rstrip(".") or "0"
    return f"{value} {currency}"


class PurchaseEngine:
    """Build and broadcast one explicitly confirmed OpenSea floor purchase."""

    def __init__(self, alchemy_key, opensea_api_key):
        self.alchemy_key = alchemy_key
        self.opensea_api_key = opensea_api_key

    def execute(self, preview, wallet_profile):
        preview = dict(preview or {})
        chain_id = int(preview.get("chain_id") or 0)
        if chain_id <= 0:
            raise ValueError("the purchase preview has no valid chain")
        expected_price = int(preview.get("price_wei") or 0)
        if expected_price <= 0:
            raise ValueError("the purchase preview has no valid price")
        if expected_price > config.MAX_BUY_VALUE_WEI:
            raise RuntimeError(
                "this listing is above MAX_BUY_PRICE_NATIVE; raise the buy cap "
                "deliberately before confirming"
            )
        minter = Minter(
            config.rpc_url_for_chain(self.alchemy_key, chain_id),
            wallet_profile.private_key,
            wallet_profile.address,
            chain_id,
        )
        live_chain, nonce = minter.warm_up()
        if live_chain != chain_id:
            raise RuntimeError("RPC chain changed after the purchase preview")
        client = opensea_client.get_api_client(self.opensea_api_key)
        try:
            calldata = build_fulfillment_calldata(
                client,
                preview.get("listing") or {},
                wallet_profile.address,
                self.opensea_api_key,
            )
        finally:
            client.close()
        if int(calldata.get("chain_id") or 0) != chain_id:
            raise RuntimeError("OpenSea returned a different chain for this listing")
        if int(calldata.get("value") or 0) != expected_price:
            raise RuntimeError(
                "the exact fulfillment price differs from the confirmed preview; "
                "nothing was signed or sent"
            )
        signed, summary = minter.build_transaction(
            calldata["to"],
            calldata["data"],
            calldata["value"],
            approved_value_wei=expected_price,
            max_value_wei=config.MAX_BUY_VALUE_WEI,
            value_label="OpenSea purchase",
        )
        result = {
            "action": "buy",
            "candidate": preview,
            "wallet": wallet_profile.public(),
            "nonce": nonce,
            "summary": summary,
            "status": "sent",
        }
        tx_hash = minter.send(signed)
        result["tx_hash"] = tx_hash
        try:
            result["confirmed"] = minter.wait_for_confirmation(tx_hash)
            receipt = minter.last_receipt or {}
            gas_used = int(receipt.get("gasUsed") or 0)
            effective_gas_price = int(receipt.get("effectiveGasPrice") or 0)
            result["actual_gas_wei"] = gas_used * effective_gas_price
            result["gas_used"] = gas_used
            result["block_number"] = receipt.get("blockNumber")
        except Exception as exc:
            result["confirmed"] = None
            result["confirmation_error"] = f"{type(exc).__name__}: confirmation timed out"
        return result
