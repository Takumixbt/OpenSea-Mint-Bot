"""
Reading OpenSea SeaDrop's published mint schedule straight off the chain.

This is the difference between reacting and being ready. The on-chain mint feed
tells you a drop is happening because tokens are already moving, which means you
are arriving mid-race. SeaDrop, by contrast, stores each collection's public
drop stage in a struct anyone can read, so for any SeaDrop collection the exact
open second is knowable in advance.

It also settles the price question authoritatively. `mint_direct` infers price
from what people have recently paid, which is honest but backward-looking and
blind to a stage that has not started. `mintPrice` here is what the contract
will actually charge, before anybody has paid it.

Struct returned by getPublicDrop(address):

    uint80  mintPrice                 wei per token
    uint48  startTime                 unix seconds
    uint48  endTime                   unix seconds
    uint16  maxTotalMintableByWallet  per-wallet cap
    uint16  feeBps
    bool    restrictFeeRecipients

Solidity ABI-encodes each of those into its own 32-byte word, so the return is
six words regardless of the declared widths.
"""

import time

from web3 import Web3

# OpenSea's SeaDrop, deployed at the same address on every chain it supports.
SEADROP = "0x00005ea00ac477b1030ce78506496e8c2de24bf5"

# Computed at runtime rather than hardcoded, same rule as mint_direct.
_GET_PUBLIC_DROP = Web3.keccak(text="getPublicDrop(address)")[:4]


def public_drop(w3, contract):
    """
    Read the public drop stage for one collection. Returns a dict or None.

    None means "not a SeaDrop collection, or it has no public stage configured",
    both of which are ordinary and not errors. Never raises: a collection that
    does not implement this simply reverts, and the sweep must survive that.
    """
    if w3 is None or not contract:
        return None

    data = "0x" + _GET_PUBLIC_DROP.hex() + \
           Web3.to_checksum_address(contract)[2:].lower().rjust(64, "0")
    try:
        raw = w3.eth.call({"to": Web3.to_checksum_address(SEADROP), "data": data})
    except Exception:
        return None

    if len(raw) < 192:
        return None

    words = [int.from_bytes(raw[i:i + 32], "big") for i in range(0, 192, 32)]
    price, start, end, max_per_wallet, fee_bps, restricted = words

    # An unconfigured stage reads back as all zeros. That is not a free mint
    # opening at the epoch, it is the absence of a drop, and treating it as the
    # former would put a phantom row at the top of the board.
    if start == 0 and end == 0 and price == 0 and max_per_wallet == 0:
        return None

    now = time.time()
    if now < start:
        state = "upcoming"
    elif now <= end:
        state = "live"
    else:
        state = "ended"

    return {
        "price_wei": price,
        "price_eth": price / 1e18,
        "start": start,
        "end": end,
        "max_per_wallet": max_per_wallet,
        "fee_bps": fee_bps,
        "restrict_fee_recipients": bool(restricted),
        "state": state,
        "seconds_until_open": max(0.0, start - now),
    }


def describe(drop):
    """One human line for the OSINT notes."""
    if not drop:
        return None
    when = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(drop["start"]))
    price = "FREE" if drop["price_wei"] == 0 else f"{drop['price_eth']:.6f} ETH"
    if drop["state"] == "upcoming":
        mins = drop["seconds_until_open"] / 60
        timing = f"opens {when} ({mins:.0f} min away)"
    elif drop["state"] == "live":
        timing = f"OPEN NOW, closes {time.strftime('%H:%M:%S UTC', time.gmtime(drop['end']))}"
    else:
        timing = f"ended {when}"
    return (f"SeaDrop public stage: {price}, max {drop['max_per_wallet']} "
            f"per wallet, {timing}.")
