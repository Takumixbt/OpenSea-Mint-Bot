"""Read-only audit of the documented OpenSea Drops API.

Run this before a live drop:

    python recon_check.py

It verifies the API base URL, the configured slug, the API key, and the
target drop's stage response. It never requests mint calldata, signs, or
broadcasts a transaction.
"""

import os
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv

import config


ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")


def filled(name):
    value = os.getenv(name, "").strip()
    upper = value.upper()
    return bool(value) and "PASTE_" not in upper and "YOUR_" not in upper


def response_message(response):
    try:
        payload = response.json()
    except ValueError:
        return response.text[:180].replace("\n", " ")
    if isinstance(payload, dict):
        for key in ("message", "error", "detail"):
            if payload.get(key):
                return str(payload[key])[:180]
    return str(payload)[:180]


def main():
    print("=" * 60)
    print("OpenSea Drops API audit")
    print("=" * 60)

    all_ok = True
    if config.OPENSEA_API_BASE_URL == "https://api.opensea.io/api/v2":
        print("[PASS] Official OpenSea API base URL")
    else:
        all_ok = False
        print("[FAIL] config.OPENSEA_API_BASE_URL is not the documented v2 API")

    try:
        slug = config.target_collection_slug()
    except ValueError as exc:
        slug = None
        all_ok = False
        print(f"[FAIL] Target collection: {exc}")
    if not slug:
        all_ok = False
        print("[FAIL] Target collection: set TARGET_COLLECTION_URL in config.py")
    else:
        print(f"[PASS] Target collection slug: {slug!r}")

    if not filled("OPENSEA_API_KEY"):
        all_ok = False
        print("[BLOCKED] OPENSEA_API_KEY is missing or still a placeholder")
    elif slug:
        try:
            response = httpx.get(
                f"{config.OPENSEA_API_BASE_URL.rstrip('/')}/drops/{slug}",
                headers={
                    "accept": "application/json",
                    "x-api-key": os.environ["OPENSEA_API_KEY"],
                },
                timeout=15,
            )
            if response.status_code != 200:
                all_ok = False
                detail = response_message(response)
                suffix = f": {detail}" if detail else ""
                print(f"[BLOCKED] OpenSea API target check: HTTP {response.status_code}{suffix}")
            else:
                try:
                    payload = response.json()
                except ValueError:
                    payload = None
                if not isinstance(payload, dict):
                    all_ok = False
                    print("[FAIL] OpenSea API target check: response was not JSON")
                else:
                    print("[PASS] OpenSea API key accepted and target drop is readable")
                    drop = payload.get("drop") if isinstance(payload.get("drop"), dict) else payload
                    stages = drop.get("stages") if isinstance(drop, dict) else None
                    if isinstance(stages, list) and stages:
                        print(f"[PASS] Target drop returned {len(stages)} mint stage(s)")
                    else:
                        all_ok = False
                        print("[FAIL] Target drop response did not contain a usable stages list")
        except Exception as exc:
            all_ok = False
            print(f"[BLOCKED] OpenSea API target check failed ({type(exc).__name__})")

    print("\n" + "=" * 60)
    if all_ok:
        print("OpenSea API audit passed. Review the target, quantity, price cap, and gas balance before live use.")
        return 0
    print("OpenSea API audit did not pass. Fix the marked item(s) before live use.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
