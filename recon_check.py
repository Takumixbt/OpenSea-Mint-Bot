"""
Run this BEFORE trusting the bot for a new drop:

    python recon_check.py

It downloads OpenSea's current website code and checks that the mechanisms the
bot relies on are still there (the internal mint endpoint, the required header,
the drop-schedule field, the mint-calldata field, and the login wording).

Every result comes with an honest confidence level, because "the words appear
somewhere in a huge pile of website code" is NOT the same as "OpenSea still
works the way the bot assumes":

  PASS       - found in the right structural context (e.g. the header name
               wired to its value, the field used inside a GraphQL query).
               Strong signal.
  PASS-WEAK  - the text is present somewhere, but not in a context this script
               can positively identify. Usually still fine, but treat it as
               "probably" rather than "confirmed".
  CHANGED    - not found at all. Something likely moved; see the README.
  UNKNOWN    - this item cannot be verified from the outside (explained inline).

If the download itself looks broken (too few files, tiny content), the script
says INCONCLUSIVE instead of falsely reporting everything as CHANGED - that
distinction matters: it usually means OpenSea changed how the site is packaged
or blocked the fetch, not that the bot's mechanisms are gone.

This exists because OpenSea can change their site at any time. This bot is not
"build once, forget forever" - re-run this check before each new drop.
"""

import json
import re
import sys

import httpx

import config
from opensea_auth import SIWE_STATEMENT

# Each item: (label, strong_regex, weak_substring, which config line it backs).
# The strong regex demands the right SURROUNDING context, so boilerplate text
# that merely mentions the same word somewhere can't produce a false PASS.
BUNDLE_CHECKS = [
    ("Internal mint endpoint host",
     re.compile(r"https://gql\.opensea\.io"),
     "gql.opensea.io",
     "config.GQL_ENDPOINT"),
    ("Required app header (x-app-id wired to os2-web)",
     re.compile(r"""x-app-id["']?\s*[:=,]\s*["']""" + re.escape(config.APP_ID_HEADER)),
     config.APP_ID_HEADER,
     "config.APP_ID_HEADER"),
    ("Drop-schedule field (dropBySlug used as a query field)",
     re.compile(r"dropBySlug\s*\("),
     "dropBySlug",
     "opensea_client.DROP_QUERY"),
    ("Mint-calldata field (transactionSubmissionData with tx fields)",
     re.compile(r"transactionSubmissionData\s*\{[^}]{0,300}\b(to|data|value)\b"),
     "transactionSubmissionData",
     "opensea_client.MINT_CALLDATA_QUERY"),
    ("Login wallet-arch field (chainArch used as a key)",
     re.compile(r"""chainArch["']?\s*[:=]"""),
     "chainArch",
     "opensea_auth.py payload"),
    ("Login connector field (connectorId used as a key)",
     re.compile(r"""connectorId["']?\s*[:=]"""),
     "connectorId",
     "opensea_auth.py payload"),
]

# The SIWE sentence is the EIP-4361 STANDARD template, so finding it only
# proves some sign-in-with-Ethereum code ships in the bundle - it cannot
# confirm OpenSea's exact statement wording. Handled separately below.
SIWE_TEMPLATE = "wants you to sign in with your Ethereum account"

# Sanity floors for the download: below these, the fetch itself failed and no
# CHANGED verdicts are trustworthy.
MIN_CHUNK_FILES = 10
MIN_BUNDLE_CHARS = 500_000


def fetch_bundle_text():
    ua = config.USER_AGENT
    with httpx.Client(headers={"user-agent": ua}, timeout=30.0,
                      follow_redirects=True) as c:
        print("Downloading OpenSea's homepage...")
        index = c.get("https://opensea.io/").text
        paths = sorted(set(re.findall(r'/_next/static/chunks/[^"\\]+?\.js\?dpl=[^"\\]+', index)))
        print(f"Found {len(paths)} code files to check. Downloading them "
              f"(this takes a minute)...")
        combined = []
        for i, p in enumerate(paths, 1):
            try:
                combined.append(c.get("https://opensea.io" + p).text)
            except Exception:
                pass
            if i % 25 == 0:
                print(f"    ...{i}/{len(paths)}")
        return len(paths), "\n".join(combined)


def check_endpoint_live():
    """
    Returns (verdict, detail). PASS only if the endpoint answers like a real
    GraphQL server (JSON with data/errors). A 404 or an HTML page is a FAIL -
    the old version of this check wrongly counted any non-5xx (even 404) as PASS.
    """
    try:
        with httpx.Client(timeout=15.0) as c:
            r = c.post(config.GQL_ENDPOINT,
                       headers={"content-type": "application/json",
                                "user-agent": config.USER_AGENT,
                                "x-app-id": config.APP_ID_HEADER},
                       json={"query": "query{__typename}"})
    except Exception as e:
        return "FAIL", f"could not reach it at all ({type(e).__name__})"
    if r.status_code == 404:
        return "FAIL", "HTTP 404 - the endpoint address appears to have moved"
    if r.status_code >= 500:
        return "FAIL", f"HTTP {r.status_code} server error"
    try:
        body = json.loads(r.text)
    except ValueError:
        return "FAIL", (f"HTTP {r.status_code} but the reply is not JSON - "
                        f"possibly a block page, not a GraphQL server")
    if isinstance(body, dict) and ("data" in body or "errors" in body):
        return "PASS", f"answers like a live GraphQL server (HTTP {r.status_code})"
    return "PASS-WEAK", (f"HTTP {r.status_code} with JSON, but not shaped like "
                         f"a normal GraphQL reply")


def main():
    print("=" * 60)
    print("OpenSea drift check")
    print("=" * 60)

    verdict, detail = check_endpoint_live()
    print(f"\n[{verdict}] Live endpoint {config.GQL_ENDPOINT}: {detail}")
    endpoint_ok = verdict.startswith("PASS")

    n_files, bundle = fetch_bundle_text()
    print()

    if n_files < MIN_CHUNK_FILES or len(bundle) < MIN_BUNDLE_CHARS:
        print(f"[INCONCLUSIVE] Only got {n_files} code files "
              f"({len(bundle)} characters) from OpenSea's site. That is far too")
        print("little to judge anything - the download itself failed or OpenSea")
        print("changed how its site is packaged (or blocked this fetch). The")
        print("checks below were NOT run; a CHANGED verdict from so little data")
        print("would be meaningless. Try again later or from a browser-visited")
        print("network. Do not run the bot for real until this is resolved.")
        sys.exit(2)

    all_ok = endpoint_ok
    any_weak = False
    for label, strong_re, weak_needle, backs in BUNDLE_CHECKS:
        if strong_re.search(bundle):
            print(f"[PASS] {label}")
        elif weak_needle in bundle:
            any_weak = True
            print(f"[PASS-WEAK] {label}")
            print(f"         ^ the text exists in the site code but not in the "
                  f"exact context expected. Probably fine; if the bot fails, "
                  f"re-check: {backs}")
        else:
            all_ok = False
            print(f"[CHANGED] {label}")
            print(f"         ^ not found in OpenSea's current site. "
                  f"May need updating: {backs}")

    if SIWE_TEMPLATE in bundle:
        print("[PASS-WEAK] Sign-In-With-Ethereum login code present")
        print("         ^ this only proves SIWE code ships in the bundle (the")
        print("           sentence is a standard template every SIWE library uses).")
    else:
        all_ok = False
        print("[CHANGED] Sign-In-With-Ethereum login code present")
        print("         ^ no SIWE wording found at all - login flow may have changed:")
        print("           opensea_auth._build_siwe_message")
    if SIWE_STATEMENT in bundle:
        print("[PASS] Exact login statement wording matches opensea_auth.SIWE_STATEMENT")
    else:
        print("[UNKNOWN] Exact login statement wording")
        print("         ^ OpenSea's exact statement text was not found in the site")
        print("           code - it may be produced server-side, so this can't be")
        print("           verified from here. If login fails, capture the real")
        print("           statement per the README ('If login stops working').")

    print("\n" + "=" * 60)
    if all_ok:
        print("No CHANGED items - the mechanisms the bot uses look intact."
              + (" Some are PASS-WEAK (see above): probably fine, not proven."
                 if any_weak else ""))
        print("Reminder: the exact mint-query NAME still can't be checked from")
        print("here (OpenSea loads it only on a live drop page). If a real run")
        print("fails at the 'asking OpenSea for mint instructions' step, do the")
        print("2-minute browser capture in the README once.")
    else:
        print("SOMETHING CHANGED - see the lines marked CHANGED/FAIL above and")
        print("the README section 'If something in the drift check fails'.")
        sys.exit(1)


if __name__ == "__main__":
    main()
