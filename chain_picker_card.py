"""Render the /scan network picker as an image with real network logos.

Telegram inline buttons render text only, so a button can never show a
network's actual logo. Sending the picker as a photo with numbered tap
buttons underneath is the only way to show real artwork while keeping it
tappable. Numbers on the image match the button labels 1:1.
"""

from pathlib import Path
import tempfile
import uuid

from PIL import Image, ImageDraw, ImageFilter

import config
from chain_assets import chain_logo, paste_logo
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

WIDTH = 900
HEADER_HEIGHT = 132
ROW_HEIGHT = 84
FOOTER_HEIGHT = 74
LOGO_SIZE = 52
ACCENT = (224, 164, 88, 255)


def picker_rows(coverage):
    """Return ``[(slug, count), ...]`` busiest-first, drops only."""
    rows = [
        (chain, int(count))
        for chain, count in (coverage or {}).items()
        if count
    ]
    rows.sort(key=lambda item: (-item[1], item[0]))
    return rows


def build_picker_card(coverage, total_networks, output_dir=None):
    """Render the numbered network list and return the image path."""
    rows = picker_rows(coverage)
    height = HEADER_HEIGHT + max(1, len(rows)) * ROW_HEIGHT + FOOTER_HEIGHT
    image = _ground(height)
    draw = ImageDraw.Draw(image)

    total_drops = sum(count for _chain, count in rows)
    draw.text((44, 40), "PICK A NETWORK", font=_font(29, bold=True), fill=CREAM)
    if rows:
        word = "network" if len(rows) == 1 else "networks"
        subtitle = (
            f"{total_drops} drops live or opening soon across {len(rows)} {word}"
            "  ·  tap the matching number"
        )
    else:
        subtitle = "OpenSea has nothing scheduled on any network right now"
    draw.text((46, 80), subtitle, font=_font(18), fill=_alpha(CREAM_DIM, 235))

    y = HEADER_HEIGHT
    if not rows:
        draw.text(
            (46, y + 20),
            "This is OpenSea's own calendar, not a failed scan.",
            font=_font(19), fill=_alpha(CREAM_DIM, 210),
        )
    for index, (chain, count) in enumerate(rows, 1):
        _draw_row(image, index, chain, count, y)
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


def _draw_row(image, index, chain, count, y):
    top = y + 6
    _soft_rect(image, (34, top, WIDTH - 34, top + ROW_HEIGHT - 16), 18,
               fill=_alpha(INK, 96))
    draw = ImageDraw.Draw(image)

    colour = chain_color(chain, ACCENT)
    index_text = str(index)
    index_font = _font(22, bold=True)
    draw.text(
        (50, top + 22),
        index_text,
        font=index_font,
        fill=_alpha(CREAM, 220),
    )

    logo = chain_logo(chain, LOGO_SIZE)
    box = (88, top + 10, 88 + LOGO_SIZE, top + 10 + LOGO_SIZE)
    if not paste_logo(image, logo, box):
        draw = ImageDraw.Draw(image)
        draw.ellipse(box, fill=_alpha(colour, 70), outline=colour, width=2)
        letter = config.chain_label(chain)[:1].upper()
        font = _font(26, bold=True)
        draw.text(
            (box[0] + (LOGO_SIZE - _text_width(draw, letter, font)) // 2,
             box[1] + 12),
            letter, font=font, fill=CREAM,
        )
    else:
        draw = ImageDraw.Draw(image)
        draw.ellipse(box, outline=_alpha(CREAM, 70), width=2)

    name = config.chain_label(chain)
    draw.text((156, top + 14), name, font=_font(25, bold=True), fill=CREAM)

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
