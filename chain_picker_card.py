"""Render the /scan network picker as an image with real network logos.

Telegram inline buttons render text and emoji only, so a button can never show
a network's actual logo. Sending the picker as a photo with the tap buttons
underneath is the only way to show real artwork while keeping it tappable.

Logos are read from the committed ``assets/chains/`` cache, so drawing this
never touches the network. A network with no cached logo falls back to a
lettered disc in its own colour, which is why a missing file is never fatal.
"""

from pathlib import Path
import tempfile
import uuid

from PIL import Image, ImageDraw, ImageFilter, ImageOps

import config
from nft_card import (
    CREAM,
    CREAM_DIM,
    INK,
    MOSS,
    MOSS_DEEP,
    _alpha,
    _font,
    _soft_rect,
    _text_width,
    chain_color,
)

ASSET_DIR = Path(__file__).resolve().parent / "assets" / "chains"

WIDTH = 900
HEADER_HEIGHT = 132
ROW_HEIGHT = 84
FOOTER_HEIGHT = 74
LOGO_SIZE = 52
ACCENT = (224, 164, 88, 255)

_LOGO_CACHE = {}


def build_picker_card(coverage, total_networks, output_dir=None):
    """Render the network list and return the image path.

    ``coverage`` is ``{chain_slug: drop_count}``; only networks with drops are
    drawn, because a network with nothing scheduled is not a choice worth
    offering as an equal.
    """
    rows = sorted(
        ((chain, int(count)) for chain, count in (coverage or {}).items() if count),
        key=lambda item: (-item[1], item[0]),
    )
    height = HEADER_HEIGHT + max(1, len(rows)) * ROW_HEIGHT + FOOTER_HEIGHT
    image = _ground(height)
    draw = ImageDraw.Draw(image)

    total_drops = sum(count for _chain, count in rows)
    draw.text((44, 40), "PICK A NETWORK", font=_font(29, bold=True), fill=CREAM)
    if rows:
        word = "network" if len(rows) == 1 else "networks"
        subtitle = f"{total_drops} drops live or opening soon across {len(rows)} {word}"
    else:
        subtitle = "OpenSea has nothing scheduled on any network right now"
    draw.text((46, 80), subtitle, font=_font(19), fill=_alpha(CREAM_DIM, 235))

    y = HEADER_HEIGHT
    if not rows:
        draw.text((46, y + 20),
                  "This is OpenSea's own calendar, not a failed scan.",
                  font=_font(19), fill=_alpha(CREAM_DIM, 210))
    for index, (chain, count) in enumerate(rows):
        _draw_row(image, chain, count, y, index == len(rows) - 1)
        y += ROW_HEIGHT

    quiet = max(0, int(total_networks) - len(rows))
    if quiet:
        draw = ImageDraw.Draw(image)
        draw.text(
            (46, height - FOOTER_HEIGHT + 22),
            f"{quiet} other supported networks have nothing scheduled",
            font=_font(18), fill=_alpha(CREAM_DIM, 190),
        )

    root = Path(output_dir) if output_dir else Path(tempfile.gettempdir())
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"scan-networks-{uuid.uuid4().hex[:12]}.jpg"
    image.convert("RGB").save(path, format="JPEG", quality=92, optimize=True)
    return path


def _ground(height):
    base = Image.new("RGB", (WIDTH, height))
    draw = ImageDraw.Draw(base)
    for y in range(height):
        mix = y / max(1, height - 1)
        draw.line((0, y, WIDTH, y), fill=tuple(
            int(MOSS_DEEP[c] + (MOSS[c] - MOSS_DEEP[c]) * mix) for c in range(3)
        ))
    image = base.convert("RGBA")
    glow = Image.new("L", image.size, 0)
    ImageDraw.Draw(glow).ellipse((-200, -320, WIDTH + 200, 260), fill=54)
    glow = glow.filter(ImageFilter.GaussianBlur(120)).point(lambda v: v // 3)
    image = Image.composite(
        Image.new("RGBA", image.size, (96, 132, 84, 255)), image, glow
    )
    ImageDraw.Draw(image).rounded_rectangle(
        (14, 14, WIDTH - 14, height - 14), radius=26, outline=ACCENT, width=3
    )
    return image


def _draw_row(image, chain, count, y, is_last):
    top = y + 6
    _soft_rect(image, (34, top, WIDTH - 34, top + ROW_HEIGHT - 16), 18,
               fill=_alpha(INK, 96))
    draw = ImageDraw.Draw(image)

    colour = chain_color(chain, ACCENT)
    logo = _logo(chain)
    box = (58, top + 10, 58 + LOGO_SIZE, top + 10 + LOGO_SIZE)
    if logo is not None:
        mask = Image.new("L", (LOGO_SIZE, LOGO_SIZE), 0)
        ImageDraw.Draw(mask).ellipse((0, 0, LOGO_SIZE - 1, LOGO_SIZE - 1), fill=255)
        image.paste(logo, (box[0], box[1]), mask)
        draw = ImageDraw.Draw(image)
        draw.ellipse(box, outline=_alpha(CREAM, 70), width=2)
    else:
        # No published logo for this network: a lettered disc in its own colour
        # still identifies it, and is better than an empty slot.
        draw.ellipse(box, fill=_alpha(colour, 70), outline=colour, width=2)
        letter = config.chain_label(chain)[:1].upper()
        font = _font(26, bold=True)
        draw.text(
            (box[0] + (LOGO_SIZE - _text_width(draw, letter, font)) // 2,
             box[1] + 12),
            letter, font=font, fill=CREAM,
        )

    name = config.chain_label(chain)
    draw.text((132, top + 14), name, font=_font(25, bold=True), fill=CREAM)

    label = f"{count} drop" if count == 1 else f"{count} drops"
    font = _font(21, bold=True)
    width = _text_width(draw, label, font)
    pill_left = WIDTH - 62 - width - 30
    _soft_rect(image, (pill_left, top + 14, WIDTH - 62, top + 52), 19,
               fill=_alpha(colour, 62))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((pill_left, top + 14, WIDTH - 62, top + 52),
                           radius=19, outline=colour, width=2)
    draw.text((pill_left + 15, top + 21), label, font=font, fill=CREAM)


def _logo(chain):
    """Return the cached logo for a network, or None."""
    slug = str(chain or "").strip().lower()
    if slug in _LOGO_CACHE:
        return _LOGO_CACHE[slug]
    path = ASSET_DIR / f"{slug}.png"
    logo = None
    try:
        if path.is_file():
            with Image.open(path) as loaded:
                loaded.load()
                logo = ImageOps.fit(
                    loaded.convert("RGB"), (LOGO_SIZE, LOGO_SIZE),
                    method=Image.Resampling.LANCZOS,
                )
    except (OSError, ValueError):
        logo = None
    _LOGO_CACHE[slug] = logo
    return logo
