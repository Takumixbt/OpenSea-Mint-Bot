"""Private-key wallet registry for guarded multi-wallet execution.

Only public wallet metadata leaves this module. Private keys stay in memory and
are loaded from environment variables on process startup.
"""

from dataclasses import dataclass
import os
import re

from eth_account import Account


MAX_WALLETS = 20
_LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _-]{0,23}$")


@dataclass(frozen=True)
class WalletProfile:
    id: str
    label: str
    address: str
    private_key: str

    def public(self):
        return {"id": self.id, "label": self.label, "address": self.address}


def load_wallet_profiles(primary_private_key, primary_address, extra_value=None):
    """Load the primary wallet plus optional ``label:private-key`` entries.

    ``MINT_WALLETS`` uses semicolons between entries. Example::

        MINT_WALLETS=Backup:0xabc...;Third:0xdef...

    Addresses are derived from keys, duplicate addresses are rejected, and the
    caller never needs to place public addresses beside every extra key.
    """
    profiles = [WalletProfile(
        id="primary",
        label="Primary",
        address=str(primary_address or "").strip(),
        private_key=str(primary_private_key or "").strip(),
    )]
    raw = os.getenv("MINT_WALLETS", "") if extra_value is None else str(extra_value or "")
    seen_addresses = {profiles[0].address.lower()} if profiles[0].address else set()
    seen_labels = {"primary"}
    for entry in (part.strip() for part in raw.split(";")):
        if not entry:
            continue
        if ":" not in entry:
            raise ValueError("MINT_WALLETS entries must look like Label:0xPRIVATE_KEY")
        label, private_key = (part.strip() for part in entry.split(":", 1))
        if not _LABEL_RE.fullmatch(label):
            raise ValueError(
                "wallet labels must be 1-24 letters, numbers, spaces, dashes, or underscores"
            )
        if label.lower() in seen_labels:
            raise ValueError(f"duplicate wallet label: {label}")
        try:
            account = Account.from_key(private_key)
        except Exception as exc:
            raise ValueError(f"wallet '{label}' has an invalid private key") from exc
        address = account.address
        if address.lower() in seen_addresses:
            raise ValueError(f"wallet '{label}' duplicates another configured address")
        wallet_id = "w" + address[-8:].lower()
        profiles.append(WalletProfile(wallet_id, label, address, private_key))
        seen_addresses.add(address.lower())
        seen_labels.add(label.lower())
        if len(profiles) > MAX_WALLETS:
            raise ValueError(f"at most {MAX_WALLETS} wallets may be configured")
    return profiles


def select_wallet_profiles(profiles, wallet_ids=None):
    """Resolve saved public wallet IDs without ever persisting private keys."""
    profiles = list(profiles or [])
    if not profiles:
        raise ValueError("no signing wallet is configured")
    wanted = [str(value) for value in (wallet_ids or []) if str(value)]
    if not wanted:
        return [profiles[0]]
    by_id = {profile.id: profile for profile in profiles}
    missing = [wallet_id for wallet_id in wanted if wallet_id not in by_id]
    if missing:
        raise ValueError("one or more selected wallets are no longer configured")
    # Preserve registry order so broadcasts and Telegram receipts are stable.
    wanted_set = set(wanted)
    return [profile for profile in profiles if profile.id in wanted_set]
