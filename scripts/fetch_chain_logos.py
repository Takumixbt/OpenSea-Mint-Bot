"""Download official network marks into assets/chains/.

Run from the repo root:

    python scripts/fetch_chain_logos.py

Sources are tried in order. The first image that decodes and is large enough
wins. Files are stored as RGBA PNG so transparent rings (Base, OP) survive.
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

import httpx
from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
DEST = ROOT / "assets" / "chains"
SOURCES_MD = DEST / "SOURCES.md"
SIZE = 256

# Official orgs / brand-kit files / Trust Wallet / Llama icons.
# Order is the preference: brand kit, then the chain's GitHub org avatar,
# then Trust Wallet, then Llama as a last resort.
CHAIN_URLS = {
    "ethereum": [
        "https://raw.githubusercontent.com/trustwallet/assets/master/blockchains/ethereum/info/logo.png",
        "https://github.com/ethereum.png?size=256",
    ],
    "optimism": [
        "https://github.com/ethereum-optimism.png?size=256",
        "https://icons.llamao.fi/icons/chains/rsz_optimism.jpg",
        "https://raw.githubusercontent.com/trustwallet/assets/master/blockchains/optimism/info/logo.png",
    ],
    "unichain": [
        "https://icons.llamao.fi/icons/chains/rsz_unichain.jpg",
        "https://raw.githubusercontent.com/trustwallet/assets/master/blockchains/unichain/info/logo.png",
        "https://github.com/Uniswap.png?size=256",
    ],
    "polygon": [
        "https://raw.githubusercontent.com/trustwallet/assets/master/blockchains/polygon/info/logo.png",
        "https://github.com/0xPolygon.png?size=256",
        "https://github.com/maticnetwork.png?size=256",
    ],
    "monad": [
        "https://raw.githubusercontent.com/trustwallet/assets/master/blockchains/monad/info/logo.png",
        "https://github.com/category-labs.png?size=256",
        "https://github.com/monad-crypto.png?size=256",
        "https://icons.llamao.fi/icons/chains/rsz_monad.jpg",
    ],
    "shape": [
        "https://github.com/shape-network.png?size=256",
        "https://icons.llamao.fi/icons/chains/rsz_shape.jpg",
    ],
    "flow": [
        "https://raw.githubusercontent.com/trustwallet/assets/master/blockchains/flow/info/logo.png",
        "https://github.com/onflow.png?size=256",
    ],
    "stablechain": [
        "https://github.com/stable-blockchain.png?size=256",
        "https://icons.llamao.fi/icons/chains/rsz_stable.jpg",
    ],
    "hyperevm": [
        "https://raw.githubusercontent.com/trustwallet/assets/master/blockchains/hyperevm/info/logo.png",
        "https://github.com/hyperliquid-dex.png?size=256",
        "https://icons.llamao.fi/icons/chains/rsz_hyperliquid.jpg",
        "https://icons.llamao.fi/icons/chains/rsz_hyperevm.jpg",
    ],
    "sei": [
        "https://raw.githubusercontent.com/trustwallet/assets/master/blockchains/sei/info/logo.png",
        "https://github.com/sei-protocol.png?size=256",
    ],
    "soneium": [
        "https://raw.githubusercontent.com/trustwallet/assets/master/blockchains/soneium/info/logo.png",
        "https://github.com/soneium.png?size=256",
        "https://icons.llamao.fi/icons/chains/rsz_soneium.jpg",
    ],
    "ronin": [
        "https://raw.githubusercontent.com/trustwallet/assets/master/blockchains/ronin/info/logo.png",
        "https://github.com/axieinfinity.png?size=256",
        "https://github.com/RoninNetwork.png?size=256",
    ],
    "abstract": [
        "https://raw.githubusercontent.com/trustwallet/assets/master/blockchains/abstract/info/logo.png",
        "https://github.com/Abstract-Foundation.png?size=256",
        "https://icons.llamao.fi/icons/chains/rsz_abstract.jpg",
    ],
    "megaeth": [
        "https://github.com/megaeth-labs.png?size=256",
        "https://github.com/megaeth.png?size=256",
        "https://icons.llamao.fi/icons/chains/rsz_megaeth.jpg",
    ],
    "robinhood": [
        "https://github.com/robinhood-chain.png?size=256",
        "https://github.com/Robinhood.png?size=256",
        "https://icons.llamao.fi/icons/chains/rsz_robinhood.jpg",
    ],
    "somnia": [
        "https://github.com/SomniaNetwork.png?size=256",
        "https://github.com/somnia-network.png?size=256",
        "https://icons.llamao.fi/icons/chains/rsz_somnia.jpg",
    ],
    "b3": [
        "https://github.com/b3-fun.png?size=256",
        "https://github.com/b3dotfun.png?size=256",
        "https://icons.llamao.fi/icons/chains/rsz_b3.jpg",
    ],
    "base": [
        "https://github.com/base-org.png?size=256",
        "https://icons.llamao.fi/icons/chains/rsz_base.jpg",
        "https://raw.githubusercontent.com/trustwallet/assets/master/blockchains/base/info/logo.png",
    ],
    "ape_chain": [
        "https://icons.llamao.fi/icons/chains/rsz_apechain.jpg",
        "https://raw.githubusercontent.com/trustwallet/assets/master/blockchains/apechain/info/logo.png",
        "https://github.com/ApeCoinDev.png?size=256",
        "https://github.com/ApeChainDev.png?size=256",
    ],
    "arbitrum": [
        "https://raw.githubusercontent.com/trustwallet/assets/master/blockchains/arbitrum/info/logo.png",
        "https://github.com/OffchainLabs.png?size=256",
        "https://github.com/ArbitrumFoundation.png?size=256",
    ],
    "avalanche": [
        "https://raw.githubusercontent.com/trustwallet/assets/master/blockchains/avalanchec/info/logo.png",
        "https://github.com/ava-labs.png?size=256",
    ],
    "gunzilla": [
        "https://github.com/GunzillaGames.png?size=256",
        "https://github.com/gunzilla.png?size=256",
        "https://icons.llamao.fi/icons/chains/rsz_gunz.jpg",
        "https://icons.llamao.fi/icons/chains/rsz_gunzilla.jpg",
    ],
    "ink": [
        "https://raw.githubusercontent.com/trustwallet/assets/master/blockchains/ink/info/logo.png",
        "https://github.com/inkonchain.png?size=256",
        "https://icons.llamao.fi/icons/chains/rsz_ink.jpg",
    ],
    "animechain": [
        "https://github.com/animechain.png?size=256",
        "https://github.com/kpop-protocol.png?size=256",
        "https://icons.llamao.fi/icons/chains/rsz_animechain.jpg",
        "https://icons.llamao.fi/icons/chains/rsz_anime.jpg",
    ],
    "bera_chain": [
        "https://raw.githubusercontent.com/trustwallet/assets/master/blockchains/berachain/info/logo.png",
        "https://github.com/berachain.png?size=256",
    ],
    "blast": [
        "https://raw.githubusercontent.com/trustwallet/assets/master/blockchains/blast/info/logo.png",
        "https://github.com/blast-io.png?size=256",
        "https://icons.llamao.fi/icons/chains/rsz_blast.jpg",
    ],
    "zora": [
        "https://raw.githubusercontent.com/trustwallet/assets/master/blockchains/zora/info/logo.png",
        "https://icons.llamao.fi/icons/chains/rsz_zora.jpg",
        "https://github.com/ourzora.png?size=256",
    ],
}


def _is_svg(content, content_type):
    head = content[:200].lstrip().lower()
    return "svg" in (content_type or "") or head.startswith(b"<svg") or head.startswith(b"<?xml")


def _rasterize_svg(content):
    """Best-effort SVG raster. Optional; skipped if cairosvg is absent."""
    try:
        import cairosvg
    except ImportError:
        return None
    png = cairosvg.svg2png(bytestring=content, output_width=SIZE, output_height=SIZE)
    return Image.open(io.BytesIO(png))


def _open_image(content, content_type):
    if _is_svg(content, content_type):
        image = _rasterize_svg(content)
        if image is not None:
            return image
        raise ValueError("svg without cairosvg")
    return Image.open(io.BytesIO(content))


def _normalize(image):
    image = image.convert("RGBA")
    image.thumbnail((SIZE, SIZE), Image.Resampling.LANCZOS)
    canvas = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    canvas.paste(
        image,
        ((SIZE - image.width) // 2, (SIZE - image.height) // 2),
        image,
    )
    return canvas


def fetch_one(client, slug, urls):
    errors = []
    for url in urls:
        try:
            response = client.get(url, follow_redirects=True, timeout=20.0)
            response.raise_for_status()
            content = response.content
            if len(content) < 400:
                errors.append(f"{url} too small")
                continue
            image = _open_image(content, response.headers.get("content-type", ""))
            image.load()
            if min(image.size) < 32:
                errors.append(f"{url} tiny")
                continue
            return _normalize(image), url
        except Exception as exc:
            errors.append(f"{url} ({type(exc).__name__})")
            continue
    return None, errors


def main():
    DEST.mkdir(parents=True, exist_ok=True)
    rows = []
    failed = []
    with httpx.Client(headers={"User-Agent": "OpenSeaMintBot/logo-fetch"}) as client:
        for slug, urls in CHAIN_URLS.items():
            image, source = fetch_one(client, slug, urls)
            dest = DEST / f"{slug}.png"
            if image is None:
                failed.append((slug, source))
                print(f"FAIL  {slug}: {source}")
                continue
            image.save(dest, format="PNG", optimize=True)
            rows.append((slug, source, dest.stat().st_size))
            print(f"OK    {slug:12}  {source}")
    lines = [
        "# Chain logo sources",
        "",
        "Regenerate with `python scripts/fetch_chain_logos.py`.",
        "Each file is the first source that decoded as a real image.",
        "",
        "| slug | source | bytes |",
        "| --- | --- | ---: |",
    ]
    for slug, source, size in rows:
        lines.append(f"| `{slug}` | {source} | {size} |")
    SOURCES_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if failed:
        print(f"\n{len(failed)} missing", file=sys.stderr)
        return 1
    print(f"\nWrote {len(rows)} logos to {DEST}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
