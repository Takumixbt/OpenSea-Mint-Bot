"""Download and normalise the network logos used by the Telegram scan picker.

Run this only when you add a network or want to refresh the artwork:

    python scripts/fetch_chain_logos.py

The result is committed to ``assets/chains/`` so the bot never depends on a
third-party CDN at runtime: rendering the picker has to be instant and must
still work when that CDN is down. A network with no logo here falls back to a
lettered disc in its own colour, so a missing file is never fatal.

The logos are third-party trademarks used only to identify each network.
"""

import io
import sys
from pathlib import Path

import httpx
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import config  # noqa: E402

OUTPUT_DIR = ROOT / "assets" / "chains"
SOURCE = "https://icons.llamao.fi/icons/chains/rsz_{name}.jpg"
SIZE = 96

# OpenSea's network slug -> the name this source publishes it under. Verified
# against the live CDN; gunzilla and animechain are absent there and fall back
# to a lettered disc at render time.
SOURCE_NAMES = {
    "ethereum": "ethereum",
    "optimism": "optimism",
    "unichain": "unichain",
    "polygon": "polygon",
    "monad": "monad",
    "shape": "shape",
    "flow": "flow",
    "stablechain": "stable",
    "hyperevm": "hyperliquid",
    "sei": "sei",
    "soneium": "soneium",
    "ronin": "ronin",
    "abstract": "abstract",
    "megaeth": "megaeth",
    "robinhood": "robinhood",
    "somnia": "somnia",
    "b3": "b3",
    "base": "base",
    "ape_chain": "apechain",
    "arbitrum": "arbitrum",
    "avalanche": "avalanche",
    "ink": "ink",
    "bera_chain": "berachain",
    "blast": "blast",
    "zora": "zora",
}


def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    saved, failed, skipped = [], [], []

    with httpx.Client(timeout=20.0, follow_redirects=True) as client:
        for slug in config.CHAIN_CONFIGS:
            name = SOURCE_NAMES.get(slug)
            if not name:
                skipped.append(slug)
                continue
            try:
                response = client.get(SOURCE.format(name=name))
                if response.status_code != 200 or len(response.content) < 300:
                    failed.append((slug, f"HTTP {response.status_code}"))
                    continue
                with Image.open(io.BytesIO(response.content)) as image:
                    image.load()
                    square = image.convert("RGB").resize(
                        (SIZE, SIZE), Image.Resampling.LANCZOS
                    )
            except Exception as exc:
                failed.append((slug, type(exc).__name__))
                continue
            square.save(OUTPUT_DIR / f"{slug}.png", format="PNG", optimize=True)
            saved.append(slug)

    print(f"saved   {len(saved)} -> {OUTPUT_DIR}")
    if skipped:
        print(f"skipped {len(skipped)} (no published logo): {skipped}")
    if failed:
        print(f"failed  {len(failed)}: {failed}")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
