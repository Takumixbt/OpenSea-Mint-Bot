"""
Logs the bot into OpenSea the same way the real website does: Sign-In-With-
Ethereum (SIWE). No password. OpenSea gives us a random "nonce", we sign a
specific text message with the wallet's key to prove we own it, and OpenSea
hands back session cookies we reuse for a few days.

Nothing here ever prints or saves the private key. The key is only used, in
memory, to produce one signature.
"""

import json
import os
import time
from datetime import datetime, timezone

import httpx
from eth_account import Account
from eth_account.messages import encode_defunct

import config

# This is the statement used by OpenSea's current website SIWE flow.
SIWE_STATEMENT = (
    "Click to sign in and accept the OpenSea Terms of Service "
    "(https://opensea.io/tos) and Privacy Policy "
    "(https://opensea.io/privacy)."
)


def _now_iso():
    # OpenSea expects an ISO-8601 UTC timestamp with milliseconds and a Z,
    # e.g. 2026-07-18T12:34:56.789Z - not Python's default +00:00 form.
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def _build_siwe_message(address, nonce, issued_at):
    # This must match the standard EIP-4361 layout byte-for-byte, because we
    # sign THIS exact text and OpenSea rebuilds the same text from the fields
    # we send it. Any difference (a missing blank line, wrong slash) breaks it.
    signin_uri = config.opensea_signin_uri()
    return (
        f"{config.OPENSEA_DOMAIN} wants you to sign in with your Ethereum account:\n"
        f"{address}\n"
        f"\n"
        f"{SIWE_STATEMENT}\n"
        f"\n"
        f"URI: {signin_uri}\n"
        f"Version: 1\n"
        f"Chain ID: {config.TARGET_CHAIN_ID}\n"
        f"Nonce: {nonce}\n"
        f"Issued At: {issued_at}"
    )


def _base_headers():
    return {
        "user-agent": config.USER_AGENT,
        "origin": "https://opensea.io",
        "referer": "https://opensea.io/",
        "content-type": "application/json",
        "x-app-id": config.APP_ID_HEADER,
    }


def _save_session(cookies_dict):
    data = {"cookies": cookies_dict, "saved_at": int(time.time())}
    with open(config.SESSION_FILE, "w") as f:
        json.dump(data, f)


def _load_session():
    if not os.path.exists(config.SESSION_FILE):
        return None
    try:
        with open(config.SESSION_FILE) as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        # A half-written or corrupted session file must mean "log in fresh",
        # not crash the run.
        print("Saved session file was unreadable - will sign in fresh.")
        return None
    if not isinstance(data, dict) or not isinstance(data.get("cookies"), dict):
        return None
    # OpenSea sessions last about 3.5 days. Refresh a bit early (after 3 days)
    # so we never race a drop with an about-to-expire cookie.
    if int(time.time()) - data.get("saved_at", 0) > 3 * 24 * 3600:
        return None
    return data.get("cookies") or None


def discard_session():
    # Called when a saved session turns out to be dead (e.g. OpenSea revoked
    # it early) so the next login attempt starts clean.
    try:
        os.remove(config.SESSION_FILE)
    except OSError:
        pass


def get_authenticated_client(private_key, address, force_fresh=False):
    """
    Returns an httpx.Client that already carries valid OpenSea session cookies
    and the required headers. Reuses a saved session if one is still fresh,
    otherwise performs a full SIWE login. Keep-alive is on so the connection
    stays warm for the mint race. force_fresh=True skips any saved session
    (used when a saved session turns out to be dead).
    """
    limits = httpx.Limits(max_keepalive_connections=10, keepalive_expiry=60)
    client = httpx.Client(http2=True, headers=_base_headers(),
                          limits=limits, timeout=15.0)

    saved = None if force_fresh else _load_session()
    if saved:
        for name, value in saved.items():
            client.cookies.set(name, value, domain=".opensea.io")
        print("Reusing saved OpenSea login session (still fresh).")
        return client

    print("No fresh session found - signing in to OpenSea with your wallet...")

    # Step 1: ask OpenSea for a one-time nonce.
    nonce_resp = client.post(config.AUTH_NONCE_URL)
    nonce_resp.raise_for_status()
    try:
        nonce = nonce_resp.json().get("nonce") or nonce_resp.json().get("data", {}).get("nonce")
    except Exception:
        nonce = nonce_resp.text.strip()
    if not nonce:
        raise RuntimeError("OpenSea did not return a login nonce - the auth URL "
                           "may have changed (see README 'If login stops working').")

    # Step 2: build and sign the SIWE message. Address is lowercased because
    # OpenSea's message uses the plain lowercase address, not a checksummed one.
    lower_address = address.lower()
    issued_at = _now_iso()
    message = _build_siwe_message(lower_address, nonce, issued_at)
    signed = Account.sign_message(encode_defunct(text=message), private_key=private_key)
    signature = signed.signature.hex()
    if not signature.startswith("0x"):
        signature = "0x" + signature

    # Step 3: send the PARSED message fields (not the raw signed string) plus
    # the signature. This is the shape used by OpenSea's current website; the
    # endpoint rebuilds the signed message from the nested message object.
    payload = {
        "message": {
            "domain": config.OPENSEA_DOMAIN,
            "address": lower_address,
            "statement": SIWE_STATEMENT,
            "uri": config.opensea_signin_uri(),
            "version": "1",
            "chainId": str(config.TARGET_CHAIN_ID),
            "nonce": nonce,
            "issuedAt": issued_at,
            "accountType": "Ethereum",
        },
        "signature": signature,
        "chainArch": "EVM",
        "connectorId": "injected",
    }
    verify_resp = client.post(config.AUTH_VERIFY_URL, json=payload)
    verify_resp.raise_for_status()

    # On success OpenSea sets access_token + refresh_token cookies on the client.
    if not client.cookies:
        raise RuntimeError("Login sent but OpenSea returned no session cookies - "
                           "the message format or verify fields may need updating "
                           "(see README 'If login stops working').")

    _save_session(dict(client.cookies))
    print("Signed in to OpenSea successfully. Session saved for next time.")
    return client
