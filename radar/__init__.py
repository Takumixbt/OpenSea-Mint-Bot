"""
Free Mint Radar: the discovery half of the mint system.

    smart_graph  the edge - diffs who your curated smart accounts follow, so a
                 project surfaces days before its mint is announced
    discover     the wide net - public X search and OpenSea's Robinhood feed
    osint        the safety screen - rejects malicious, only flags mediocre
    score        ranking, so your attention goes to the right row first
    notion_log   the watchlist and the control surface (you tick Armed)
    mint_direct  direct contract minting, no marketplace login needed
    chain        raw Robinhood Chain RPC reads

Entry points live one level up: radar_bootstrap.py, radar_scan.py, radar_watch.py
"""
