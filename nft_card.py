"""Generate a compact Telegram image card for an NFT mint preview.

The card is intentionally a summary. Telegram buttons and the HTML caption
carry the clickable links because pixels inside a JPEG cannot be made into
reliable embedded links.
"""

from datetime import datetime
import io
import os
from pathlib import Path
import tempfile
import uuid

import httpx
from PIL import Image, ImageDraw, ImageFont, ImageOps


CARD_WIDTH = 1200
CARD_HEIGHT = 675
MAX_IMAGE_BYTES = 8 * 1024 * 1024
DEFAULT_ACCENT = "#63E6BE"
PERSISTENT_BACKGROUND = Path(__file__).resolve().parent / "state" / "nft-card-background.jpg"


def build_mint_card(candidate, research=None, output_dir=None):
    """Create a JPEG summary card and return its temporary path."""
    candidate = dict(candidate or {})
    research = dict(research or {})
    background_source = os.getenv("NFT_CARD_BACKGROUND", "").strip()
    if not background_source and PERSISTENT_BACKGROUND.is_file():
        background_source = str(PERSISTENT_BACKGROUND)
    background = _load_image(background_source) if background_source else None
    if background is None:
        background = _gradient_background()
    background = _cover(background.convert("RGB"), CARD_WIDTH, CARD_HEIGHT)

    image = background.convert("RGBA")
    overlay = Image.new("RGBA", image.size, (6, 10, 20, 145))
    image = Image.alpha_composite(image, overlay)
    draw = ImageDraw.Draw(image)

    accent = _parse_color(os.getenv("NFT_CARD_ACCENT_COLOR", DEFAULT_ACCENT), DEFAULT_ACCENT)
    white = (245, 248, 252, 255)
    muted = (185, 198, 215, 255)
    panel = (9, 17, 31, 205)
    panel_light = (18, 30, 49, 190)

    draw.rounded_rectangle((38, 34, CARD_WIDTH - 38, CARD_HEIGHT - 34), radius=30,
                           fill=(5, 11, 22, 105), outline=accent, width=2)
    draw.rounded_rectangle((64, 60, CARD_WIDTH - 64, 148), radius=22,
                           fill=panel)
    draw.text((92, 79), "NFT MINT CARD", font=_font(24, bold=True), fill=accent)
    brand = os.getenv("NFT_CARD_BRAND_NAME", "OpenSea Mint Bot").strip() or "OpenSea Mint Bot"
    brand_width = _text_width(draw, brand, _font(23, bold=True))
    draw.text((CARD_WIDTH - 92 - brand_width, 82), brand,
              font=_font(23, bold=True), fill=muted)

    name = str(candidate.get("name") or research.get("name") or candidate.get("slug") or "NFT mint")
    draw.text((92, 176), _clip(name, 40), font=_font(40, bold=True), fill=white)
    chain = str(candidate.get("chain") or "unknown").replace("_", " ").replace("-", " ").title()
    stage = str(candidate.get("stage_label") or "Stage unknown")
    draw.text((94, 228), f"{chain}  ·  {stage}", font=_font(23), fill=muted)

    # A small collection image is useful when the user has not supplied a
    # custom background. It is also kept optional so a broken CDN cannot block
    # card generation.
    image_url = research.get("image_url") or candidate.get("image_url")
    logo = _load_image(str(image_url or ""))
    if logo is not None:
        logo = ImageOps.fit(logo.convert("RGB"), (142, 142), method=_resample())
        logo_mask = Image.new("L", (142, 142), 0)
        ImageDraw.Draw(logo_mask).rounded_rectangle((0, 0, 141, 141), radius=24, fill=255)
        logo_rgba = logo.convert("RGBA")
        image.paste(logo_rgba, (CARD_WIDTH - 92 - 142, 180), logo_mask)
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((CARD_WIDTH - 92 - 142, 180, CARD_WIDTH - 92, 322),
                               radius=24, outline=accent, width=2)

    draw.rounded_rectangle((84, 292, 610, 590), radius=24, fill=panel)
    draw.rounded_rectangle((638, 292, CARD_WIDTH - 84, 590), radius=24, fill=panel_light)

    left_fields = [
        ("STATUS", _status(candidate)),
        ("PRICE", str(candidate.get("price_display") or "Price unknown")),
        ("ACCESS", str(candidate.get("access_label") or "Unknown")),
        ("QUANTITY", str(candidate.get("quantity") or 1)),
        ("OPENS", _time(candidate.get("start_time"))),
    ]
    right_fields = [
        ("SUPPLY", _supply(candidate, research)),
        ("FLOOR", _floor(research)),
        ("24H VOLUME", _volume(research)),
        ("OWNERS", _owners(research)),
        ("CONTRACT", _short_address(research.get("contract_address") or candidate.get("contract_address"))),
    ]
    _draw_fields(draw, left_fields, 112, 312, 53, accent, white, muted)
    _draw_fields(draw, right_fields, 666, 312, 53, accent, white, muted)

    output_root = Path(output_dir) if output_dir else Path(tempfile.gettempdir())
    output_root.mkdir(parents=True, exist_ok=True)
    output_path = output_root / f"opensea-mint-card-{uuid.uuid4().hex[:12]}.jpg"
    image.convert("RGB").save(output_path, format="JPEG", quality=91, optimize=True)
    return output_path


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


def _draw_fields(draw, fields, x, y, row_height, accent, white, muted):
    label_font = _font(16, bold=True)
    value_font = _font(23, bold=True)
    for label, value in fields:
        draw.text((x, y), label, font=label_font, fill=accent)
        value = _clip(str(value or "—"), 27)
        draw.text((x, y + 25), value, font=value_font, fill=white)
        y += row_height


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


def _gradient_background():
    image = Image.new("RGB", (CARD_WIDTH, CARD_HEIGHT))
    draw = ImageDraw.Draw(image)
    for y in range(CARD_HEIGHT):
        mix = y / max(1, CARD_HEIGHT - 1)
        color = (
            int(8 + 22 * mix),
            int(30 - 12 * mix),
            int(70 - 32 * mix),
        )
        draw.line((0, y, CARD_WIDTH, y), fill=color)
    return image


def _cover(image, width, height):
    return ImageOps.fit(image, (width, height), method=_resample(), centering=(0.5, 0.5))


def _resample():
    return getattr(Image, "Resampling", Image).LANCZOS


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
    return _parse_color(fallback, "63E6BE") if fallback != value else (99, 230, 190, 255)


def _status(candidate):
    try:
        if int(candidate.get("price_wei")) == 0:
            return "FREE MINT"
    except (TypeError, ValueError):
        pass
    price = str(candidate.get("price_display") or "").strip()
    return "PAID MINT" if price else "CHECK PRICE"


def _time(value):
    try:
        return datetime.fromtimestamp(int(value)).strftime("%d %b · %H:%M")
    except (TypeError, ValueError, OverflowError, OSError):
        return "Unknown"


def _supply(candidate, research):
    value = candidate.get("max_supply") or candidate.get("total_supply")
    value = value or research.get("unique_item_count") or research.get("total_supply")
    return str(value) if value not in (None, "") else "Unknown"


def _floor(research):
    latest = research.get("latest_floor") or {}
    if not isinstance(latest, dict):
        latest = {}
    # OpenSea's floor history rows contain ``floor_price`` in some versions
    # and ``usd_price``/``symbol`` in others. Keep the card honest when only
    # USD data is available.
    amount = latest.get("token_unit") or latest.get("floor_price") or latest.get("price") or latest.get("usd_price")
    unit = latest.get("symbol") or ("USD" if latest.get("usd_price") is not None else "")
    if amount is not None:
        return f"{amount} {unit or 'USD'}"
    total = research.get("stats_total") or {}
    if isinstance(total, dict) and total.get("floor_price") is not None:
        return f"{total.get('floor_price')} {total.get('floor_price_symbol') or ''}".strip()
    return "Unknown"


def _volume(research):
    one_day = research.get("stats_one_day") or {}
    if not isinstance(one_day, dict):
        return "Unknown"
    amount = one_day.get("volume")
    symbol = one_day.get("volume_symbol") or one_day.get("symbol") or ""
    return f"{amount} {symbol}".strip() if amount is not None else "Unknown"


def _owners(research):
    stats = research.get("stats_total") or {}
    if not isinstance(stats, dict):
        return "Unknown"
    return str(stats.get("num_owners") or "Unknown")


def _short_address(value):
    value = str(value or "").strip()
    if len(value) >= 12 and value.startswith("0x"):
        return f"{value[:6]}…{value[-4:]}"
    return value or "Unknown"


def _clip(value, length):
    text = " ".join(str(value or "").split())
    return text if len(text) <= length else text[:length - 1].rstrip() + "…"


def _text_width(draw, value, font):
    try:
        return int(draw.textbbox((0, 0), str(value), font=font)[2])
    except (AttributeError, TypeError):
        return int(draw.textlength(str(value), font=font))
