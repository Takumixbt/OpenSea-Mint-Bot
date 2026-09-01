"""Fast, on-chain execution for compatible public SeaDrop stages.

OpenSea's public SeaDrop route is deterministic: the singleton contract stores
the public price and window, and ``mintPublic`` needs no OpenSea-issued
signature.  This module only prepares that narrow route.  It never guesses a
custom contract function, never bypasses an allowlist, and returns ``None``
when a collection is not a compatible SeaDrop public stage.  The same planner
is also used when a collection is linked on OpenSea but omitted from its drop
calendar.

The caller can then sign the returned transaction before the stage opens.  The
existing OpenSea and verified-generic routes remain available as fallbacks for
stages that need API calldata, signatures, or custom contract arguments.
"""

import time

from web3 import Web3

import config


SEADROP_ADDRESS = "0x00005EA00Ac477B1030CE78506496e8C2dE24bf5"
OPENSEA_FEE_RECIPIENT = "0x0000a26b00c1F0DF003000390027140000fAa719"


class DirectSafetyError(RuntimeError):
    """A direct on-chain check found a request that must not be sent."""


PUBLIC_ABI = [
    {
        "type": "function",
        "name": "mintPublic",
        "stateMutability": "payable",
        "inputs": [
            {"name": "nftContract", "type": "address"},
            {"name": "feeRecipient", "type": "address"},
            {"name": "minterIfNotPayer", "type": "address"},
            {"name": "quantity", "type": "uint256"},
        ],
        "outputs": [],
    },
    {
        "type": "function",
        "name": "getPublicDrop",
        "stateMutability": "view",
        "inputs": [{"name": "nftContract", "type": "address"}],
        "outputs": [{
            "name": "",
            "type": "tuple",
            "components": [
                {"name": "mintPrice", "type": "uint80"},
                {"name": "startTime", "type": "uint48"},
                {"name": "endTime", "type": "uint48"},
                {"name": "maxTotalMintableByWallet", "type": "uint16"},
                {"name": "feeBps", "type": "uint16"},
                {"name": "restrictFeeRecipients", "type": "bool"},
            ],
        }],
    },
    {
        "type": "function",
        "name": "getAllowedFeeRecipients",
        "stateMutability": "view",
        "inputs": [{"name": "nftContract", "type": "address"}],
        "outputs": [{"name": "", "type": "address[]"}],
    },
]


def _as_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def _address(value):
    raw = str(value or "").strip()
    if not Web3.is_address(raw):
        return None
    return Web3.to_checksum_address(raw)


def is_candidate_eligible(candidate):
    """Return whether a saved candidate is safe to try on the direct path."""
    if not config.DIRECT_PUBLIC_SEADROP or not isinstance(candidate, dict):
        return False
    if str(candidate.get("route") or "opensea_drop") == "generic_contract":
        return False
    if candidate.get("is_public") is not True:
        return False
    return _address(candidate.get("contract_address")) is not None


def _read_public_drop(seadrop, nft_contract):
    try:
        raw = seadrop.functions.getPublicDrop(nft_contract).call()
    except Exception:
        return None
    if isinstance(raw, dict):
        values = (
            raw.get("mintPrice"),
            raw.get("startTime"),
            raw.get("endTime"),
            raw.get("maxTotalMintableByWallet"),
            raw.get("feeBps"),
            raw.get("restrictFeeRecipients"),
        )
    else:
        try:
            values = tuple(raw)
        except TypeError:
            return None
    if len(values) < 6:
        return None
    drop = {
        "mint_price_wei": _as_int(values[0]),
        "start_time": _as_int(values[1]),
        "end_time": _as_int(values[2]),
        "max_per_wallet": _as_int(values[3]),
        "fee_bps": _as_int(values[4]),
        "restrict_fee_recipients": bool(values[5]),
    }
    if not drop["start_time"] and not drop["end_time"] and not drop["max_per_wallet"]:
        return None
    return drop


def _fee_recipient(seadrop, nft_contract, restricted):
    try:
        allowed = list(seadrop.functions.getAllowedFeeRecipients(nft_contract).call())
    except Exception:
        allowed = []
    allowed = [_address(value) for value in allowed]
    allowed = [value for value in allowed if value]
    if allowed:
        return allowed[0], "allowed fee recipient from SeaDrop"
    if restricted:
        return None, "SeaDrop restricts fee recipients but returned no allowed recipient"
    return Web3.to_checksum_address(OPENSEA_FEE_RECIPIENT), "OpenSea default fee recipient"


def inspect_public_stage(rpc_url, nft_contract):
    """Read a live/upcoming public SeaDrop stage without building a tx.

    This is used when a collection is on OpenSea but is missing from OpenSea's
    calendar.  It deliberately returns only the narrow public SeaDrop shape;
    allowlists, signatures, and project-specific arguments are not inferred.
    """
    if not config.DIRECT_PUBLIC_SEADROP:
        return None
    nft_contract = _address(nft_contract)
    if not nft_contract:
        return None
    provider = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 12}))
    if not provider.is_connected():
        return None
    seadrop = provider.eth.contract(
        address=Web3.to_checksum_address(SEADROP_ADDRESS), abi=PUBLIC_ABI
    )
    drop = _read_public_drop(seadrop, nft_contract)
    if not drop:
        return None
    now = int(time.time())
    if drop["end_time"] and now >= drop["end_time"]:
        return None
    fee_recipient, fee_source = _fee_recipient(
        seadrop, nft_contract, drop["restrict_fee_recipients"]
    )
    if not fee_recipient:
        return None
    drop["fee_recipient"] = fee_recipient
    drop["fee_source"] = fee_source
    return drop


def _start_matches(candidate, drop):
    candidate_start = _as_int(candidate.get("start_time"))
    onchain_start = _as_int(drop.get("start_time"))
    if not candidate_start or not onchain_start:
        return True
    return abs(candidate_start - onchain_start) <= config.DIRECT_SEADROP_START_TOLERANCE_SECONDS


def build_public_plan(rpc_url, candidate, quantity):
    """Return a deterministic SeaDrop transaction plan, or ``None``.

    The plan is read-only.  No signature or transaction is created here.  A
    price/start mismatch means the saved OpenSea preview is stale, so the
    caller should use its normal OpenSea calldata route instead.
    """
    if not is_candidate_eligible(candidate):
        return None
    try:
        quantity = int(quantity)
    except (TypeError, ValueError):
        raise RuntimeError("quantity must be an integer")
    if quantity < 1 or quantity > 100:
        raise RuntimeError("quantity must be between 1 and 100")

    nft_contract = _address(candidate.get("contract_address"))
    if not nft_contract:
        return None
    provider = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 12}))
    if not provider.is_connected():
        return None
    seadrop = provider.eth.contract(
        address=Web3.to_checksum_address(SEADROP_ADDRESS), abi=PUBLIC_ABI
    )
    drop = _read_public_drop(seadrop, nft_contract)
    if not drop or not _start_matches(candidate, drop):
        return None

    max_per_wallet = drop["max_per_wallet"]
    if max_per_wallet and quantity > max_per_wallet:
        raise DirectSafetyError(
            f"SeaDrop allows only {max_per_wallet} NFT(s) per wallet; quantity {quantity} would revert"
        )
    candidate_price = candidate.get("price_wei")
    if candidate_price is not None and _as_int(candidate_price) != drop["mint_price_wei"]:
        return None

    fee_recipient, fee_source = _fee_recipient(
        seadrop, nft_contract, drop["restrict_fee_recipients"]
    )
    if not fee_recipient:
        return None
    total_value = drop["mint_price_wei"] * quantity
    data = seadrop.functions.mintPublic(
        nft_contract,
        fee_recipient,
        "0x0000000000000000000000000000000000000000",
        quantity,
    )._encode_transaction_data()
    if not isinstance(data, str):
        data = Web3.to_hex(data)
    now = int(time.time())
    if drop["end_time"] and now >= drop["end_time"]:
        return None
    return {
        "to": Web3.to_checksum_address(SEADROP_ADDRESS),
        "data": data,
        "value": total_value,
        "mint_price_wei": drop["mint_price_wei"],
        "start_time": drop["start_time"],
        "end_time": drop["end_time"],
        "max_per_wallet": max_per_wallet,
        "fee_recipient": fee_recipient,
        "fee_source": fee_source,
        "route": "direct_seadrop",
    }
