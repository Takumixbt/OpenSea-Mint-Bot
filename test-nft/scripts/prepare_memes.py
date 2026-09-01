"""Freeze a random selection of local meme images into an NFT upload bundle."""

from __future__ import annotations

import argparse
import json
import random
import shutil
from pathlib import Path


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
PLACEHOLDER_ASSET_CID = "REPLACE_ASSET_CID"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True, help="Folder containing source images")
    parser.add_argument("--output", type=Path, default=Path("upload"), help="Bundle output directory")
    parser.add_argument("--count", type=int, default=50, help="Number of NFTs to prepare")
    parser.add_argument("--seed", type=int, default=20260815, help="Seed for reproducible random selection")
    parser.add_argument(
        "--assets-cid",
        default=PLACEHOLDER_ASSET_CID,
        help="CID of the already-pinned assets folder (metadata uses this in image URLs)",
    )
    parser.add_argument("--clean", action="store_true", help="Remove the generated assets and metadata subfolders first")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    source = args.source.expanduser().resolve()
    output = args.output.expanduser().resolve()

    if not source.is_dir():
        raise SystemExit(f"Source folder does not exist: {source}")
    if args.count < 1:
        raise SystemExit("--count must be at least 1")

    candidates = sorted(
        path for path in source.rglob("*") if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS
    )
    if len(candidates) < args.count:
        raise SystemExit(f"Found {len(candidates)} images, but {args.count} were requested")

    assets_dir = output / "assets"
    metadata_dir = output / "metadata"
    if args.clean:
        for generated_dir in (assets_dir, metadata_dir):
            if generated_dir.exists():
                shutil.rmtree(generated_dir)
    assets_dir.mkdir(parents=True, exist_ok=True)
    metadata_dir.mkdir(parents=True, exist_ok=True)

    selected = random.Random(args.seed).sample(candidates, args.count)
    manifest = []
    for token_id, source_path in enumerate(selected, start=1):
        filename = f"{token_id}{source_path.suffix.lower()}"
        shutil.copy2(source_path, assets_dir / filename)
        image_uri = f"ipfs://{args.assets_cid}/{filename}"
        metadata = {
            "name": f"Takumi Rugs #{token_id}",
            "description": "Takumi Rugs is a zero-price NFT collection created to test a free-mint bot. Network gas still applies.",
            "image": image_uri,
            "external_url": "https://opensea.io/collection/takumi-rugs",
            "attributes": [
                {"trait_type": "Collection", "value": "Takumi Rugs"},
                {"trait_type": "Mint price", "value": "Free"},
                {"trait_type": "Image index", "value": token_id},
            ],
        }
        (metadata_dir / f"{token_id}.json").write_text(
            json.dumps(metadata, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        manifest.append({"token_id": token_id, "asset": filename, "source_filename": source_path.name})

    collection = {
        "name": "Takumi Rugs",
        "description": "A small public free-mint collection for testing an OpenSea mint bot.",
        "image": f"ipfs://{args.assets_cid}/{manifest[0]['asset']}",
        "external_url": "https://opensea.io/collection/takumi-rugs",
        "seller_fee_basis_points": 0,
        "fee_recipient": "0x0000000000000000000000000000000000000000",
    }
    (metadata_dir / "collection.json").write_text(
        json.dumps(collection, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    (output / "selection-manifest.json").write_text(
        json.dumps({"seed": args.seed, "source": str(source), "items": manifest}, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    print(f"Prepared {len(selected)} images in {output}")
    if args.assets_cid == PLACEHOLDER_ASSET_CID:
        print("Upload upload/assets first, then rerun with --assets-cid <ASSET_CID> before uploading upload/metadata.")
    else:
        print(f"Metadata now points to ipfs://{args.assets_cid}/<image-file>.")


if __name__ == "__main__":
    main()
