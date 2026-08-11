"""
Ranking.

The score decides READING ORDER, not admission. Anything not blocked by the
safety screen reaches the Notion watchlist; the score just puts the strongest
signal at the top so a scarce resource (your attention, before you tick Armed)
goes to the right row first.

Smart-account follows dominate every other term on purpose. That signal is the
only genuinely leading one in the system: engagement, contract deployment, and
public announcements all happen after the crowd already knows.
"""

from . import settings


def score_candidate(candidate):
    """
    Take an assembled candidate dict and return (score, breakdown_lines).
    """
    breakdown = []
    total = 0.0

    smart_count = candidate.get("smart_follow_count", 0)
    if smart_count:
        points = min(smart_count * settings.W_SMART_FOLLOW, settings.W_SMART_FOLLOW_CAP)
        total += points
        breakdown.append(f"+{points:.0f}  {smart_count} smart account(s) newly following")

    mentions = candidate.get("smart_mentions") or []
    if mentions:
        evidence = candidate.get("evidence") or "mentioned"
        weight = settings.EVIDENCE_MULTIPLIER.get(evidence, 0.25)
        points = min(len(mentions) * settings.W_SMART_MENTION,
                     settings.W_SMART_MENTION_CAP) * weight
        total += points
        # Weighted below a follow on purpose. Posting about a project is a
        # public act that the whole timeline sees; quietly following it is the
        # thing that happens before anyone is looking. And a bare @-mention is
        # weaker still: being tagged in a mint post is not being the mint.
        breakdown.append(f"+{points:.0f}  {evidence} by {len(mentions)} smart "
                         f"account(s): {', '.join(mentions[:4])}")

    if candidate.get("names_chain"):
        total += settings.W_CHAIN_MATCH
        breakdown.append(f"+{settings.W_CHAIN_MATCH}  the post named {settings.CHAIN_NAME} Chain")

    likes = candidate.get("engagement_likes", 0)
    if likes:
        points = likes * settings.W_X_ENGAGEMENT
        total += points
        breakdown.append(f"+{points:.1f}  {likes:,} likes across matched posts")

    age = candidate.get("account_age")
    if age is not None and age >= settings.MIN_X_ACCOUNT_AGE_DAYS:
        total += settings.W_ACCOUNT_AGE
        breakdown.append(f"+{settings.W_ACCOUNT_AGE}  X account is {age}d old, past the fresh-account bar")

    if candidate.get("deployer") and "deployer-new-wallet" not in candidate.get("flags", []):
        total += settings.W_CLEAN_DEPLOYER
        breakdown.append(f"+{settings.W_CLEAN_DEPLOYER}  deployer wallet has real prior history")

    if candidate.get("mint_contract"):
        total += settings.W_HAS_CONTRACT
        breakdown.append(f"+{settings.W_HAS_CONTRACT}  mint contract resolved, executable")
    else:
        total += settings.W_NO_CONTRACT
        breakdown.append(f"{settings.W_NO_CONTRACT}  no contract: unpriced, unverified, "
                         f"and not yet proven to be a mint at all")

    price = candidate.get("mint_price_wei")
    if price == 0:
        total += settings.W_FREE_MINT
        breakdown.append(f"+{settings.W_FREE_MINT}  confirmed free, recent mints paid gas only")
    elif price:
        # Heavily negative rather than blocking. A paid mint is not a scam, it is
        # simply not what this hunts, and the executor's value cap stops it from
        # ever going out by accident.
        total += settings.W_PAID_MINT
        breakdown.append(f"{settings.W_PAID_MINT}  costs {price / 1e18:.5f} ETH, not a free mint")

    live = candidate.get("live_mints") or 0
    if live:
        # Not a prediction. Tokens left the zero address, so the mint is open
        # and the only question left is whether you want it.
        total += settings.W_LIVE_MINT
        breakdown.append(f"+{settings.W_LIVE_MINT}  minting live right now "
                         f"({live} in the last window)")

    risk_flags = [f for f in candidate.get("flags", []) if f not in settings.INFO_FLAGS]
    if risk_flags:
        points = len(risk_flags) * settings.W_RISK_FLAG
        total += points
        breakdown.append(f"{points:.0f}  {len(risk_flags)} risk flag(s): {', '.join(risk_flags)}")

    return round(total, 1), breakdown


def status_for(candidate, score):
    """
    Pick the Notion lifecycle status. Nothing here ever sets Armed - that is a
    human tick, by design. An unattended auto-mint on an unvetted contract is
    how a wallet gets drained.
    """
    if candidate.get("blocked"):
        return "Rejected"
    if candidate.get("mint_contract") and candidate.get("mint_open"):
        return "Watching"
    return "Candidate"
