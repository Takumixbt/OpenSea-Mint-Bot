"""
The safety screen.

This is deliberately NOT a quality filter. On Robinhood Chain gas is near-zero,
so minting a dud costs cents while missing a real one costs multiple ETH. A
precision filter that rejects borderline projects loses money on that trade.

So this module answers one question only: does this look actively malicious or
fake? Everything else gets flagged and still lands on the watchlist, ranked
lower. Only BLOCKING_FLAGS (currently: provable links to a prior rug) reject a
project outright.
"""

from . import chain, settings, xcli

# Wording that reliably indicates a drainer rather than a mint. These are the
# patterns where the "free mint" page exists to get a wallet signature, not to
# hand out an NFT.
SCAM_PHRASES = (
    "verify your wallet",
    "validate your wallet",
    "sync your wallet",
    "claim your airdrop now",
    "connect to claim",
    "wallet validation",
    "migrate your nft",
)


def screen_project(handle, mint_contract=None, w3=None, log=print):
    """
    Run the screen on one candidate.

    Returns a dict with:
        flags        list of risk-flag strings (matching the Notion multi-select)
        blocked      True if a fatal flag fired
        notes        human-readable findings, newline separated
        profile      the X profile dict, if we got one
        deployer     deployer address, if resolved
        account_age  days, if resolved
    """
    flags = []
    notes = []
    profile = None
    deployer = None
    age_days = None

    # --- X account screen ---
    if handle:
        profile, note = xcli.profile(handle)
        if note:
            notes.append(f"X profile unavailable: {note}")
            flags.append("no-prior-history")
        else:
            age_days = xcli.account_age_days(profile.get("created_at"))
            followers = profile.get("followers", 0)
            notes.append(
                f"@{profile['handle']}: {followers:,} followers, "
                f"{'age ' + str(age_days) + 'd' if age_days is not None else 'age unknown'}"
                f"{', verified' if profile.get('verified') else ''}"
            )
            if profile.get("bio"):
                notes.append(f"Bio: {str(profile['bio'])[:200]}")

            if age_days is not None and age_days < settings.MIN_X_ACCOUNT_AGE_DAYS:
                flags.append("fresh-x-account")
                notes.append(
                    f"X account is only {age_days}d old (under the "
                    f"{settings.MIN_X_ACCOUNT_AGE_DAYS}d bar). Throwaway-scam pattern, "
                    f"though plenty of real launches are also new."
                )

            blob = " ".join(str(profile.get(k) or "") for k in ("bio", "name")).lower()
            hit = next((p for p in SCAM_PHRASES if p in blob), None)
            if hit:
                flags.append("prior-rug-linked")
                notes.append(f"BLOCKING: drainer-style wording in the profile ({hit!r}).")
    else:
        # Found on-chain or on OpenSea with no X account attached. Not a risk in
        # itself, and most collections on this chain are in this position, but
        # it means the entire social half of the screen never ran. Say so on the
        # row rather than letting it read as "screened clean".
        flags.append("no-x-account")
        notes.append("No X account known, so no social screen was possible. "
                     "Everything below is on-chain evidence only.")

    # --- On-chain screen ---
    if mint_contract:
        if w3 is None:
            w3, note = chain.connect()
            if note:
                notes.append(f"On-chain screen skipped: {note}")
        if w3 is not None:
            exists, note = chain.contract_exists(w3, mint_contract)
            if note:
                notes.append(f"Contract code check failed: {note}")
            elif not exists:
                flags.append("unverified-contract")
                notes.append(f"No bytecode at {mint_contract} yet (not deployed, or wrong address).")
            else:
                notes.append(f"Contract {mint_contract} has deployed bytecode.")

                deployer, note = chain.find_deployer(w3, mint_contract)
                if note:
                    notes.append(f"Deployer not resolved: {note}")
                    flags.append("no-prior-history")
                else:
                    wallet, wnote = chain.wallet_profile(w3, deployer)
                    if wnote:
                        notes.append(f"Deployer {deployer} found, but not profiled: {wnote}")
                    else:
                        notes.append(
                            f"Deployer {deployer}: {wallet['tx_count']} txs, "
                            f"{wallet['balance_eth']:.4f} ETH"
                        )
                        if wallet["tx_count"] < settings.MIN_DEPLOYER_TX_COUNT:
                            flags.append("deployer-new-wallet")
                            notes.append(
                                f"Deployer wallet has only {wallet['tx_count']} txs. "
                                f"Fresh wallet, worth a look before arming."
                            )
    else:
        notes.append("No mint contract known yet, so no on-chain screen was possible.")

    if not flags:
        flags.append("clean")

    blocked = bool(set(flags) & settings.BLOCKING_FLAGS)

    return {
        "flags": sorted(set(flags)),
        "blocked": blocked,
        "notes": "\n".join(notes),
        "profile": profile,
        "deployer": deployer,
        "account_age": age_days,
    }
