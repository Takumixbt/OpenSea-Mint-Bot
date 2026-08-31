"""Generate the Telegram image card for a mint preview or a mint receipt.

The card is a summary, not a link surface: Telegram buttons and the HTML
caption carry every clickable link, because pixels inside a JPEG cannot.

Layout is deliberately artwork-first. The NFT being minted is the largest
element on the card, because that is the thing the operator actually wants to
see. Everything else - status, price, access, supply - sits in a single left
rail so the card stays readable on a phone.
"""

from datetime import datetime, timezone
import io
import os
import time
from pathlib import Path
import tempfile
import uuid

import httpx
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps


CARD_WIDTH = 1200
CARD_HEIGHT = 675
MAX_IMAGE_BYTES = 8 * 1024 * 1024
PERSISTENT_BACKGROUND = Path(__file__).resolve().parent / "state" / "nft-card-background.jpg"

# Warm swamp palette. Deep moss ground, cream type, amber accent: readable at
# Telegram's preview size and in both light and dark chat themes.
INK = (18, 26, 21, 255)
MOSS_DEEP = (24, 38, 30)
MOSS = (47, 79, 53)
CREAM = (239, 230, 208, 255)
CREAM_DIM = (176, 178, 152, 255)
AMBER = "#E0A458"
FROG = (122, 168, 87, 255)

# One colour per network, matching the glyphs used in the Telegram picker.
# Unlisted networks fall back to the card accent.
CHAIN_COLORS = {
    "ethereum": (108, 113, 235),
    "base": (0, 82, 255),
    "optimism": (255, 4, 32),
    "arbitrum": (18, 170, 255),
    "polygon": (130, 71, 229),
    "avalanche": (232, 65, 66),
    "zora": (255, 122, 0),
    "blast": (252, 252, 3),
    "shape": (72, 200, 255),
    "robinhood": (204, 255, 0),
    "abstract": (0, 209, 128),
    "monad": (131, 110, 249),
    "megaeth": (255, 214, 0),
    "hyperevm": (80, 213, 180),
    "ape_chain": (0, 84, 250),
    "bera_chain": (208, 116, 42),
    "ink": (117, 137, 255),
    "sei": (156, 28, 40),
    "soneium": (140, 140, 140),
    "ronin": (0, 130, 255),
    "unichain": (255, 0, 122),
    "flow": (0, 239, 139),
    "somnia": (255, 149, 0),
    "b3": (255, 122, 60),
    "gunzilla": (196, 158, 73),
    "animechain": (255, 105, 180),
    "stablechain": (0, 200, 120),
}


def chain_color(chain, fallback):
    """Return the network's brand colour, or the card accent."""
    value = CHAIN_COLORS.get(str(chain or "").strip().lower())
    return (value + (255,)) if value else fallback


STATUS_COLORS = {
    "confirmed": (122, 168, 87, 255),
    "sent": (224, 164, 88, 255),
    "reverted": (198, 88, 74, 255),
    "free": (122, 168, 87, 255),
    "paid": (224, 164, 88, 255),
    "unknown": (176, 178, 152, 255),
}


def build_mint_card(candidate, research=None, output_dir=None):
    """Create a JPEG summary card and return its temporary path."""
    candidate = dict(candidate or {})
    research = dict(research or {})
    accent = _parse_color(os.getenv("NFT_CARD_ACCENT_COLOR", AMBER), AMBER)

    image = _background(candidate, research)
    receipt_status = str(candidate.get("receipt_status") or "").strip().lower()
    artwork = _load_image(_nft_image_source(candidate, research))
    if artwork is None:
        artwork = _load_image(
            str(research.get("image_url") or candidate.get("image_url") or "")
        )

    _draw_frame(image, accent)
    _draw_header(image, candidate, research, accent, receipt_status)
    art_box = _draw_artwork(image, artwork, accent)
    _draw_identity(image, candidate, research, accent)
    _draw_rail(image, candidate, research, accent, receipt_status)
    _draw_footer(image, candidate, research, accent)
    _draw_mascot(image, receipt_status, candidate, art_box)

    output_root = Path(output_dir) if output_dir else Path(tempfile.gettempdir())
    output_root.mkdir(parents=True, exist_ok=True)
    output_path = output_root / f"opensea-mint-card-{uuid.uuid4().hex[:12]}.jpg"
    image.convert("RGB").save(output_path, format="JPEG", quality=92, optimize=True)
    return output_path


# ---------------------------------------------------------------------------
# Background and frame
# ---------------------------------------------------------------------------

def _background(candidate=None, research=None):
    """Return the card ground.

    An operator-installed background wins, because it is an explicit choice.
    Otherwise the collection's own banner (or logo) becomes the ground, so a
    card looks like the project it belongs to instead of looking generic.
    """
    candidate = candidate if isinstance(candidate, dict) else {}
    research = research if isinstance(research, dict) else {}

    source = os.getenv("NFT_CARD_BACKGROUND", "").strip()
    if not source and PERSISTENT_BACKGROUND.is_file():
        source = str(PERSISTENT_BACKGROUND)
    custom = _load_image(source) if source else None
    veil_alpha = 208

    if custom is None:
        for key in ("banner_image_url", "image_url"):
            candidate_source = str(
                research.get(key) or candidate.get(key) or ""
            ).strip()
            custom = _load_image(candidate_source)
            if custom is not None:
                # A logo is small and busy; blur it harder than a wide banner
                # so it reads as texture rather than a stretched icon.
                veil_alpha = 214 if key == "banner_image_url" else 224
                break

    if custom is not None:
        base = _cover(custom.convert("RGB"), CARD_WIDTH, CARD_HEIGHT)
        base = base.filter(ImageFilter.GaussianBlur(9)).convert("RGBA")
        # The ground must never fight the type, so sit it behind a heavy moss
        # veil rather than using it at full strength.
        veil = Image.new("RGBA", base.size, MOSS_DEEP + (veil_alpha,))
        return _add_glow(Image.alpha_composite(base, veil))

    base = Image.new("RGB", (CARD_WIDTH, CARD_HEIGHT))
    draw = ImageDraw.Draw(base)
    for y in range(CARD_HEIGHT):
        mix = y / max(1, CARD_HEIGHT - 1)
        draw.line(
            (0, y, CARD_WIDTH, y),
            fill=tuple(
                int(MOSS_DEEP[channel] + (MOSS[channel] - MOSS_DEEP[channel]) * mix)
                for channel in range(3)
            ),
        )
    return _add_glow(base.convert("RGBA"))


def _add_glow(image):
    """Add a faint top-light so flat grounds do not band on Telegram's JPEG."""
    mask = Image.new("L", (CARD_WIDTH, CARD_HEIGHT), 0)
    ImageDraw.Draw(mask).ellipse(
        (-260, -420, CARD_WIDTH + 260, CARD_HEIGHT + 120), fill=58
    )
    mask = mask.filter(ImageFilter.GaussianBlur(150)).point(lambda v: v // 3)
    warm = Image.new("RGBA", image.size, (96, 132, 84, 255))
    return Image.composite(warm, image, mask)


def _soft_rect(image, box, radius, fill=None, outline=None, width=1):
    """Blend a translucent rounded rectangle onto the card.

    ImageDraw sets pixels rather than compositing them, so a translucent fill
    drawn directly would survive as an opaque block once the card is flattened
    to JPEG. Drawing onto a scratch layer and alpha-compositing keeps the
    transparency real.
    """
    layer = Image.new("RGBA", image.size, (0, 0, 0, 0))
    ImageDraw.Draw(layer).rounded_rectangle(
        box, radius=radius, fill=fill, outline=outline, width=width
    )
    image.alpha_composite(layer)


def _soft_line(image, box, fill, width=1):
    """Blend a translucent hairline onto the card."""
    layer = Image.new("RGBA", image.size, (0, 0, 0, 0))
    ImageDraw.Draw(layer).line(box, fill=fill, width=width)
    image.alpha_composite(layer)


def _draw_frame(image, accent):
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle(
        (26, 26, CARD_WIDTH - 26, CARD_HEIGHT - 26),
        radius=34, outline=accent, width=3,
    )
    _soft_rect(
        image, (36, 36, CARD_WIDTH - 36, CARD_HEIGHT - 36),
        27, outline=_alpha(CREAM, 46), width=1,
    )


# ---------------------------------------------------------------------------
# Header, artwork, identity
# ---------------------------------------------------------------------------

def _draw_header(image, candidate, research, accent, receipt_status):
    draw = ImageDraw.Draw(image)
    status_text, status_key = _status(candidate, receipt_status)
    color = STATUS_COLORS.get(status_key, STATUS_COLORS["unknown"])

    font = _font(21, bold=True)
    pill_top, pill_height, pad_x = 60, 44, 22
    width = _text_width(draw, status_text, font) + pad_x * 2 + 24
    _soft_rect(
        image, (64, pill_top, 64 + width, pill_top + pill_height),
        pill_height // 2, fill=_alpha(color, 52),
    )
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle(
        (64, pill_top, 64 + width, pill_top + pill_height),
        radius=pill_height // 2, outline=color, width=2,
    )
    draw.ellipse(
        (64 + pad_x - 4, pill_top + 17, 64 + pad_x + 6, pill_top + 27), fill=color
    )
    draw.text((64 + pad_x + 18, pill_top + 11), status_text, font=font, fill=CREAM)

    brand = os.getenv("NFT_CARD_BRAND_NAME", "").strip() or "MINT BOT"
    brand_font = _font(19, bold=True)
    brand_width = _text_width(draw, brand, brand_font)
    _soft_text(
        image, (CARD_WIDTH - 64 - brand_width, pill_top + 14),
        brand, brand_font, _alpha(CREAM, 130),
    )


def _soft_text(image, position, text, font, fill):
    """Blend translucent text so faint labels do not flatten to solid."""
    layer = Image.new("RGBA", image.size, (0, 0, 0, 0))
    ImageDraw.Draw(layer).text(position, text, font=font, fill=fill)
    image.alpha_composite(layer)


def _draw_artwork(image, artwork, accent):
    """Place the NFT art as the hero element and return its box."""
    box = (700, 132, CARD_WIDTH - 64, CARD_HEIGHT - 96)
    left, top, right, bottom = box
    width, height = right - left, bottom - top
    draw = ImageDraw.Draw(image)

    if artwork is None:
        _soft_rect(image, box, 26, fill=_alpha(INK, 130),
                   outline=_alpha(CREAM, 52), width=2)
        label = "ARTWORK UNAVAILABLE"
        font = _font(19, bold=True)
        _soft_text(
            image,
            (left + (width - _text_width(draw, label, font)) // 2, top + height // 2 - 10),
            label, font, _alpha(CREAM, 120),
        )
        return box

    # A soft drop shadow lifts the art off the ground without a hard border.
    shadow = Image.new("RGBA", image.size, (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle(
        (left + 8, top + 14, right + 8, bottom + 14), radius=26, fill=(0, 0, 0, 120)
    )
    image.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(18)))

    fitted = ImageOps.fit(artwork.convert("RGB"), (width, height), method=_resample())
    mask = Image.new("L", (width, height), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, width - 1, height - 1), radius=26, fill=255)
    image.paste(fitted.convert("RGBA"), (left, top), mask)

    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle(box, radius=26, outline=accent, width=3)
    return box


def _draw_identity(image, candidate, research, accent):
    draw = ImageDraw.Draw(image)
    name = str(
        candidate.get("name") or research.get("name") or candidate.get("slug") or "NFT mint"
    )
    # Two lines of a large face beats one clipped line: collection names are
    # long and the name is the first thing read.
    lines = _wrap(draw, name, _font(43, bold=True), 560, max_lines=2)
    y = 140
    for line in lines:
        draw.text((66, y), line, font=_font(43, bold=True), fill=CREAM)
        y += 50

    raw_chain = str(candidate.get("chain") or "unknown")
    chain = raw_chain.replace("_", " ").replace("-", " ").title()
    badge = chain_color(raw_chain, accent)

    # A filled chip in the network's own colour, so the chain is identifiable
    # before the label is read.
    font = _font(19, bold=True)
    label_width = _text_width(draw, chain, font)
    top = y + 4
    height = 34
    right = 66 + label_width + 46
    _soft_rect(image, (66, top, right, top + height), height // 2,
               fill=_alpha(badge, 58))
    draw.rounded_rectangle((66, top, right, top + height),
                           radius=height // 2, outline=badge, width=2)
    draw.ellipse((84, top + 12, 94, top + 22), fill=badge)
    draw.text((104, top + 7), chain, font=font, fill=CREAM)

    stage = str(candidate.get("stage_label") or "Stage unknown")
    _soft_text(
        image, (right + 16, top + 8), _clip(stage, 24),
        _font(19), _alpha(CREAM_DIM, 235),
    )


def _draw_rail(image, candidate, research, accent, receipt_status):
    """One left-hand column of label/value pairs, ordered by what matters."""
    if receipt_status:
        fields = [
            ("PAID", str(candidate.get("mint_value_display")
                         or candidate.get("price_display") or "Unknown")),
            ("NETWORK FEE", str(candidate.get("gas_display") or "Unknown")),
            ("QUANTITY", str(candidate.get("quantity") or 1)),
            ("EST. P&L", str(candidate.get("pnl_display") or "Unavailable")),
        ]
    else:
        fields = [
            ("PRICE", str(candidate.get("price_display") or "Price unknown")),
            ("ACCESS", str(candidate.get("access_label") or "Unknown")),
            ("QUANTITY", str(candidate.get("quantity") or 1)),
            _timing_field(candidate),
        ]

    top, row_height = 296, 68
    bottom = top + row_height * len(fields)
    _soft_rect(image, (56, top - 20, 648, bottom + 6), 22, fill=_alpha(INK, 128))

    draw = ImageDraw.Draw(image)
    label_font, value_font = _font(15, bold=True), _font(26, bold=True)
    y = top
    for index, (label, value) in enumerate(fields):
        draw.text((84, y), label, font=label_font, fill=accent)
        draw.text((84, y + 23), _clip(value, 29), font=value_font, fill=CREAM)
        if index < len(fields) - 1:
            # The rule belongs at the row boundary. Sitting it just under the
            # value made every value look underlined.
            rule_y = y + row_height - 8
            _soft_line(image, (84, rule_y, 620, rule_y), _alpha(CREAM, 30), width=1)
        y += row_height


def _draw_footer(image, candidate, research, accent):
    supply = _supply(candidate, research)
    contract = _short_address(
        research.get("contract_address") or candidate.get("contract_address")
    )
    parts = []
    if supply != "Unknown":
        parts.append(f"Supply {supply}")
    floor = _floor(research)
    if floor != "Unknown":
        parts.append(f"Floor {floor}")
    if contract != "Unknown":
        parts.append(contract)
    if not parts:
        return
    _soft_text(
        image, (66, CARD_HEIGHT - 92),
        _clip("  \u00b7  ".join(parts), 52), _font(19), _alpha(CREAM_DIM, 210),
    )


# ---------------------------------------------------------------------------
# Mascot
# ---------------------------------------------------------------------------

def _draw_mascot(image, receipt_status, candidate, art_box):
    """Draw the frog badge whose expression follows the mint outcome.

    It is drawn from primitives rather than shipped as an asset so the repo
    stays dependency-free and the badge always matches the palette.
    """
    _status_text, key = _status(candidate, receipt_status)
    size = 84
    # Overlap the artwork's top-left corner so the badge belongs to the art
    # rather than floating in the gap between the two columns.
    left = art_box[0] - size // 2
    top = art_box[1] - size // 3

    badge = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(badge)
    body = STATUS_COLORS.get(key, FROG)

    draw.ellipse((4, 10, size - 4, size - 4), fill=body)
    # Eyes sit on top of the head like a frog's, which is what makes the
    # silhouette read as apu rather than as a generic circle.
    for eye_x in (18, size - 46):
        draw.ellipse((eye_x, 2, eye_x + 28, 30), fill=body)
        draw.ellipse((eye_x + 7, 9, eye_x + 21, 23), fill=(250, 250, 245, 255))
        draw.ellipse((eye_x + 12, 13, eye_x + 20, 21), fill=(20, 24, 20, 255))

    mouth_y = size - 32
    if key in {"confirmed", "free"}:
        draw.arc((22, mouth_y - 18, size - 22, mouth_y + 10), 15, 165,
                 fill=(20, 30, 22, 255), width=4)
    elif key == "reverted":
        draw.arc((22, mouth_y - 2, size - 22, mouth_y + 26), 195, 345,
                 fill=(20, 30, 22, 255), width=4)
    else:
        draw.line((26, mouth_y + 2, size - 26, mouth_y + 2),
                  fill=(20, 30, 22, 255), width=4)

    ring = Image.new("RGBA", (size + 16, size + 16), (0, 0, 0, 0))
    ImageDraw.Draw(ring).ellipse(
        (0, 0, size + 15, size + 15), fill=MOSS_DEEP + (255,), outline=body, width=3
    )
    ring.alpha_composite(badge, (8, 8))
    image.alpha_composite(ring, (int(left) - 8, int(top) - 8))


# ---------------------------------------------------------------------------
# Background installation (unchanged public API)
# ---------------------------------------------------------------------------

def install_card_background(source):
    """Validate and persist a custom background from a local path or URL."""
    image = _load_image(source)
    if image is None:
        raise ValueError("the background must be a valid JPG, PNG, or WEBP under 8 MB")
    PERSISTENT_BACKGROUND.parent.mkdir(parents=True, exist_ok=True)
    temporary = PERSISTENT_BACKGROUND.with_suffix(".tmp.jpg")
    _cover(ImageOps.exif_transpose(image).convert("RGB"), CARD_WIDTH, CARD_HEIGHT).save(
        temporary, format="JPEG", quality=92, optimize=True
    )
    temporary.replace(PERSISTENT_BACKGROUND)
    return PERSISTENT_BACKGROUND


def clear_card_background():
    """Restore the built-in background and report whether one was removed."""
    try:
        PERSISTENT_BACKGROUND.unlink()
        return True
    except FileNotFoundError:
        return False


# ---------------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------------

def _status(candidate, receipt_status=None):
    """Return ``(display_text, palette_key)`` for the card's status pill."""
    receipt_status = str(
        receipt_status if receipt_status is not None
        else candidate.get("receipt_status") or ""
    ).strip().lower()
    if receipt_status:
        return {
            "confirmed": ("MINTED", "confirmed"),
            "sent": ("BROADCAST", "sent"),
            "reverted": ("REVERTED", "reverted"),
        }.get(receipt_status, (receipt_status.upper(), "unknown"))
    try:
        if int(candidate.get("price_wei")) == 0:
            return "FREE MINT", "free"
    except (TypeError, ValueError):
        pass
    if str(candidate.get("price_display") or "").strip():
        return "PAID MINT", "paid"
    return "CHECK PRICE", "unknown"


def _nft_image_source(candidate, research):
    """Find an explicit NFT image without mistaking a collection logo for one."""
    candidate = candidate if isinstance(candidate, dict) else {}
    research = research if isinstance(research, dict) else {}

    def image_from(item):
        if not isinstance(item, dict):
            return ""
        for key in (
            "image_original_url", "image_url", "display_image_url",
            "image_preview_url", "image", "imageUrl",
            "nft_image_url", "token_image_url", "asset_image_url",
        ):
            value = str(item.get(key) or "").strip()
            if value.startswith(("https://", "http://")) or Path(value).is_file():
                return value
        return ""

    source = image_from(research.get("asset_nft"))
    if source:
        return source
    for sample in research.get("sample_nfts") or []:
        source = image_from(sample)
        if source:
            return source
    for key in ("nft_image_url", "token_image_url", "asset_image_url"):
        source = image_from({key: candidate.get(key)})
        if source:
            return source
    return ""


def _load_image(source):
    source = str(source or "").strip()
    if not source:
        return None
    try:
        if source.startswith(("https://", "http://")):
            response = httpx.get(source, timeout=5.0, follow_redirects=True)
            if response.status_code != 200 or len(response.content) > MAX_IMAGE_BYTES:
                return None
            payload = response.content
        else:
            path = Path(source).expanduser()
            if not path.is_file() or path.stat().st_size > MAX_IMAGE_BYTES:
                return None
            payload = path.read_bytes()
        with Image.open(io.BytesIO(payload)) as loaded:
            return loaded.convert("RGBA")
    except (OSError, ValueError, httpx.HTTPError):
        return None


def _cover(image, width, height):
    return ImageOps.fit(image, (width, height), method=_resample(), centering=(0.5, 0.5))


def _resample():
    return getattr(Image, "Resampling", Image).LANCZOS


def _alpha(color, value):
    return tuple(color[:3]) + (int(value),)


def _font(size, bold=False):
    candidates = []
    if os.name == "nt":
        candidates.extend([
            r"C:\Windows\Fonts\segoeuib.ttf" if bold else r"C:\Windows\Fonts\segoeui.ttf",
            r"C:\Windows\Fonts\arialbd.ttf" if bold else r"C:\Windows\Fonts\arial.ttf",
        ])
    candidates.extend([
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold
        else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf" if bold
        else "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    ])
    for candidate in candidates:
        try:
            return ImageFont.truetype(candidate, size=size)
        except OSError:
            continue
    return ImageFont.load_default()


def _parse_color(value, fallback):
    value = str(value or "").strip().lstrip("#")
    if len(value) == 6:
        try:
            return tuple(int(value[index:index + 2], 16) for index in (0, 2, 4)) + (255,)
        except ValueError:
            pass
    return _parse_color(fallback, "E0A458") if fallback != value else (224, 164, 88, 255)


def _timing_field(candidate):
    """Return the timing row, phrased for whether the stage is open yet.

    Showing "OPENS" beside a timestamp that has already passed reads as though
    the mint were still pending, so a live stage reports its closing time.
    """
    now = time.time()
    start = _epoch(candidate.get("start_time"))
    end = _epoch(candidate.get("end_time"))
    if start is not None and start > now:
        return "OPENS", _time(start)
    if start is not None:
        if end is None:
            return "STATUS", "Live now"
        if end >= now:
            return "LIVE UNTIL", _time(end)
        return "CLOSED", _time(end)
    return "OPENS", "Unknown"


def _epoch(value):
    try:
        value = int(value)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _time(value):
    try:
        moment = datetime.fromtimestamp(int(value), timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        return "Unknown"
    return moment.strftime("%d %b %H:%M UTC")


def _supply(candidate, research):
    value = candidate.get("max_supply") or candidate.get("total_supply")
    value = value or research.get("unique_item_count") or research.get("total_supply")
    return str(value) if value not in (None, "") else "Unknown"


def _floor(research):
    latest = research.get("latest_floor") or {}
    if not isinstance(latest, dict):
        latest = {}
    # OpenSea's floor history rows carry ``floor_price`` in some versions and
    # ``usd_price``/``symbol`` in others. Keep the card honest when only USD
    # data is available.
    amount = (
        latest.get("token_unit") or latest.get("floor_price")
        or latest.get("price") or latest.get("usd_price")
    )
    unit = latest.get("symbol") or ("USD" if latest.get("usd_price") is not None else "")
    if amount is not None:
        return f"{amount} {unit or 'USD'}"
    total = research.get("stats_total") or {}
    if isinstance(total, dict) and total.get("floor_price") is not None:
        return f"{total.get('floor_price')} {total.get('floor_price_symbol') or ''}".strip()
    return "Unknown"


def _short_address(value):
    value = str(value or "").strip()
    if len(value) >= 12 and value.startswith("0x"):
        return f"{value[:6]}\u2026{value[-4:]}"
    return value or "Unknown"


def _clip(value, length):
    text = " ".join(str(value or "").split())
    return text if len(text) <= length else text[:length - 1].rstrip() + "\u2026"


def _wrap(draw, value, font, max_width, max_lines=2):
    """Wrap to a pixel width so long collection names never overrun the rail."""
    words = " ".join(str(value or "").split()).split(" ")
    lines, current, truncated = [], "", False
    for index, word in enumerate(words):
        trial = f"{current} {word}".strip()
        if current and _text_width(draw, trial, font) > max_width:
            lines.append(current)
            current = word
            if len(lines) == max_lines:
                # Words remain but there is no room for them, so the final
                # line has to show that the name was cut rather than imply
                # the collection is called something it is not.
                truncated = index < len(words)
                current = ""
                break
        else:
            current = trial
    if current and len(lines) < max_lines:
        lines.append(current)
    if not lines:
        return [""]
    if truncated:
        lines[-1] = lines[-1].rstrip() + "\u2026"
    while _text_width(draw, lines[-1], font) > max_width and len(lines[-1]) > 1:
        lines[-1] = lines[-1][:-2].rstrip() + "\u2026"
    return lines


def _text_width(draw, value, font):
    try:
        return int(draw.textbbox((0, 0), str(value), font=font)[2])
    except (AttributeError, TypeError):
        return int(draw.textlength(str(value), font=font))
