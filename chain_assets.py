"""Load committed network logos with alpha preserved.

Telegram buttons cannot show images, so logos only appear on the picker card
and the mint card. Files live in ``assets/chains/<slug>.png``. Missing files
are not fatal: callers fall back to a lettered disc.
"""

from pathlib import Path

from PIL import Image, ImageOps


ASSET_DIR = Path(__file__).resolve().parent / "assets" / "chains"
_CACHE = {}


def logo_path(chain_slug):
    slug = str(chain_slug or "").strip().lower()
    if not slug:
        return None
    path = ASSET_DIR / f"{slug}.png"
    return path if path.is_file() else None


def chain_logo(chain_slug, size):
    """Return a square RGBA logo, or None."""
    slug = str(chain_slug or "").strip().lower()
    size = int(size)
    key = (slug, size)
    if key in _CACHE:
        return _CACHE[key]
    path = logo_path(slug)
    logo = None
    if path is not None:
        try:
            with Image.open(path) as loaded:
                loaded.load()
                source = loaded.convert("RGBA")
            fitted = ImageOps.contain(
                source, (size, size), method=Image.Resampling.LANCZOS
            )
            canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
            canvas.paste(
                fitted,
                ((size - fitted.width) // 2, (size - fitted.height) // 2),
                fitted,
            )
            logo = canvas
        except (OSError, ValueError):
            logo = None
    _CACHE[key] = logo
    return logo


def logo_is_opaque(logo):
    """True when the mark is a solid square (GitHub-style avatar)."""
    if logo is None or logo.mode != "RGBA":
        return True
    extrema = logo.getextrema()
    if len(extrema) < 4:
        return True
    return extrema[3][0] >= 250


def paste_logo(image, logo, box, fallback_mask=None):
    """Paste a logo into ``box`` (left, top, right, bottom).

    Opaque square avatars are circle-cropped so they read as app icons.
    Marks that already carry transparency (rings, holes, wordmarks) are
    pasted as-is so the inner cutout of Base, OP, etc. survives.
    """
    left, top, right, bottom = box
    width, height = right - left, bottom - top
    if logo is None:
        return False
    if logo.size != (width, height):
        logo = logo.resize((width, height), Image.Resampling.LANCZOS)
    if logo_is_opaque(logo):
        mask = fallback_mask
        if mask is None:
            mask = Image.new("L", (width, height), 0)
            from PIL import ImageDraw
            ImageDraw.Draw(mask).ellipse((1, 1, width - 2, height - 2), fill=255)
        image.paste(logo, (left, top), mask)
    else:
        image.paste(logo, (left, top), logo)
    return True
