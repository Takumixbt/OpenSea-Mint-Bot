"""
Direct-contract minting: the primary execution path.

The original bot could only mint by asking OpenSea's private GraphQL API for a
server-signed calldata blob. That path is fragile (it needs a working SIWE
login, and the query shape changes without notice) and it is currently broken.

Most free mints on a young chain do not need any of that. The contract has a
public mint function and anyone can call it. So this module works out WHICH
mint function a contract exposes by probing, then builds the call itself. No
marketplace login, no server signature, nothing to go stale.

Selectors are computed at runtime with keccak rather than hardcoded, so there
are no magic constants here to get subtly wrong.
"""

import os
import time

import httpx
from web3 import Web3

# Ordered most-likely-first. Each entry is (solidity signature, how to fill args).
#   "none"     no arguments
#   "qty"      a single quantity argument
#   "to_qty"   recipient then quantity
CANDIDATE_MINTS = [
    ("mint(uint256)", "qty"),
    ("mint()", "none"),
    ("publicMint(uint256)", "qty"),
    ("freeMint(uint256)", "qty"),
    ("mintPublic(uint256)", "qty"),
    ("claim(uint256)", "qty"),
    ("mint(address,uint256)", "to_qty"),
    ("publicMint()", "none"),
    ("freeMint()", "none"),
    ("claim()", "none"),
]

# Measured against 60 real mints on Robinhood Chain, 2026-08-05: not one of the
# candidate signatures above appeared. What actually showed up was OpenSea's
# SeaDrop (mintPublic on 0x00005ea0...) and four selectors that resolve to no
# public signature at all. Guessing signatures does not work on this chain.
#
# So the primary path is replay: take a transaction that just successfully
# minted this collection, swap the minter's address for ours, and simulate it.
# That works for any contract shape, including proprietary ones, because the
# chain has already proven that exact call mints. See replay_probe().
SEADROP = "0x00005ea00ac477b1030ce78506496e8c2de24bf5"


def selector(signature):
    return Web3.keccak(text=signature)[:4]


def encode(signature, arg_style, wallet_address, quantity):
    """
    Build the calldata for one candidate mint function.
    """
    data = selector(signature)
    if arg_style == "qty":
        data += quantity.to_bytes(32, "big")
    elif arg_style == "to_qty":
        addr = bytes.fromhex(Web3.to_checksum_address(wallet_address)[2:])
        data += b"\x00" * 12 + addr
        data += quantity.to_bytes(32, "big")
    return "0x" + data.hex()


def _rpc(method, params, timeout=20.0):
    """Raw JSON-RPC, for the Alchemy-only methods web3 does not expose."""
    key = os.getenv("ALCHEMY_API_KEY")
    if not key:
        return None
    url = f"https://robinhood-mainnet.g.alchemy.com/v2/{key}"
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(url, json={"jsonrpc": "2.0", "id": 1,
                                          "method": method, "params": params})
            resp.raise_for_status()
            return resp.json().get("result")
    except (httpx.HTTPError, ValueError):
        return None


# Resolving replay candidates costs one getAssetTransfers plus one
# getTransactionByHash per candidate, which measured at ~30s. Inside a race that
# is fatal, so the expensive lookup is cached and only the cheap simulation runs
# on every loop iteration. The TTL keeps it honest: before a mint opens there is
# nothing to replay, and the list has to be refreshed to notice it started.
_CALL_CACHE = {}
CALL_CACHE_TTL_SECONDS = 10.0


def recent_mint_calls(contract, limit=12, use_cache=True):
    """
    Find transactions that recently minted this collection.

    Returns a list of {to, input, value, from} dicts, newest first. These are
    proof by existence: the chain already executed them, so their shape is
    known-good for this contract.
    """
    address = Web3.to_checksum_address(contract)

    cached = _CALL_CACHE.get(address)
    if use_cache and cached and (time.time() - cached[0]) < CALL_CACHE_TTL_SECONDS:
        return cached[1]
    transfers = _rpc("alchemy_getAssetTransfers", [{
        "fromAddress": "0x" + "0" * 40,
        "contractAddresses": [address],
        "category": ["erc721", "erc1155"],
        "order": "desc",
        "maxCount": hex(limit),
    }])
    if not transfers:
        # Cache the empty answer too. Before a mint opens this is the common
        # case, and re-asking every 200ms would spend the rate limit on nothing.
        _CALL_CACHE[address] = (time.time(), [])
        return []

    calls = []
    seen = set()
    for transfer in transfers.get("transfers", []):
        tx_hash = transfer.get("hash")
        if not tx_hash or tx_hash in seen:
            continue
        seen.add(tx_hash)
        tx = _rpc("eth_getTransactionByHash", [tx_hash])
        if not tx or not tx.get("input") or len(tx["input"]) < 10:
            continue
        calls.append({
            "to": tx.get("to"),
            "input": tx["input"],
            "value": int(tx.get("value") or "0x0", 16),
            "from": tx.get("from"),
            "hash": tx_hash,
        })
    _CALL_CACHE[address] = (time.time(), calls)
    return calls


def observed_mint_price(contract, limit=10):
    """
    What have people actually paid to mint this? Returns (min_wei, max_wei, n).

    Read off real transactions rather than from a `price()` getter, because the
    getter is per-stage and lies about what the current stage costs. What people
    are paying right now is the only number that matters, and it is the same
    number the executor will be asked to send.

    A spread means tiered pricing or multi-quantity mints, not free-then-paid,
    so the maximum is the figure to judge by.
    """
    calls = recent_mint_calls(contract, limit=limit, use_cache=False)
    if not calls:
        return None, None, 0
    values = [c["value"] for c in calls]
    return min(values), max(values), len(values)


def replay_probe(w3, contract, wallet_address, log=print, max_value_wei=0):
    """
    Mint by replaying a transaction that just minted this collection.

    Guessing at `mint(uint256)` and friends does not work here: a survey of real
    mints on this chain found none of them, and four of the selectors in use
    resolve to no published signature. Rather than keep guessing, this copies
    the calldata of a mint the chain has already accepted and substitutes our
    address wherever the original minter's appears.

    That handles arbitrary contract shapes, and it handles calls routed through
    a different contract entirely: OpenSea's SeaDrop mints are sent to SeaDrop
    with the collection as an argument, so the correct `to` is carried along
    with the calldata rather than assumed to be the collection.

    It cannot replay a mint gated on a server-issued signature or a per-address
    Merkle proof, because those encode the original minter and re-signing them
    is not possible. Those are also not snipeable by anyone, so nothing is lost.

    `max_value_wei` is a hard cap. A replayed call carries the original's ETH
    value, and a paid mint must never be sent just because a free one was
    expected. Returns (to, calldata, value, note) or (None, None, 0, reason).
    """
    sender = Web3.to_checksum_address(wallet_address)
    sender_word = sender[2:].lower()

    calls = recent_mint_calls(contract)
    if not calls:
        return None, None, 0, "no recent mint transactions to copy"

    for call in calls:
        if call["value"] > max_value_wei:
            continue  # a paid mint; the cap is not negotiable
        original = (call.get("from") or "")[2:].lower()
        data = call["input"]
        if original:
            # Swap the minter out. Addresses appear in calldata left-padded to
            # 32 bytes, so a plain hex replace hits every occurrence.
            data = data.replace(original, sender_word)
        try:
            w3.eth.estimate_gas({
                "from": sender,
                "to": Web3.to_checksum_address(call["to"]),
                "data": data,
                "value": call["value"],
            })
        except Exception:
            continue
        log(f"  replaying the mint from {call['hash'][:12]}... "
            f"to {call['to'][:10]}...")
        return call["to"], data, call["value"], None

    return None, None, 0, f"none of {len(calls)} recent mints replayed cleanly"


def probe(w3, contract, wallet_address, quantity, log=print):
    """
    Find a mint function this contract will actually accept.

    Works by asking the node to simulate each candidate call. A function that
    estimates successfully is one that would execute; everything else reverts.
    Returns (calldata_hex, signature) or (None, None).

    Worth knowing: before a mint opens, EVERY candidate will revert (that is
    what "not open yet" means on-chain). So a failed probe is not proof the
    contract is wrong. The watcher re-probes in a loop as the clock hits zero.
    """
    target = Web3.to_checksum_address(contract)
    sender = Web3.to_checksum_address(wallet_address)

    for signature, arg_style in CANDIDATE_MINTS:
        data = encode(signature, arg_style, sender, quantity)
        try:
            w3.eth.estimate_gas({
                "from": sender,
                "to": target,
                "data": data,
                "value": 0,
            })
        except Exception:
            continue
        log(f"  contract accepts {signature}")
        return data, signature

    return None, None


def probe_readonly(w3, contract, wallet_address, quantity):
    """
    Same probe but quiet and boolean, for the screening pass where we only want
    to know whether this address looks like a mintable contract at all.
    """
    data, signature = probe(w3, contract, wallet_address, quantity, log=lambda *_: None)
    return signature is not None
