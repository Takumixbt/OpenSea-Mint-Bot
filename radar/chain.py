"""
On-chain reads for Robinhood Chain, over the same Alchemy key the mint bot
already uses. Plain JSON-RPC via web3, no explorer API required - Etherscan's
V2 coverage of chain 4663 is unconfirmed, and Blockscout may or may not index
this chain, so the radar depends on neither.

Used for the deployer screen: is this contract real, who deployed it, and does
that wallet have any history or did it appear yesterday.
"""

import os

from web3 import Web3

from . import settings


def _rpc_url():
    key = os.getenv("ALCHEMY_API_KEY")
    if not key:
        return None
    # Mirrors CHAIN_RPC_SUBDOMAINS in the bot's config.py.
    return f"https://robinhood-mainnet.g.alchemy.com/v2/{key}"


def connect():
    """Returns (web3_or_None, note)."""
    url = _rpc_url()
    if not url:
        return None, "ALCHEMY_API_KEY is not set in .env"
    w3 = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": 20}))
    try:
        if not w3.is_connected():
            return None, "could not reach the Robinhood Chain RPC"
        live = w3.eth.chain_id
    except Exception as e:
        return None, f"RPC error: {type(e).__name__}"
    if live != settings.CHAIN_ID:
        return None, f"RPC is on chain {live}, expected {settings.CHAIN_ID}"
    return w3, None


def contract_exists(w3, address):
    """Is there actually deployed bytecode at this address?"""
    try:
        code = w3.eth.get_code(Web3.to_checksum_address(address))
    except Exception as e:
        return None, f"could not read code: {type(e).__name__}"
    return (code is not None and len(code) > 2), None


def wallet_profile(w3, address):
    """
    What the radar can learn about a wallet from raw RPC alone: how many
    transactions it has ever sent, and whether it holds a balance.

    Returns (dict_or_None, note).
    """
    try:
        checksum = Web3.to_checksum_address(address)
        tx_count = w3.eth.get_transaction_count(checksum)
        balance = w3.eth.get_balance(checksum)
    except Exception as e:
        return None, f"could not profile wallet: {type(e).__name__}"
    return {
        "address": checksum,
        "tx_count": tx_count,
        "balance_wei": balance,
        "balance_eth": float(Web3.from_wei(balance, "ether")),
    }, None


def find_deployer(w3, contract_address, from_block=0):
    """
    Find who deployed a contract by binary-searching for its creation block.

    Searches the whole chain by default. That sounds expensive and is not:
    the search is logarithmic, so covering all ~28M blocks of Robinhood Chain
    costs about 25 RPC calls. Narrowing the window to save a couple of calls
    just produces "predates the search window" on anything older than a few
    hours, which is most contracts.

    Returns (deployer_address_or_None, note).
    """
    try:
        latest = w3.eth.block_number
    except Exception as e:
        return None, f"could not read block height: {type(e).__name__}"

    target = Web3.to_checksum_address(contract_address)

    # Binary-search the first block where the contract has code. Contract code
    # appears at its creation block and never disappears, so the predicate is
    # monotonic and this costs ~log2(chain length) RPC calls instead of a scan.
    low = max(0, from_block)
    high = latest
    try:
        if len(w3.eth.get_code(target, block_identifier=low)) > 2:
            return None, "contract predates the search window"
        if len(w3.eth.get_code(target, block_identifier=high)) <= 2:
            return None, "no contract code at this address"
    except Exception as e:
        # Some RPC providers reject historical get_code on archive-less nodes.
        return None, f"historical code lookup unsupported: {type(e).__name__}"

    while low + 1 < high:
        mid = (low + high) // 2
        try:
            has_code = len(w3.eth.get_code(target, block_identifier=mid)) > 2
        except Exception:
            return None, "historical lookup failed mid-search"
        if has_code:
            high = mid
        else:
            low = mid

    # `high` is the creation block. Find the transaction in it that made this
    # contract by checking each receipt's contractAddress.
    try:
        block = w3.eth.get_block(high, full_transactions=True)
    except Exception as e:
        return None, f"could not read creation block: {type(e).__name__}"

    for tx in block.transactions:
        if tx.get("to") is not None:
            continue
        try:
            receipt = w3.eth.get_transaction_receipt(tx["hash"])
        except Exception:
            continue
        created = receipt.get("contractAddress")
        if created and Web3.to_checksum_address(created) == target:
            return Web3.to_checksum_address(tx["from"]), None

    # Factory-deployed contracts have no direct creation tx in the block; the
    # deployer is then the factory caller, which is a deeper trace than this
    # screen needs. Report honestly instead of returning something wrong.
    return None, "created by a factory or internal call, no direct deployer"
