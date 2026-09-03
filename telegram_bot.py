"""Professional Telegram control panel for the NFT Mint Bot.

The bot uses Telegram's HTTPS Bot API directly. The interface is intentionally
button-first: discovery, research, live scheduling, and mint confirmation are
available from the dashboard.
"""

from datetime import datetime
from decimal import Decimal
import hashlib
import html
import json
import os
import re
import shlex
import threading
import time
import tempfile
from pathlib import Path

import httpx
from dotenv import load_dotenv

import config
from daily_runner import (
    DailyMintService,
    is_free_public_candidate,
    quantity_limit,
    redact_secrets,
    same_candidate_stage,
    validate_quantity,
)
from chain_picker_card import build_picker_card
from nft_card import (
    PERSISTENT_BACKGROUND,
    build_mint_card,
    clear_card_background,
    install_card_background,
)


ROOT = Path(__file__).resolve().parent
TELEGRAM_MAX_TEXT = 3500
CANDIDATE_PAGE_SIZE = 8
SCHEDULE_STAGE_PAGE_SIZE = 8
QUANTITY_OPTIONS = (1, 2, 3, 5, 10, 25, 50, 100)
MAX_QUANTITY = 100
HTML_MODE = "HTML"
TELEGRAM_MAX_CAPTION = 1000


def esc(value):
    """Escape dynamic text before it is placed in Telegram HTML."""
    # Telegram's HTML parser documents only the four named entities below.
    # html.escape emits ``&#x27;`` for apostrophes, which is unnecessary in
    # text and can be rejected by Telegram in some clients.
    return html.escape(str(value), quote=True).replace("&#x27;", "'")


def pretty_chain(chain):
    """Network display name, shared with config so labels never drift."""
    return config.chain_label(chain)


def short_text(value, length=28):
    text = " ".join(str(value).split())
    return text if len(text) <= length else text[: max(1, length - 1)] + "…"


def candidate_token(candidate):
    """Return a short identity token so old inline buttons cannot mint a new candidate."""
    # Older saved scans did not carry an explicit route. Treat those records as
    # the normal OpenSea drop route so a fresh scan does not invalidate their
    # buttons merely because route metadata was added later.
    route = candidate.get("route") or ("opensea_drop" if candidate.get("slug") else "")
    stage_identity = str(candidate.get("stage_id") or "").strip().lower()
    if not stage_identity:
        stage_identity = ":".join(str(candidate.get(name, "")) for name in (
            "stage_index", "start_time", "stage_type"
        ))
    # Bind the button to the stable OpenSea stage UUID and the exact terms the
    # user saw. A stage reorder remains valid; a price/access change does not.
    identity = ":".join(str(value) for value in (
        candidate.get("chain", ""),
        candidate.get("slug", ""),
        stage_identity,
        candidate.get("start_time", ""),
        candidate.get("end_time", ""),
        candidate.get("price_wei", ""),
        candidate.get("access_label", ""),
        candidate.get("contract_address", ""),
        route,
    ))
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:10]


def project_token(candidate):
    identity = f"{candidate.get('chain', '')}:{candidate.get('slug', '')}"
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:10]


def project_groups(candidates, indexes=None):
    """Group mint windows by collection while preserving scan order/indexes.

    ``indexes`` is used by a chain-filtered view.  The Telegram callback still
    needs the candidate's absolute position in the service's last scan, even
    when the visible list contains only one network.
    """
    grouped = []
    positions = {}
    candidates = list(candidates or [])
    if indexes is None:
        indexes = range(1, len(candidates) + 1)
    for index, candidate in zip(indexes, candidates):
        key = (str(candidate.get("chain") or "").lower(), str(candidate.get("slug") or "").lower())
        if key not in positions:
            positions[key] = len(grouped)
            grouped.append({"candidate": candidate, "options": []})
        grouped[positions[key]]["options"].append((index, candidate))
    return grouped


def candidate_badge(candidate):
    """Return a compact status label for the all-mints Telegram list."""
    if candidate.get("is_sold_out") is True:
        return "⛔ SOLD OUT"
    if candidate.get("is_free") is True and candidate.get("is_public") is True:
        return "🟢 FREE + PUBLIC"
    if candidate.get("is_free") is True:
        return "🟡 FREE + ELIGIBILITY CHECK"
    if str(candidate.get("price_display", "")).lower().startswith("paid"):
        return "💰 PAID"
    return "⚪ PRICE CHECK NEEDED"


def format_time(timestamp):
    try:
        return datetime.fromtimestamp(
            int(timestamp), config.discovery_timezone()
        ).strftime("%d %b · %H:%M")
    except (TypeError, ValueError, OverflowError, OSError):
        return "time unavailable"


def format_saved_time(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.astimezone(config.discovery_timezone()).strftime("%d %b · %H:%M")
    except (TypeError, ValueError):
        return "Not scanned yet"


def safe_http_url(value):
    """Return an escaped http(s) URL suitable for a Telegram HTML href."""
    raw = str(value or "").strip()
    if raw.startswith(("https://", "http://")):
        return esc(raw)
    return ""


def embedded_link(label, url):
    href = safe_http_url(url)
    return f'<a href="{href}">{esc(label)}</a>' if href else esc(label)


def shorten_description(value, length=700):
    text = " ".join(str(value or "").split())
    if len(text) <= length:
        return text
    return text[: length - 1].rstrip() + "…"


def description_block(candidate):
    """Return a consistent, escaped collection-description section."""
    description = shorten_description((candidate or {}).get("description"))
    if description:
        return f"<b>Description:</b>\n{esc(description)}"
    return "<b>Description:</b>\n<i>OpenSea did not provide a collection description.</i>"


CHAIN_EXPLORER_ADDRESS_URLS = {
    slug: f"{str(settings.get('explorer') or '').rstrip('/')}/address/{{address}}"
    for slug, settings in config.CHAIN_CONFIGS.items()
    if settings.get("explorer")
}

CHAIN_EXPLORER_TX_URLS = {
    chain: template.replace("/address/{address}", "/tx/{tx_hash}")
    for chain, template in CHAIN_EXPLORER_ADDRESS_URLS.items()
}


def explorer_tx_url(chain, tx_hash):
    template = CHAIN_EXPLORER_TX_URLS.get(str(chain or "").lower())
    value = str(tx_hash or "").strip()
    return template.format(tx_hash=value) if template and value.startswith("0x") else ""


def format_native_wei(value, symbol="native"):
    try:
        amount = Decimal(int(value)) / Decimal(10 ** 18)
    except (TypeError, ValueError):
        return "Unavailable"
    text = format(amount, ".12f").rstrip("0").rstrip(".") or "0"
    return f"{text} {symbol}"


def format_wallet_balance(value, symbol="native"):
    """Format wallet balances for a compact Telegram dashboard."""
    try:
        amount = Decimal(int(value)) / Decimal(10 ** 18)
    except (TypeError, ValueError):
        return "Unavailable"
    if amount == 0:
        return f"0 {symbol}"
    if amount < Decimal("0.000001"):
        return f"<0.000001 {symbol}"
    decimals = 8 if amount < Decimal("0.01") else 4
    text = format(amount, f".{decimals}f").rstrip("0").rstrip(".")
    return f"{text} {symbol}"


class TelegramAPI:
    def __init__(self, token):
        self.token = token
        self.client = httpx.Client(timeout=40.0)
        self.base_url = f"https://api.telegram.org/bot{token}"

    def close(self):
        self.client.close()

    def call(self, method, payload=None, timeout=None):
        try:
            response = self.client.post(
                f"{self.base_url}/{method}",
                json=payload or {},
                timeout=timeout,
            )
            data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise RuntimeError(f"Telegram request failed ({type(exc).__name__})") from exc
        if not isinstance(data, dict) or not data.get("ok"):
            description = data.get("description") if isinstance(data, dict) else None
            raise RuntimeError(redact_secrets(description or f"Telegram rejected {method}"))
        return data.get("result")

    def send_message(self, chat_id, text, reply_markup=None, parse_mode=None):
        payload = {"chat_id": chat_id, "text": str(text)}
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        if parse_mode:
            payload["parse_mode"] = parse_mode
        return self.call("sendMessage", payload)

    def send_photo(self, chat_id, photo_path, caption=None, reply_markup=None, parse_mode=None):
        """Upload one local Telegram photo using the multipart Bot API route."""
        data = {"chat_id": str(chat_id)}
        if caption:
            data["caption"] = str(caption)[:TELEGRAM_MAX_CAPTION]
        if reply_markup is not None:
            data["reply_markup"] = json.dumps(reply_markup, separators=(",", ":"))
        if parse_mode:
            data["parse_mode"] = parse_mode
        try:
            with open(photo_path, "rb") as photo:
                response = self.client.post(
                    f"{self.base_url}/sendPhoto",
                    data=data,
                    files={"photo": (Path(photo_path).name, photo, "image/jpeg")},
                    timeout=40.0,
                )
            payload = response.json()
        except (OSError, httpx.HTTPError, ValueError) as exc:
            raise RuntimeError(f"Telegram photo upload failed ({type(exc).__name__})") from exc
        if not isinstance(payload, dict) or not payload.get("ok"):
            description = payload.get("description") if isinstance(payload, dict) else None
            raise RuntimeError(redact_secrets(description or "Telegram rejected sendPhoto"))
        return payload.get("result")

    def download_file(self, file_id, destination):
        """Download one Telegram file to a local temporary destination."""
        info = self.call("getFile", {"file_id": str(file_id)}, timeout=20) or {}
        file_path = str(info.get("file_path") or "")
        if not file_path:
            raise RuntimeError("Telegram did not return a file path")
        try:
            response = self.client.get(
                f"https://api.telegram.org/file/bot{self.token}/{file_path}",
                timeout=30.0,
            )
            response.raise_for_status()
            if len(response.content) > 8 * 1024 * 1024:
                raise RuntimeError("background image is larger than 8 MB")
            Path(destination).write_bytes(response.content)
        except RuntimeError:
            raise
        except (OSError, httpx.HTTPError) as exc:
            raise RuntimeError(f"Telegram file download failed ({type(exc).__name__})") from exc
        return Path(destination)

    def edit_message_text(self, chat_id, message_id, text, reply_markup=None, parse_mode=None):
        payload = {"chat_id": chat_id, "message_id": message_id, "text": str(text)}
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        if parse_mode:
            payload["parse_mode"] = parse_mode
        return self.call("editMessageText", payload)

    def delete_message(self, chat_id, message_id):
        """Remove one message, ignoring the usual "already gone" rejections."""
        try:
            return self.call(
                "deleteMessage",
                {"chat_id": chat_id, "message_id": message_id},
                timeout=15,
            )
        except RuntimeError:
            # Telegram refuses to delete a message that is missing or older
            # than 48 hours. Neither is worth surfacing to the operator.
            return None

    def answer_callback(self, callback_query_id, text=None, show_alert=False):
        payload = {"callback_query_id": callback_query_id, "show_alert": show_alert}
        if text:
            payload["text"] = str(text)[:200]
        return self.call("answerCallbackQuery", payload, timeout=15)

    def send(self, chat_id, text):
        """Send plain-text operational notifications in safe-sized chunks."""
        text = str(text) or " "
        for start in range(0, len(text), TELEGRAM_MAX_TEXT):
            self.send_message(chat_id, text[start:start + TELEGRAM_MAX_TEXT])


class TelegramBot:
    def __init__(self, api, service, allowed_chat_id=None):
        self.api = api
        self.service = service
        self.allowed_chat_id = int(allowed_chat_id) if allowed_chat_id else None
        self.job_lock = threading.Lock()
        self.input_lock = threading.RLock()
        self.pending_inputs = {}
        self.schedule_drafts = {}
        self.research_drafts = {}
        self.quantity_choices = {}
        self.wallet_choices = {}
        self.last_scan_chain = None
        # The newest picture-based screen per chat, so refreshing one replaces
        # it instead of stacking a new image on every tap.
        self.photo_screens = {}
        self.offset = None

    # ------------------------------------------------------------------
    # Process lifecycle and update routing
    # ------------------------------------------------------------------

    def notify_operator(self, message):
        """Send runner notifications without allowing external HTML injection."""
        if self.allowed_chat_id is None:
            return
        try:
            self.api.send_message(
                self.allowed_chat_id,
                "🔔 <b>NFT Mint Bot update</b>\n\n" + esc(message),
                parse_mode=HTML_MODE,
            )
        except Exception as exc:
            print(
                f"Telegram notification failed: {type(exc).__name__}: "
                f"{redact_secrets(exc)}",
                flush=True,
            )

    def prewarm_calendar(self):
        """Load the drop calendar in the background so the first scan is warm."""
        def done(counts, errors, age):
            if counts is None:
                print("Drop calendar prewarm skipped.", flush=True)
                return
            total = sum(counts.values())
            print(
                f"Drop calendar ready: {total} drops across "
                f"{len(counts)} network(s).",
                flush=True,
            )

        warmer = getattr(self.service, "prewarm_calendar", None)
        if callable(warmer):
            warmer(on_done=done)
            return

        reader = getattr(self.service, "chain_coverage", None)
        if not callable(reader):
            return

        def warm():
            try:
                counts, errors, age = reader()
                done(counts, errors, age)
            except Exception:
                done(None, None, None)

        threading.Thread(target=warm, name="calendar-prewarm", daemon=True).start()

    def run_forever(self):
        self.api.call("deleteWebhook", {"drop_pending_updates": False}, timeout=15)
        self.api.call(
            "setMyCommands",
            {"commands": self.command_menu()},
            timeout=15,
        )
        self.prewarm_calendar()
        print("Telegram bot is running. Press Ctrl-C to stop.", flush=True)
        while True:
            try:
                payload = {
                    "timeout": config.TELEGRAM_POLL_TIMEOUT_SECONDS,
                    "allowed_updates": ["message", "edited_message", "callback_query"],
                }
                if self.offset is not None:
                    payload["offset"] = self.offset
                updates = self.api.call(
                    "getUpdates",
                    payload,
                    timeout=config.TELEGRAM_POLL_TIMEOUT_SECONDS + 10,
                ) or []
                for update in updates:
                    self.offset = int(update["update_id"]) + 1
                    self.handle_update(update)
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                safe_error = redact_secrets(exc)
                if "conflict" in safe_error.lower() and "getupdates" in safe_error.lower():
                    print(
                        "Another bot instance is already using this token for "
                        "getUpdates. Stop the other instance, then start this one again.",
                        flush=True,
                    )
                    return False
                print(
                    f"Telegram polling error: {type(exc).__name__}: {safe_error}",
                    flush=True,
                )
                time.sleep(5)

    def handle_update(self, update):
        callback = update.get("callback_query")
        if callback:
            self.handle_callback(callback)
            return

        message = update.get("message") or update.get("edited_message") or {}
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        text = (message.get("text") or "").strip()
        if chat_id is None:
            return
        if not self.authorize_chat(chat_id):
            return

        pending = self._pending_input(chat_id)
        if pending and pending.get("kind") == "card_background" and not text.startswith("/"):
            try:
                self.handle_background_input(chat_id, message, pending)
            except Exception as exc:
                self._send_error(chat_id, exc)
            return
        if not text:
            return

        # A schedule can be started with the button-first flow. The next plain
        # text message is treated as the OpenSea URL; slash commands still
        # work so the operator can send /cancel or /home at any time.
        if not text.startswith("/"):
            pending = self._consume_pending_input(chat_id)
            if pending:
                try:
                    if pending.get("kind") == "schedule_url":
                        self.start_schedule_lookup(chat_id, text)
                    elif pending.get("kind") == "research_url":
                        self.start_research_lookup(chat_id, text)
                    elif pending.get("kind") == "quantity":
                        self.handle_quantity_input(chat_id, text, pending)
                    elif pending.get("kind") == "price_cap":
                        self.handle_price_cap_input(chat_id, text)
                    elif pending.get("kind") == "buy_cap":
                        self.handle_buy_cap_input(chat_id, text)
                    elif pending.get("kind") == "card_accent":
                        self.handle_accent_input(chat_id, text)
                    elif pending.get("kind") == "card_brand":
                        self.handle_brand_input(chat_id, text)
                except Exception as exc:
                    self._send_error(chat_id, exc)
                return

        try:
            parts = shlex.split(text)
        except ValueError:
            self._send(chat_id, self.error_card("Could not parse that command. Use /help."))
            return
        command = parts[0].split("@", 1)[0].lower()
        args = parts[1:]
        try:
            self.dispatch_message(chat_id, command, args)
        except Exception as exc:
            self._send_error(chat_id, exc)

    def handle_callback(self, callback):
        callback_id = callback.get("id")
        message = callback.get("message") or {}
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        message_id = message.get("message_id")
        data = str(callback.get("data") or "")
        if chat_id is None:
            self._answer_callback(callback_id, "Missing chat context", True)
            return
        if not self.authorize_chat(chat_id, callback_id=callback_id):
            return
        self._answer_callback(callback_id)
        try:
            self.dispatch_callback(chat_id, message_id, data)
        except Exception as exc:
            self._send_error(chat_id, exc, message_id=message_id)

    def authorize_chat(self, chat_id, callback_id=None):
        if self.allowed_chat_id is None:
            if callback_id:
                self._answer_callback(callback_id, "Authorize this chat first", True)
            else:
                self._send(
                    chat_id,
                    "🔐 <b>Authorization required</b>\n\n"
                    "Add this chat ID to <code>TELEGRAM_ALLOWED_CHAT_ID</code> "
                    "in .env, then restart:\n\n"
                    f"<code>{esc(chat_id)}</code>",
                )
            return False
        if int(chat_id) != self.allowed_chat_id:
            if callback_id:
                self._answer_callback(callback_id, "This chat is not authorized", True)
            return False
        return True

    # ------------------------------------------------------------------
    # Text commands
    # ------------------------------------------------------------------

    def dispatch_message(self, chat_id, command, args):
        if command in {"/start", "/home", "/dashboard"}:
            self.show_home(chat_id)
        elif command == "/help":
            self._send(chat_id, self.help_text(), self.help_keyboard())
        elif command == "/status":
            self.show_status(chat_id)
        elif command in {"/wallet", "/portfolio"}:
            self.start_wallet_status(chat_id, args[0].lower() if args else None)
        elif command in {"/mints", "/history"}:
            self.show_mint_history(chat_id)
        elif command == "/scan":
            self.start_scan(chat_id, args[0].lower() if args else None)
        elif command == "/candidates":
            self.show_candidates(chat_id)
        elif command == "/daily":
            self.handle_daily_command(chat_id, args)
        elif command == "/mint":
            self.handle_mint_command(chat_id, args)
        elif command == "/schedule":
            self.handle_schedule_command(chat_id, args)
        elif command in {"/info", "/research"}:
            self.handle_research_command(chat_id, args)
        elif command in {"/schedules", "/schedule_list"}:
            self.show_schedules(chat_id)
        elif command == "/settings":
            self.show_settings(chat_id)
        elif command == "/cancel":
            self._clear_pending_input(chat_id)
            self._send(chat_id, "↩️ <b>Input cancelled.</b>", self.home_keyboard())
        elif command == "/stop":
            self.service.stop()
            self.show_daily(
                chat_id,
                notice="⏹ <b>Stop requested.</b> The current network call will finish safely, then the runner will stop.",
            )
        else:
            self._send(chat_id, "Unknown command. Open the dashboard with /home.", self.home_keyboard())

    def handle_daily_command(self, chat_id, args):
        if not args:
            self.show_daily(chat_id)
            return
        mode = args[0].lower()
        if mode != "live" or len(args) < 2 or args[1].upper() != "CONFIRM":
            self.show_live_daily_confirmation(chat_id)
            return
        self.start_daily(chat_id)

    def handle_mint_command(self, chat_id, args):
        if not args or not args[0].isdigit():
            self.show_candidates(chat_id, notice="Choose a mint option, review it, then confirm the live transaction.")
            return
        index = int(args[0])
        if len(args) < 2 or args[-1].upper() != "CONFIRM":
            self.show_live_mint_confirmation(chat_id, index)
            return
        self.start_mint(chat_id, index)

    def handle_schedule_command(self, chat_id, args):
        if args:
            self.start_schedule_lookup(chat_id, " ".join(args))
        else:
            self.begin_schedule_input(chat_id)

    def handle_research_command(self, chat_id, args):
        if args:
            self.start_research_lookup(chat_id, " ".join(args))
        else:
            self.begin_research_input(chat_id)

    # ------------------------------------------------------------------
    # Callback buttons
    # ------------------------------------------------------------------

    def dispatch_callback(self, chat_id, message_id, data):
        if data == "home":
            self.show_home(chat_id, message_id)
        elif data == "help":
            self._present(chat_id, self.help_text(), self.help_keyboard(), message_id)
        elif data == "status":
            self.show_status(chat_id, message_id)
        elif data == "wallet":
            self.start_wallet_status(chat_id, message_id=message_id)
        elif data.startswith("wallet:chain:"):
            parts = data.split(":")
            self.start_wallet_status(
                chat_id,
                parts[2],
                message_id,
                parts[3] if len(parts) > 3 else "primary",
            )
        elif data.startswith("wallet:profile:"):
            self.start_wallet_status(
                chat_id,
                message_id=message_id,
                wallet_id=data.rsplit(":", 1)[-1],
            )
        elif data == "wallet:mints":
            self.show_mint_history(chat_id, message_id)
        elif data == "chains":
            self.show_chain_picker(chat_id, message_id)
        elif data == "chains:all":
            self.show_chain_picker(chat_id, message_id, show_empty=True)
        elif data == "chains:refresh":
            self.show_chain_picker(chat_id, message_id, force=True)
        elif data == "candidates":
            self.show_candidates(chat_id, message_id=message_id)
        elif data.startswith("candidates:chain:"):
            parts = data.split(":")
            if len(parts) != 5 or parts[3] != "page" or not parts[4].isdigit():
                raise ValueError("invalid candidate network page")
            self.show_candidates(
                chat_id,
                message_id=message_id,
                page=int(parts[4]),
                scan_chain=parts[2],
            )
        elif data.startswith("candidates:page:"):
            page_text = data.rsplit(":", 1)[-1]
            if not page_text.isdigit():
                raise ValueError("invalid candidate page")
            self.show_candidates(chat_id, message_id=message_id, page=int(page_text))
        elif data.startswith("project:mint:") or data.startswith("project:schedule:"):
            self.dispatch_project_action(chat_id, message_id, data)
        elif data.startswith("project:"):
            self.show_project(chat_id, data.split(":", 1)[1], message_id)
        elif data == "daily":
            self.show_daily(chat_id, message_id=message_id)
        elif data == "daily:live":
            self.show_live_daily_confirmation(chat_id, message_id)
        elif data == "daily:live:confirm":
            self.start_daily(chat_id, message_id)
        elif data == "noop":
            return
        elif data == "daily:stop":
            self.service.stop()
            self.show_daily(
                chat_id,
                message_id=message_id,
                notice="⏹ <b>Stop requested.</b> The current network call will finish safely, then the runner will stop.",
            )
        elif data == "schedule:new":
            self.begin_schedule_input(chat_id, message_id)
        elif data == "settings":
            self.show_settings(chat_id, message_id)
        elif data == "settings:bg":
            self.begin_background_input(chat_id, message_id)
        elif data == "settings:bg:reset":
            clear_card_background()
            self._set_env_value("NFT_CARD_BACKGROUND", "")
            self.show_settings(chat_id, message_id, notice="✅ <b>Built-in card background restored.</b>")
        elif data == "settings:cap":
            self.begin_price_cap_input(chat_id, message_id)
        elif data == "settings:buycap":
            self.begin_buy_cap_input(chat_id, message_id)
        elif data == "settings:accent":
            self.begin_accent_input(chat_id, message_id)
        elif data == "settings:brand":
            self.begin_brand_input(chat_id, message_id)
        elif data == "settings:preview":
            self.send_settings_preview(chat_id, message_id)
        elif data in {"research:new", "info:new"}:
            self.begin_research_input(chat_id, message_id)
        elif data.startswith("research:"):
            self.dispatch_research_callback(chat_id, message_id, data)
        elif data.startswith("info:"):
            self.dispatch_info_callback(chat_id, message_id, data)
        elif data == "schedules":
            self.show_schedules(chat_id, message_id=message_id)
        elif data.startswith("schedule:"):
            self.dispatch_schedule_callback(chat_id, message_id, data)
        elif data.startswith("scan:"):
            self.start_scan(chat_id, data.split(":", 1)[1], message_id)
        elif data.startswith("candidate:"):
            index, candidate = self._candidate_from_ref(data)
            self.show_candidate(chat_id, index, message_id, candidate)
        elif data.startswith("card:"):
            self.dispatch_card_callback(chat_id, message_id, data)
        elif data.startswith("mint:"):
            self.dispatch_mint_callback(chat_id, message_id, data)
        elif data.startswith("quantity:"):
            self.dispatch_quantity_callback(chat_id, message_id, data)
        elif data.startswith("wallets:"):
            self.dispatch_wallets_callback(chat_id, message_id, data)
        else:
            self._present(chat_id, "That button has expired. Open the dashboard again.", self.home_keyboard(), message_id)

    def dispatch_project_action(self, chat_id, message_id, data):
        parts = data.split(":")
        if len(parts) != 4 or parts[0] != "project" or parts[1] not in {"mint", "schedule"}:
            raise ValueError("invalid project action")
        group, index, candidate = self._project_option_from_ref(parts[2], parts[3])
        if parts[1] == "mint":
            self.show_live_mint_confirmation(
                chat_id,
                index,
                message_id,
                self._with_quantity(chat_id, candidate),
            )
            return
        self._save_schedule_draft(chat_id, [candidate])
        self.show_schedule_stage(chat_id, candidate, message_id)

    def dispatch_schedule_callback(self, chat_id, message_id, data):
        parts = data.split(":")
        if data == "schedule:stages":
            self.show_schedule_stages(chat_id, 0, message_id)
        elif len(parts) == 3 and parts[1] == "stage":
            candidate = self._schedule_candidate_from_ref(chat_id, parts[2])
            self.show_schedule_stage(chat_id, candidate, message_id)
        elif len(parts) == 3 and parts[1] == "page" and parts[2].isdigit():
            self.show_schedule_stages(chat_id, int(parts[2]), message_id)
        elif len(parts) == 3 and parts[1] == "live":
            candidate = self._schedule_candidate_from_ref(chat_id, parts[2])
            self.show_live_schedule_confirmation(chat_id, candidate, message_id)
        elif len(parts) == 4 and parts[1] == "mint" and parts[2] == "live":
            candidate = self._schedule_candidate_from_ref(chat_id, parts[3])
            self.show_live_draft_mint_confirmation(
                chat_id, candidate, parts[3], message_id, context="schedule"
            )
        elif len(parts) == 5 and parts[1:4] == ["mint", "live", "confirm"]:
            candidate = self._schedule_candidate_from_ref(chat_id, parts[4])
            self.start_draft_mint(chat_id, candidate, parts[4], message_id)
        elif len(parts) == 4 and parts[1:3] == ["live", "confirm"]:
            candidate = self._schedule_candidate_from_ref(chat_id, parts[3])
            self.arm_schedule(chat_id, candidate, message_id)
        elif len(parts) == 3 and parts[1] == "id":
            self.show_schedule_detail(chat_id, parts[2], message_id)
        elif len(parts) == 3 and parts[1] == "cancel":
            self.service.cancel_schedule(parts[2])
            self.show_schedules(
                chat_id,
                message_id=message_id,
                notice="✅ <b>Schedule cancelled.</b>",
            )
        elif len(parts) == 4 and parts[1] == "candidate":
            index, candidate = self._candidate_from_ref(f"candidate:{parts[2]}:{parts[3]}")
            self._save_schedule_draft(chat_id, [candidate])
            self.show_schedule_stage(chat_id, candidate, message_id)
        else:
            raise ValueError("invalid or expired schedule button")

    def dispatch_info_callback(self, chat_id, message_id, data):
        parts = data.split(":")
        if len(parts) == 4 and parts[1] == "candidate" and parts[2].isdigit():
            index, candidate = self._candidate_from_ref(
                f"candidate:{parts[2]}:{parts[3]}"
            )
            self.start_candidate_research(chat_id, index, candidate, message_id)
        elif len(parts) == 3 and parts[1] == "schedule":
            candidate = self._schedule_candidate_from_ref(chat_id, parts[2])
            self.start_schedule_research(chat_id, candidate, parts[2], message_id)
        else:
            raise ValueError("invalid or expired info button")

    def dispatch_card_callback(self, chat_id, message_id, data):
        parts = data.split(":")
        if len(parts) == 4 and parts[1] == "candidate" and parts[2].isdigit():
            index, candidate = self._candidate_from_ref(
                f"candidate:{parts[2]}:{parts[3]}"
            )
            self.start_candidate_card(chat_id, index, candidate, message_id)
        elif len(parts) == 3 and parts[1] == "schedule":
            candidate = self._schedule_candidate_from_ref(chat_id, parts[2])
            self.start_schedule_card(chat_id, candidate, parts[2], message_id)
        else:
            raise ValueError("invalid or expired card button")

    def dispatch_research_callback(self, chat_id, message_id, data):
        parts = data.split(":")
        if len(parts) != 3 or parts[2] == "":
            raise ValueError("invalid or expired research button")
        token = parts[2]
        draft = self._research_draft(chat_id, token)
        action = parts[1]
        if action == "view":
            self.show_research_draft(chat_id, token, message_id)
        elif action == "card":
            candidate = self._research_candidate(draft)
            if not candidate:
                raise ValueError("this research result has no mintable stage for a card")
            self.start_research_card(chat_id, token, candidate, message_id)
        elif action == "stages":
            candidates = draft.get("mint_candidates") or []
            if not candidates:
                raise ValueError("OpenSea did not return an active/upcoming mint stage")
            self._save_schedule_draft(chat_id, candidates)
            self.show_schedule_stages(chat_id, 0, message_id)
        elif action == "live":
            candidate = self._research_candidate(draft)
            if not candidate:
                raise ValueError("this research result has no mintable stage")
            self.show_live_draft_mint_confirmation(chat_id, candidate, token, message_id)
        elif action == "confirm" and len(parts) == 3:
            # ``research:confirm:<token>`` is only used by a live confirmation
            # screen after the operator has already seen the warning.
            candidate = self._research_candidate(draft)
            if not candidate:
                raise ValueError("this research result has no mintable stage")
            self.start_draft_mint(chat_id, candidate, token, message_id)
        elif action == "buy":
            self.show_purchase_confirmation(chat_id, token, draft, message_id)
        elif action == "buyconfirm":
            self.start_purchase(chat_id, token, draft, message_id)
        else:
            raise ValueError("invalid or expired research button")

    def show_purchase_confirmation(self, chat_id, token, draft, message_id=None):
        research = draft.get("research") or draft
        slug = str(research.get("slug") or "")
        preview = self.service.purchase_preview(slug)
        preview["name"] = research.get("name") or slug
        with self.input_lock:
            saved = (self.research_drafts.get(int(chat_id)) or {}).get(str(token))
            if saved is not None:
                saved["best_listing"] = preview
                if isinstance(saved.get("research"), dict):
                    saved["research"]["best_listing"] = preview
        wallet = self._public_wallets()[0]
        over_cap = int(preview.get("price_wei") or 0) > config.MAX_BUY_VALUE_WEI
        cap_note = (
            "❌ This listing is above your buy cap. Increase it in Settings before confirming."
            if over_cap else
            "✅ Exact listing and price will be checked again before signing."
        )
        rows = []
        if not over_cap:
            rows.append([self.button("✅ Buy this NFT", f"research:buyconfirm:{token}")])
        rows.extend([
            [self.button("⚙️ Buy price cap", "settings:buycap")],
            [self.button("↩️ Back", f"research:view:{token}")],
        ])
        text = (
            "⚠️ <b>Confirm OpenSea purchase</b>\n\n"
            f"<b>Collection:</b> {embedded_link(preview.get('name'), f'https://opensea.io/collection/{slug}')}\n"
            f"<b>NFT:</b> {embedded_link('#' + str(preview.get('token_id')), preview.get('opensea_url'))}\n"
            f"<b>Chain:</b> {esc(pretty_chain(preview.get('chain')))}\n"
            f"<b>Current total price:</b> {esc(preview.get('price_display'))}\n"
            f"<b>Buy cap:</b> {esc(config.MAX_BUY_PRICE_NATIVE)} native coin\n"
            f"<b>Wallet:</b> {esc(wallet.get('label'))} · <code>{esc(wallet.get('address'))}</code>\n\n"
            f"{esc(cap_note)}\n\n"
            "This is a secondary-market purchase, not a mint. The confirmation buys exactly "
            "the displayed token from its exact signed listing; it never substitutes a more expensive item."
        )
        self._present(chat_id, text, self.markup(rows), message_id)

    def start_purchase(self, chat_id, token, draft, message_id=None):
        preview = (draft.get("research") or draft).get("best_listing") or draft.get("best_listing")
        if not preview:
            raise ValueError("the listing preview expired; open Buy current price again")
        self._background(
            chat_id,
            "Buying exact OpenSea listing",
            lambda target_id: self.purchase_job(chat_id, preview, target_id),
            message_id,
        )

    def purchase_job(self, chat_id, preview, target_id):
        result = self.service.buy_listing(preview, "primary")
        tx_hash = result.get("tx_hash")
        chain = (result.get("candidate") or {}).get("chain")
        link = embedded_link("View transaction", explorer_tx_url(chain, tx_hash))
        status = (
            "✅ Confirmed" if result.get("confirmed") is True
            else "❌ Reverted" if result.get("confirmed") is False
            else "📡 Broadcast"
        )
        self._present(
            chat_id,
            "<b>🛍 OpenSea purchase</b>\n\n"
            f"<b>Status:</b> {status}\n"
            f"<b>NFT:</b> #{esc((result.get('candidate') or {}).get('token_id'))}\n"
            f"<b>Price:</b> {esc((result.get('candidate') or {}).get('price_display'))}\n"
            f"<b>Transaction:</b> {link}",
            self.home_keyboard(),
            target_id,
        )

    def dispatch_mint_callback(self, chat_id, message_id, data):
        parts = data.split(":")
        if len(parts) not in {4, 5}:
            raise ValueError("invalid mint button")
        index = int(parts[1])
        token = parts[2]
        candidate = self._candidate_from_ref(f"candidate:{index}:{token}")[1]
        candidate = self._with_quantity(chat_id, candidate)
        action = parts[3]
        if action == "live" and len(parts) == 4:
            self.show_live_mint_confirmation(chat_id, index, message_id, candidate)
        elif action == "live" and len(parts) == 5 and parts[4] == "confirm":
            self.start_mint(
                chat_id,
                index,
                message_id,
                candidate,
            )
        else:
            raise ValueError("invalid mint button")

    def dispatch_quantity_callback(self, chat_id, message_id, data):
        # A button choice supersedes any waiting custom-number message.
        self._clear_pending_input(chat_id)
        parts = data.split(":")
        if len(parts) == 4 and parts[1] == "candidate" and parts[2].isdigit():
            index = int(parts[2])
            candidate = self._candidate_from_ref(f"candidate:{index}:{parts[3]}")[1]
            self.show_quantity_picker(chat_id, candidate, "candidate", index, message_id)
        elif len(parts) == 3 and parts[1] == "schedule":
            candidate = self._schedule_candidate_from_ref(chat_id, parts[2])
            self.show_quantity_picker(chat_id, candidate, "schedule", None, message_id)
        elif len(parts) == 6 and parts[1:3] == ["set", "candidate"]:
            if not parts[3].isdigit() or not parts[5].isdigit():
                raise ValueError("invalid quantity button")
            index = int(parts[3])
            candidate = self._candidate_from_ref(f"candidate:{index}:{parts[4]}")[1]
            self.set_quantity(chat_id, candidate, "candidate", index, int(parts[5]), message_id)
        elif len(parts) == 5 and parts[1:3] == ["set", "schedule"]:
            if not parts[4].isdigit():
                raise ValueError("invalid quantity button")
            candidate = self._schedule_candidate_from_ref(chat_id, parts[3])
            self.set_quantity(chat_id, candidate, "schedule", None, int(parts[4]), message_id)
        elif len(parts) == 5 and parts[1:3] == ["custom", "candidate"]:
            if not parts[3].isdigit():
                raise ValueError("invalid quantity button")
            index = int(parts[3])
            candidate = self._candidate_from_ref(f"candidate:{index}:{parts[4]}")[1]
            self.begin_custom_quantity_input(chat_id, candidate, "candidate", index, message_id)
        elif len(parts) == 4 and parts[1:3] == ["custom", "schedule"]:
            candidate = self._schedule_candidate_from_ref(chat_id, parts[3])
            self.begin_custom_quantity_input(chat_id, candidate, "schedule", None, message_id)
        else:
            raise ValueError("invalid or expired quantity button")

    def dispatch_wallets_callback(self, chat_id, message_id, data):
        parts = data.split(":")
        if len(parts) == 4 and parts[1] == "candidate" and parts[2].isdigit():
            index = int(parts[2])
            candidate = self._candidate_from_ref(f"candidate:{index}:{parts[3]}")[1]
            self.show_wallet_picker(chat_id, candidate, "candidate", index, message_id)
            return
        if len(parts) == 3 and parts[1] == "schedule":
            candidate = self._schedule_candidate_from_ref(chat_id, parts[2])
            self.show_wallet_picker(chat_id, candidate, "schedule", None, message_id)
            return
        if len(parts) == 6 and parts[1] == "toggle" and parts[2] == "candidate":
            if not parts[3].isdigit():
                raise ValueError("invalid wallet button")
            index = int(parts[3])
            candidate = self._candidate_from_ref(f"candidate:{index}:{parts[4]}")[1]
            self.toggle_wallet(chat_id, candidate, parts[5], "candidate", index, message_id)
            return
        if len(parts) == 5 and parts[1] == "toggle" and parts[2] == "schedule":
            candidate = self._schedule_candidate_from_ref(chat_id, parts[3])
            self.toggle_wallet(chat_id, candidate, parts[4], "schedule", None, message_id)
            return
        raise ValueError("invalid or expired wallet button")

    # ------------------------------------------------------------------
    # Actions and background jobs
    # ------------------------------------------------------------------

    def start_scan(self, chat_id, chain=None, message_id=None):
        chain = str(chain or "").strip().lower()
        if not chain:
            self.show_chain_picker(chat_id, message_id)
            return
        if chain == "all":
            self._background(
                chat_id,
                "Scanning OpenSea networks",
                lambda target_id: self.scan_job(chat_id, "all", target_id),
                message_id,
            )
            return
        if chain not in self.service.supported_chains():
            self.show_chain_picker(chat_id, message_id)
            return
        self._background(
            chat_id,
            f"Scanning {pretty_chain(chain)}",
            lambda target_id: self.scan_job(chat_id, chain, target_id),
            message_id,
        )

    def scan_job(self, chat_id, chain, target_id):
        scan_chain = None if chain == "all" else chain
        candidates, errors = self.service.scan_now(scan_chain)
        self.last_scan_chain = chain
        if chain == "all":
            self._present(
                chat_id,
                self.render_chain_summary(candidates, errors),
                self.chain_summary_keyboard(candidates),
                target_id,
            )
            return
        title = f"🎨 <b>OpenSea mints · {esc(pretty_chain(chain))}</b>"
        self._present(
            chat_id,
            self.render_candidates(
                candidates,
                errors,
                title=title,
            ),
            self.candidates_keyboard(candidates, scan_chain=chain),
            target_id,
        )

    # ------------------------------------------------------------------
    # One-time URL scheduling
    # ------------------------------------------------------------------

    def begin_schedule_input(self, chat_id, message_id=None):
        with self.input_lock:
            self.pending_inputs[int(chat_id)] = {
                "kind": "schedule_url",
                "expires_at": time.time() + 10 * 60,
            }
        self._present(
            chat_id,
            "<b>📌 Schedule one mint</b>\n\n"
            "Send an OpenSea <b>collection, drop, item, or asset URL</b> (or a collection slug) as your next message.\n\n"
            "Every safe mint route resolved from that OpenSea link will be shown. Pick one, "
            "review its current price and access rule, then arm it.\n\n"
            "<i>Marketplace listings are not mint transactions. If OpenSea exposes no hosted stage "
            "and no verified on-chain route, the bot stops without signing.</i>",
            self.schedule_input_keyboard(),
            message_id,
        )

    def start_schedule_lookup(self, chat_id, value, message_id=None):
        self._clear_pending_input(chat_id)
        self._background(
            chat_id,
            "Inspect OpenSea mint route",
            lambda target_id: self.schedule_lookup_job(chat_id, value, target_id),
            message_id,
        )

    def schedule_lookup_job(self, chat_id, value, target_id):
        candidates = self.service.inspect_drop(value)
        self._save_schedule_draft(chat_id, candidates)
        self._present(
            chat_id,
            self.render_schedule_stages(candidates, page=0),
            self.schedule_stage_keyboard(candidates, page=0),
            target_id,
        )

    def begin_research_input(self, chat_id, message_id=None):
        with self.input_lock:
            self.pending_inputs[int(chat_id)] = {
                "kind": "research_url",
                "expires_at": time.time() + 10 * 60,
            }
        self._present(
            chat_id,
            "<b>ℹ️ OpenSea mint info</b>\n\n"
            "Paste an OpenSea <b>collection, drop, item, or asset URL</b> (or a slug).\n\n"
            "The bot resolves hosted stages first, then shows a direct SeaDrop or verified "
            "contract route when it can simulate one safely.",
            self.research_input_keyboard(),
            message_id,
        )

    def start_research_lookup(self, chat_id, value, message_id=None):
        self._clear_pending_input(chat_id)
        self._background(
            chat_id,
            "Load OpenSea mint info",
            lambda target_id: self.research_lookup_job(chat_id, value, target_id),
            message_id,
        )

    def research_lookup_job(self, chat_id, value, target_id):
        research = self.service.research_reference(value)
        candidates = research.get("mint_candidates") or []
        if candidates:
            self._save_schedule_draft(chat_id, candidates)
        token = self._save_research_draft(chat_id, research)
        self._present(
            chat_id,
            self.render_research(research),
            self.research_keyboard(token, research),
            target_id,
        )

    def start_candidate_research(self, chat_id, index, candidate, message_id=None):
        self._background(
            chat_id,
            "Load mint info",
            lambda target_id: self.candidate_research_job(
                chat_id, index, candidate, target_id
            ),
            message_id,
        )

    def candidate_research_job(self, chat_id, index, candidate, target_id):
        research = self.service.research_candidate(candidate)
        fresh = research.get("candidate")
        if isinstance(fresh, dict):
            fresh = dict(fresh)
            fresh["quantity"] = candidate.get("quantity", fresh.get("quantity", 1))
            if candidate.get("wallet_ids"):
                fresh["wallet_ids"] = list(candidate["wallet_ids"])
            research["candidate"] = fresh
        research["candidate_index"] = index
        stages = [
            fresh if isinstance(fresh, dict) and same_candidate_stage(fresh, item) else dict(item)
            for item in (research.get("mint_candidates") or [])
            if isinstance(item, dict)
        ]
        research["mint_candidates"] = stages
        if stages:
            self._save_schedule_draft(chat_id, stages)
        self._save_schedule_draft(chat_id, [candidate])
        token = self._save_research_draft(chat_id, research)
        self._present(
            chat_id,
            self.render_research(research),
            self.research_keyboard(token, research),
            target_id,
        )

    def start_schedule_research(self, chat_id, candidate, token, message_id=None):
        self._background(
            chat_id,
            "Load scheduled mint info",
            lambda target_id: self.schedule_research_job(
                chat_id, candidate, token, target_id
            ),
            message_id,
        )

    def schedule_research_job(self, chat_id, candidate, schedule_token, target_id):
        research = self.service.research_candidate(candidate)
        fresh = research.get("candidate")
        if isinstance(fresh, dict):
            fresh = dict(fresh)
            fresh["quantity"] = candidate.get("quantity", fresh.get("quantity", 1))
            if candidate.get("wallet_ids"):
                fresh["wallet_ids"] = list(candidate["wallet_ids"])
            research["candidate"] = fresh
        research["schedule_token"] = schedule_token
        stages = [
            fresh if isinstance(fresh, dict) and same_candidate_stage(fresh, item) else dict(item)
            for item in (research.get("mint_candidates") or [])
            if isinstance(item, dict)
        ]
        research["mint_candidates"] = stages
        if stages:
            self._save_schedule_draft(chat_id, stages)
        self._save_schedule_draft(chat_id, [candidate])
        token = self._save_research_draft(chat_id, research)
        self._present(
            chat_id,
            self.render_research(research),
            self.research_keyboard(token, research),
            target_id,
        )

    def start_candidate_card(self, chat_id, index, candidate, message_id=None):
        self._background(
            chat_id,
            "Create NFT mint card",
            lambda target_id: self.candidate_card_job(
                chat_id, index, candidate, target_id
            ),
            message_id,
        )

    def candidate_card_job(self, chat_id, index, candidate, target_id):
        research = self.service.research_candidate(candidate)
        self.send_card(
            chat_id,
            candidate,
            research,
            self.candidate_card_keyboard(index, candidate),
            target_id,
        )

    def start_schedule_card(self, chat_id, candidate, token, message_id=None):
        self._background(
            chat_id,
            "Create scheduled mint card",
            lambda target_id: self.schedule_card_job(
                chat_id, candidate, token, target_id
            ),
            message_id,
        )

    def schedule_card_job(self, chat_id, candidate, token, target_id):
        research = self.service.research_candidate(candidate)
        self.send_card(
            chat_id,
            candidate,
            research,
            self.schedule_card_keyboard(candidate, token),
            target_id,
        )

    def start_research_card(self, chat_id, token, candidate, message_id=None):
        self._background(
            chat_id,
            "Create mint card",
            lambda target_id: self.research_card_job(
                chat_id, token, candidate, target_id
            ),
            message_id,
        )

    def research_card_job(self, chat_id, token, candidate, target_id):
        draft = self._research_draft(chat_id, token)
        research = draft.get("research") or draft
        self.send_card(
            chat_id,
            candidate,
            research,
            self.research_card_keyboard(token, candidate, research),
            target_id,
        )

    def send_card(self, chat_id, candidate, research, keyboard, target_id=None):
        path = None
        try:
            path = build_mint_card(candidate, research)
            caption = self.card_caption(candidate, research)
            self.api.send_photo(
                chat_id,
                path,
                caption=caption,
                reply_markup=keyboard,
                parse_mode=HTML_MODE,
            )
            self._present(
                chat_id,
                "✅ <b>NFT mint card sent.</b>\n\n"
                "Use the buttons on the image for OpenSea, mint info, and the available mint actions.",
                keyboard,
                target_id,
            )
        finally:
            if path is not None:
                try:
                    Path(path).unlink()
                except OSError:
                    pass

    def start_draft_mint(self, chat_id, candidate, token, message_id=None):
        candidate = self._with_quantity(chat_id, candidate)
        self._save_schedule_draft(chat_id, [candidate])
        if not self.service.live_enabled:
            self._present(
                chat_id,
                "🔒 <b>Live mode is disabled</b>\n\n"
                "Set <code>ENABLE_LIVE_MINTS=true</code> in .env, restart the bot, "
                "and confirm live mode again. Nothing was broadcast.",
                self.schedule_candidate_keyboard(candidate),
                message_id,
            )
            return
        self._background(
            chat_id,
            "Live mint request",
            lambda target_id: self.draft_mint_job(chat_id, candidate, target_id),
            message_id,
        )

    def draft_mint_job(self, chat_id, candidate, target_id):
        result = self.service.mint_candidate(candidate)
        self._present(
            chat_id,
            self.render_result(result),
            self.schedule_candidate_keyboard(result.get("candidate") or candidate),
            target_id,
        )
        self.send_mint_receipt(chat_id, result)

    def show_live_draft_mint_confirmation(
        self, chat_id, candidate, token, message_id=None, context="research"
    ):
        candidate = self._with_quantity(chat_id, candidate)
        if not self.service.live_enabled:
            self._present(
                chat_id,
                "🔒 <b>Live mode is disabled</b>\n\n"
                "Set <code>ENABLE_LIVE_MINTS=true</code> in .env before using a live action.",
                self.schedule_candidate_keyboard(candidate),
                message_id,
            )
            return
        collection = embedded_link(
            candidate.get("name", candidate.get("slug", "Mint")),
            candidate.get("opensea_url") or candidate.get("url"),
        )
        if context == "schedule":
            confirm = f"schedule:mint:live:confirm:{token}"
            cancel = f"schedule:stage:{token}"
        else:
            confirm = f"research:confirm:{token}"
            cancel = f"research:view:{token}"
        funding = self._funding_block(candidate)
        route_note = self._route_guard_text(candidate)
        text = (
            "⚠️ <b>Confirm mint now</b>\n\n"
            f"<b>Collection:</b> {collection}\n"
            f"<b>Chain:</b> {esc(pretty_chain(candidate.get('chain')))}\n"
            f"<b>Stage:</b> {esc(candidate.get('stage_label', 'Unknown'))}\n"
            f"<b>Price:</b> {esc(candidate.get('price_display', 'Price unknown'))}\n"
            f"<b>Total mint value:</b> {esc(self._total_mint_value(candidate))}\n"
            f"<b>Quantity:</b> {esc(candidate.get('quantity', 1))} per wallet\n"
            f"<b>Wallets:</b> {esc(len(candidate.get('wallet_ids') or ['primary']))}\n\n"
            f"{funding}\n\n"
            f"{self._schedule_price_warning(candidate)}\n"
            "The next button may request calldata, sign, and broadcast a real transaction. "
            f"{route_note}"
        )
        self._present(
            chat_id,
            text,
            self.markup([
                [self.button("✅ Confirm mint now", confirm)],
                [self.button("↩️ Back", cancel)],
            ]),
            message_id,
        )

    def show_schedule_stages(self, chat_id, page=0, message_id=None):
        with self.input_lock:
            draft = self.schedule_drafts.get(int(chat_id))
            if not draft or float(draft.get("expires_at", 0)) < time.time():
                raise ValueError("that schedule preview expired; start a new schedule")
            candidates = [dict(candidate) for candidate in draft.get("candidates", [])]
        self._present(
            chat_id,
            self.render_schedule_stages(candidates, page=page),
            self.schedule_stage_keyboard(candidates, page=page),
            message_id,
        )

    def show_schedule_stage(self, chat_id, candidate, message_id=None):
        candidate = self._rich_candidate(self._with_quantity(chat_id, candidate))
        self._present(
            chat_id,
            self.render_schedule_candidate(candidate),
            self.schedule_candidate_keyboard(candidate),
            message_id,
        )

    def show_live_schedule_confirmation(self, chat_id, candidate, message_id=None):
        candidate = self._rich_candidate(self._with_quantity(chat_id, candidate))
        if not self.service.live_enabled:
            self._present(
                chat_id,
                "🔒 <b>Live schedules are disabled</b>\n\n"
                "Set <code>ENABLE_LIVE_MINTS=true</code> in .env, restart the bot, "
                "then open this schedule again.",
                self.schedule_candidate_keyboard(candidate),
                message_id,
            )
            return
        warning = self._schedule_price_warning(candidate)
        collection_link = embedded_link(
            candidate.get("name", candidate.get("slug", "Unknown")),
            candidate.get("opensea_url") or candidate.get("url"),
        )
        links = self.rich_links(candidate)
        funding = self._funding_block(candidate)
        route_note = self._route_guard_text(candidate)
        text = (
            "⚠️ <b>Confirm schedule</b>\n\n"
            f"<b>Collection:</b> {collection_link}\n"
            f"<b>Chain:</b> {esc(pretty_chain(candidate.get('chain')))}\n"
            f"<b>Stage:</b> {esc(candidate.get('stage_label', 'Unknown'))}\n"
            f"<b>Opens:</b> {esc(format_time(candidate.get('start_time')))}\n"
            f"<b>Price:</b> {esc(candidate.get('price_display', 'Price unknown'))}\n"
            f"<b>Total mint value:</b> {esc(self._total_mint_value(candidate))}\n"
            f"<b>Access:</b> {esc(candidate.get('access_label', 'Unknown'))}\n"
            f"<b>Quantity:</b> {esc(candidate.get('quantity', config.MINT_QUANTITY))} per wallet\n"
            f"<b>Wallets:</b> {esc(len(candidate.get('wallet_ids') or ['primary']))}\n\n"
            f"{funding}\n\n"
            f"{warning}\n\n"
            f"{route_note}\n\n"
            "The bot must stay online. At opening it refreshes the exact transaction, "
            "simulates it, checks every selected wallet and cap, then broadcasts only "
            "if every guard passes.\n\n"
            f"{description_block(candidate)}\n\n"
            f"{links}"
        )
        self._present(chat_id, text, self.confirm_schedule_keyboard(candidate), message_id)

    def arm_schedule(self, chat_id, candidate, message_id=None):
        candidate = self._with_quantity(chat_id, candidate)
        try:
            schedule = self.service.add_schedule(candidate)
        except Exception as exc:
            self._present(
                chat_id,
                "⚠️ <b>Schedule not armed</b>\n\n"
                f"{esc(redact_secrets(exc))}\n\n"
                "Nothing was signed or broadcast.",
                self.schedule_candidate_keyboard(candidate),
                message_id,
            )
            return
        self._present(
            chat_id,
            self.render_schedule(
                schedule,
                notice="✅ <b>Schedule armed.</b> It will still obey every cap, simulation, and route eligibility check.",
            ),
            self.schedule_detail_keyboard(schedule),
            message_id,
        )

    def show_schedules(self, chat_id, message_id=None, notice=None):
        schedules = self.service.schedules()
        text = self.render_schedules(schedules, notice=notice)
        self._present(chat_id, text, self.schedules_keyboard(schedules), message_id)

    def show_schedule_detail(self, chat_id, schedule_id, message_id=None):
        schedule = self.service.schedule_by_id(schedule_id)
        self._present(
            chat_id,
            self.render_schedule(schedule),
            self.schedule_detail_keyboard(schedule),
            message_id,
        )

    def start_mint(self, chat_id, index, message_id=None, candidate=None):
        candidate = self._with_quantity(chat_id, candidate or self._candidate_at(index))
        if not self.service.live_enabled:
            self._present(
                chat_id,
                "🔒 <b>Live mode is disabled</b>\n\n"
                "Set <code>ENABLE_LIVE_MINTS=true</code> in .env, restart the bot, "
                "and confirm live mode again.",
                self.daily_keyboard(),
                message_id,
            )
            return
        label = f"Live mint · candidate {index}"
        self._background(
            chat_id,
            label,
            lambda target_id: self.mint_job(chat_id, index, candidate, target_id),
            message_id,
        )

    def mint_job(self, chat_id, index, candidate, target_id):
        result = self.service.mint_candidate(candidate)
        candidate = result.get("candidate") or {}
        text = self.render_result(result)
        keyboard = self.candidate_detail_keyboard(index, candidate) if candidate else self.home_keyboard()
        self._present(chat_id, text, keyboard, target_id)
        self.send_mint_receipt(chat_id, result)

    def notify_mint_result(self, schedule, result):
        """Send a structured automatic/scheduled result plus its visual receipt."""
        if self.allowed_chat_id is None:
            return
        try:
            self.api.send_message(
                self.allowed_chat_id,
                self.render_result(result),
                reply_markup=self.home_keyboard(),
                parse_mode=HTML_MODE,
            )
            self.send_mint_receipt(self.allowed_chat_id, result)
        except Exception as exc:
            print(
                f"Telegram mint result failed: {type(exc).__name__}: {redact_secrets(exc)}",
                flush=True,
            )

    def send_mint_receipt(self, chat_id, result):
        """Send a branded post-mint card; transaction links remain clickable in Telegram."""
        wallet_results = result.get("wallet_results") or []
        if len(wallet_results) > 1:
            for wallet_result in wallet_results:
                if not wallet_result.get("tx_hash"):
                    continue
                item = dict(wallet_result)
                item["candidate"] = result.get("candidate") or {}
                self.send_mint_receipt(chat_id, item)
            return
        tx_hash = result.get("tx_hash")
        if not tx_hash:
            return
        candidate = self._rich_candidate(result.get("candidate") or {})
        if result.get("confirmed") is True:
            status = "confirmed"
        elif result.get("confirmed") is False:
            status = "reverted"
        else:
            status = "sent"
        native = (config.chain_config(candidate.get("chain")) or {}).get("native") or "native"
        path = None
        try:
            research = {}
            try:
                research = self.service.research_candidate(candidate)
            except Exception:
                pass
            mint_value_wei = int((result.get("summary") or {}).get("value_wei", 0))
            gas_wei = int(
                result.get("actual_gas_wei")
                if result.get("actual_gas_wei") is not None
                else result.get("worst_case_gas_wei", 0)
            )
            spent_wei = mint_value_wei + gas_wei
            quantity = int(candidate.get("quantity") or 1)
            floor_value_display = "Unavailable"
            pnl_display = "Unavailable"
            total_stats = research.get("stats_total") or {}
            floor = total_stats.get("floor_price") if isinstance(total_stats, dict) else None
            floor_symbol = str(
                (total_stats.get("floor_price_symbol") if isinstance(total_stats, dict) else "")
                or research.get("stats_currency")
                or ""
            ).upper()
            try:
                if floor is not None and (not floor_symbol or floor_symbol == native.upper()):
                    floor_value = Decimal(str(floor)) * quantity
                    spent_native = Decimal(spent_wei) / Decimal(10 ** 18)
                    pnl = floor_value - spent_native
                    floor_value_display = f"{format(floor_value, '.6f').rstrip('0').rstrip('.')} {native}"
                    sign = "+" if pnl >= 0 else ""
                    pnl_display = f"{sign}{format(pnl, '.6f').rstrip('0').rstrip('.')} {native}"
            except (TypeError, ValueError, ArithmeticError):
                pass
            receipt_candidate = dict(candidate)
            receipt_candidate.update({
                "receipt_status": status,
                "mint_value_display": format_native_wei(mint_value_wei, native),
                "gas_display": format_native_wei(gas_wei, native),
                "spent_display": format_native_wei(spent_wei, native),
                "floor_value_display": floor_value_display,
                "pnl_display": pnl_display,
                "minted_at": int(result.get("broadcast_at") or time.time()),
            })
            path = build_mint_card(receipt_candidate, research)
            tx_url = explorer_tx_url(candidate.get("chain"), tx_hash)
            status_line = {
                "confirmed": "✅ Confirmed on-chain",
                "reverted": "❌ Reverted on-chain",
                "sent": "⏳ Sent; confirmation pending",
            }[status]
            caption = (
                f"<b>{status_line}</b>\n"
                f"{embedded_link(candidate.get('name', candidate.get('slug', 'NFT mint')), candidate.get('opensea_url') or candidate.get('url'))}\n"
                f"Quantity: <b>{esc(candidate.get('quantity', 1))}</b> · "
                f"{embedded_link('View transaction', tx_url)}\n"
                "<i>P&L uses the current collection floor when available; it is an estimate, not a realized sale. "
                "OpenSea ownership indexing can lag chain confirmation.</i>"
            )
            rows = []
            if tx_url:
                rows.append([self.url_button("🔗 View transaction", tx_url)])
            collection_url = candidate.get("opensea_url") or candidate.get("url")
            if str(collection_url or "").startswith(("https://", "http://")):
                rows.append([self.url_button("🌊 OpenSea collection", collection_url)])
            rows.append([self.button("💼 Check wallet", "wallet"), self.button("🏠 Home", "home")])
            self.api.send_photo(
                chat_id, path, caption=caption,
                reply_markup=self.markup(rows), parse_mode=HTML_MODE,
            )
        except Exception as exc:
            # A receipt is presentation only. Never turn a successful mint
            # into an apparent failure because image generation/upload failed.
            print(
                f"Mint receipt card failed: {type(exc).__name__}: {redact_secrets(exc)}",
                flush=True,
            )
        finally:
            if path:
                try:
                    Path(path).unlink()
                except OSError:
                    pass

    def start_daily(self, chat_id, message_id=None):
        self.service.start_daily()
        self.show_daily(chat_id, message_id=message_id, notice="✅ <b>Live automatic runner started.</b>")

    def _background(self, chat_id, label, function, source_message_id=None):
        if not self.job_lock.acquire(blocking=False):
            self._present(
                chat_id,
                "⏳ <b>Another operation is already running.</b>\n\n"
                "Wait for it to finish or press Stop.",
                self.home_keyboard(),
                source_message_id,
            )
            return

        progress = (
            f"⏳ <b>{esc(label)}</b>\n\n"
            "The bot is working. This message will update when it finishes."
        )
        result = self._present(chat_id, progress, self.home_keyboard(), source_message_id)
        target_id = source_message_id or (result or {}).get("message_id")

        def run():
            try:
                function(target_id)
            except Exception as exc:
                self._send_error(chat_id, exc, message_id=target_id)
            finally:
                self.job_lock.release()

        threading.Thread(target=run, name="telegram-job", daemon=True).start()

    # ------------------------------------------------------------------
    # Screens and formatting
    # ------------------------------------------------------------------

    def show_home(self, chat_id, message_id=None):
        self._present(chat_id, self.home_text(), self.home_keyboard(), message_id)

    def show_settings(self, chat_id, message_id=None, notice=None):
        custom = bool(os.getenv("NFT_CARD_BACKGROUND", "").strip() or PERSISTENT_BACKGROUND.is_file())
        accent = os.getenv("NFT_CARD_ACCENT_COLOR", "#63E6BE").strip() or "#63E6BE"
        brand = os.getenv("NFT_CARD_BRAND_NAME", "NFT Mint Bot").strip() or "NFT Mint Bot"
        text = (
            (notice + "\n\n" if notice else "")
            + "<b>⚙️ More</b>\n\n"
            f"<b>Live transactions:</b> {'Enabled' if self.service.live_enabled else 'Locked'}\n"
            f"<b>Maximum mint price:</b> {esc(config.MAX_MINT_PRICE_NATIVE)} native coin\n"
            f"<b>Fallback background:</b> {'Custom' if custom else 'Built in'}\n"
            f"<b>Card accent:</b> <code>{esc(accent)}</code>\n"
            f"<b>Card brand:</b> {esc(brand)}\n\n"
            "<i>The price cap covers mint value only. Gas has its own separate "
            "hard cap.</i>"
        )
        self._present(chat_id, text, self.settings_keyboard(custom), message_id)

    def begin_background_input(self, chat_id, message_id=None):
        with self.input_lock:
            self.pending_inputs[int(chat_id)] = {
                "kind": "card_background",
                "expires_at": time.time() + 10 * 60,
            }
        self._present(
            chat_id,
            "<b>🖼 Card fallback background</b>\n\n"
            "Send a JPG, PNG, or WEBP as a photo/document, or paste a direct HTTPS image URL. "
            "It appears behind the NFT artwork and is used when artwork is unavailable. "
            "The bot crops it to 1200×675. Maximum size: 8 MB.",
            self.markup([[self.button("↩️ Cancel", "settings")]]),
            message_id,
        )

    def handle_background_input(self, chat_id, message, pending):
        self._clear_pending_input(chat_id)
        text_value = str(message.get("text") or "").strip()
        source = None
        temporary = None
        try:
            if text_value.startswith(("https://", "http://")):
                source = text_value
            else:
                photos = message.get("photo") or []
                document = message.get("document") or {}
                file_id = document.get("file_id") if isinstance(document, dict) else None
                if photos:
                    file_id = photos[-1].get("file_id")
                if not file_id:
                    raise ValueError("send an image file or a direct HTTPS image URL")
                suffix = Path(str(document.get("file_name") or ".jpg")).suffix or ".jpg"
                temporary = Path(tempfile.gettempdir()) / f"opensea-card-upload-{time.time_ns()}{suffix}"
                self.api.download_file(file_id, temporary)
                source = str(temporary)
            install_card_background(source)
            # A Telegram upload replaces any old path/URL from .env so the
            # newly installed persistent background takes effect immediately.
            self._set_env_value("NFT_CARD_BACKGROUND", "")
        finally:
            if temporary:
                try:
                    temporary.unlink()
                except OSError:
                    pass
        self.show_settings(chat_id, notice="✅ <b>Mint-card background updated.</b>")

    def begin_price_cap_input(self, chat_id, message_id=None):
        with self.input_lock:
            self.pending_inputs[int(chat_id)] = {
                "kind": "price_cap",
                "expires_at": time.time() + 10 * 60,
            }
        self._present(
            chat_id,
            "<b>💰 Maximum mint price</b>\n\n"
            "Send the maximum native coin the bot may spend as mint value in one transaction.\n\n"
            "Examples: <code>0</code> for free-only, <code>0.02</code> for up to 0.02 ETH/POL/AVAX. "
            "This does not include gas.",
            self.markup([[self.button("↩️ Cancel", "settings")]]),
            message_id,
        )

    def handle_price_cap_input(self, chat_id, value):
        cap = config.set_max_mint_price_native(value)
        self._set_env_value("MAX_MINT_PRICE_NATIVE", cap)
        self.show_settings(chat_id, notice="✅ <b>Maximum mint price updated.</b>")

    def begin_buy_cap_input(self, chat_id, message_id=None):
        with self.input_lock:
            self.pending_inputs[int(chat_id)] = {
                "kind": "buy_cap",
                "expires_at": time.time() + 10 * 60,
            }
        self._present(
            chat_id,
            "<b>🛍 Maximum OpenSea buy price</b>\n\n"
            "Send the maximum native coin allowed for one exact secondary-market purchase. "
            "This is separate from mint price and excludes gas.\n\n"
            "Example: <code>0.02</code>. Use <code>0</code> to lock buying.",
            self.markup([[self.button("↩️ Cancel", "settings")]]),
            message_id,
        )

    def handle_buy_cap_input(self, chat_id, value):
        cap = config.set_max_buy_price_native(value)
        self._set_env_value("MAX_BUY_PRICE_NATIVE", cap)
        self.show_settings(chat_id, notice="✅ <b>Maximum buy price updated.</b>")

    def begin_accent_input(self, chat_id, message_id=None):
        with self.input_lock:
            self.pending_inputs[int(chat_id)] = {
                "kind": "card_accent", "expires_at": time.time() + 10 * 60,
            }
        self._present(
            chat_id,
            "<b>🎨 Card accent color</b>\n\nSend a six-digit hex color such as "
            "<code>#63E6BE</code> or <code>#FF4D8D</code>.",
            self.markup([[self.button("↩️ Cancel", "settings")]]),
            message_id,
        )

    def handle_accent_input(self, chat_id, value):
        value = str(value or "").strip().upper()
        if not re.fullmatch(r"#[0-9A-F]{6}", value):
            raise ValueError("use a six-digit hex color such as #63E6BE")
        self._set_env_value("NFT_CARD_ACCENT_COLOR", value)
        self.show_settings(chat_id, notice="✅ <b>Card accent updated.</b>")

    def begin_brand_input(self, chat_id, message_id=None):
        with self.input_lock:
            self.pending_inputs[int(chat_id)] = {
                "kind": "card_brand", "expires_at": time.time() + 10 * 60,
            }
        self._present(
            chat_id,
            "<b>✏️ Card brand</b>\n\nSend the name to display in the top-right of every card "
            "and mint receipt (1–28 characters).",
            self.markup([[self.button("↩️ Cancel", "settings")]]),
            message_id,
        )

    def handle_brand_input(self, chat_id, value):
        value = " ".join(str(value or "").split())
        if not 1 <= len(value) <= 28 or any(char in value for char in "\r\n=#"):
            raise ValueError("brand name must be 1–28 plain-text characters")
        self._set_env_value("NFT_CARD_BRAND_NAME", value)
        self.show_settings(chat_id, notice="✅ <b>Card brand updated.</b>")

    def send_settings_preview(self, chat_id, message_id=None):
        candidate = {
            "name": "Your NFT Collection",
            "slug": "preview",
            "chain": "ethereum",
            "stage_label": "Public mint",
            "price_display": "0.01 ETH",
            "price_wei": 10 ** 16,
            "access_label": "Public",
            "quantity": 1,
            "start_time": int(time.time()),
        }
        path = None
        try:
            path = build_mint_card(candidate, {})
            self.api.send_photo(
                chat_id, path,
                caption=(
                    "<b>🎨 Mint-card preview</b>\n"
                    "Your fallback background, accent, and brand are applied. "
                    "Live cards use the actual NFT artwork when OpenSea provides it."
                ),
                reply_markup=self.settings_keyboard(
                    bool(os.getenv("NFT_CARD_BACKGROUND", "").strip() or PERSISTENT_BACKGROUND.is_file())
                ),
                parse_mode=HTML_MODE,
            )
            self._present(
                chat_id, "✅ <b>Preview sent.</b>",
                self.settings_keyboard(
                    bool(os.getenv("NFT_CARD_BACKGROUND", "").strip() or PERSISTENT_BACKGROUND.is_file())
                ), message_id,
            )
        finally:
            if path:
                try:
                    Path(path).unlink()
                except OSError:
                    pass

    @staticmethod
    def _set_env_value(name, value):
        env_path = ROOT / ".env"
        lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
        replacement = f"{name}={value}"
        updated = False
        for index, line in enumerate(lines):
            if line.strip().startswith(f"{name}="):
                lines[index] = replacement
                updated = True
                break
        if not updated:
            lines.append(replacement)
        env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        os.environ[name] = str(value)

    def show_status(self, chat_id, message_id=None):
        self._present(chat_id, self.status_card(), self.status_keyboard(), message_id)

    def start_wallet_status(
        self, chat_id, chain_slug=None, message_id=None, wallet_id="primary"
    ):
        if chain_slug:
            resolved = config.resolve_chain_slug(chain_slug)
            if resolved == "all":
                chain_slug = None
            elif resolved:
                chain_slug = resolved
            if chain_slug and chain_slug not in self.service.supported_chains():
                raise ValueError("choose one of the enabled networks from the wallet screen")
        label = f"Check {pretty_chain(chain_slug)} wallet" if chain_slug else "Check wallet"
        self._background(
            chat_id, label,
            lambda target_id: self.wallet_status_job(
                chat_id, chain_slug, wallet_id, target_id
            ),
            message_id,
        )

    def wallet_status_job(self, chat_id, chain_slug, wallet_id, target_id):
        snapshot = self.service.wallet_snapshot(
            chain_slug=chain_slug, wallet_id=wallet_id
        )
        self._present(
            chat_id, self.render_wallet(snapshot), self.wallet_keyboard(snapshot), target_id
        )

    def show_mint_history(self, chat_id, message_id=None):
        self._present(
            chat_id, self.render_mint_history(self.service.mint_history(limit=10)),
            self.markup([[self.button("💼 Wallet", "wallet"), self.button("🏠 Home", "home")]]),
            message_id,
        )

    def render_wallet(self, snapshot):
        address = str(snapshot.get("address") or "")
        account_url = f"https://opensea.io/{address}" if address else ""
        short_address = f"{address[:6]}…{address[-4:]}" if len(address) > 12 else address
        wallet_label = str((snapshot.get("wallet") or {}).get("label") or "Primary")
        chain_items = snapshot.get("chains") or []
        selected_chain = snapshot.get("selected_chain")
        total_nfts = 0
        all_exact = True
        issue_count = 0
        for item in chain_items:
            count = item.get("nft_count")
            if count is None:
                all_exact = False
            else:
                total_nfts += int(count)
                capped = bool(item.get("nft_count_capped"))
                all_exact = all_exact and not capped
            if item.get("errors"):
                issue_count += 1

        if selected_chain and len(chain_items) == 1:
            item = chain_items[0]
            count = item.get("nft_count")
            count_text = "Unavailable" if count is None else f"{count}{'+' if item.get('nft_count_capped') else ''}"
            lines = [
                f"<b>💼 {esc(pretty_chain(selected_chain))}</b>",
                f"<i>{esc(wallet_label)} wallet</i>",
                embedded_link(short_address, account_url),
                "",
                f"<b>Balance</b>\n{esc(format_wallet_balance(item.get('balance_wei'), item.get('native')))}",
                "",
                f"<b>NFTs</b>\n{esc(count_text)}",
            ]
            if item.get("errors"):
                lines.extend(["", "⚠️ Some wallet data could not load. Try refreshing."])
            records = snapshot.get("recent_mints") or []
            if records:
                lines.extend(["", self.render_mint_history(records, compact=True)])
            return "\n".join(lines)

        lines = [
            "<b>💼 My wallet</b>",
            f"<i>{esc(wallet_label)}</i>",
            f"{embedded_link(short_address, account_url)} · <b>{esc(total_nfts)}{'+' if not all_exact else ''} NFTs</b>",
            "",
        ]
        for item in chain_items:
            count = item.get("nft_count")
            nft_text = "— NFTs" if count is None else f"{count}{'+' if item.get('nft_count_capped') else ''} NFT{'s' if count != 1 else ''}"
            balance = format_wallet_balance(item.get("balance_wei"), item.get("native"))
            lines.append(
                f"<b>{esc(pretty_chain(item.get('chain')))}</b>  ·  {esc(balance)}  ·  {esc(nft_text)}"
            )
        if issue_count:
            subject = "network needs" if issue_count == 1 else "networks need"
            lines.extend(["", f"⚠️ {issue_count} {subject} a refresh."])
        records = snapshot.get("recent_mints") or []
        if records:
            lines.extend(["", self.render_mint_history(records, compact=True)])
        lines.extend(["", "<i>Tap a network below for details.</i>"])
        return "\n".join(lines)

    def render_mint_history(self, records, compact=False):
        lines = ["<b>Latest mint</b>"] if compact else ["<b>🧾 Mint history</b>", ""]
        if not records:
            lines.append("No mints yet.")
            return "\n".join(lines)
        for record in records[: 1 if compact else 10]:
            status = str(record.get("status") or "unknown").lower()
            icon = {"confirmed": "✅", "sent": "⏳", "reverted": "❌", "failed": "⚠️"}.get(status, "•")
            name = short_text(record.get("name") or record.get("slug") or "Mint", 30)
            tx_hash = record.get("tx_hash")
            tx = embedded_link("View transaction", explorer_tx_url(record.get("chain"), tx_hash)) if tx_hash else "No transaction"
            indexed = record.get("indexed_owned_count")
            if status == "confirmed" and indexed is not None:
                ownership = (
                    f" · {indexed} owned"
                    if indexed else " · NFT updating"
                )
            else:
                ownership = ""
            lines.append(
                f"{icon} <b>{esc(name)}</b>\n{esc(pretty_chain(record.get('chain', 'unknown')))} · {tx}{esc(ownership)}"
            )
        return "\n".join(lines)

    def wallet_keyboard(self, snapshot=None):
        chains = [item.get("chain") for item in (snapshot or {}).get("chains", [])]
        selected_chain = (snapshot or {}).get("selected_chain")
        wallet_id = str(((snapshot or {}).get("wallet") or {}).get("id") or "primary")
        available = (snapshot or {}).get("available_wallets") or []
        if selected_chain:
            refresh_callback = (
                f"wallet:chain:{selected_chain}"
                if wallet_id == "primary" else f"wallet:chain:{selected_chain}:{wallet_id}"
            )
            all_callback = "wallet" if wallet_id == "primary" else f"wallet:profile:{wallet_id}"
            return self.markup([
                [self.button("🔄 Refresh", refresh_callback), self.button("🧾 Mint history", "wallet:mints")],
                [self.button("‹ All networks", all_callback), self.button("🏠 Home", "home")],
            ])
        refresh_callback = "wallet" if wallet_id == "primary" else f"wallet:profile:{wallet_id}"
        rows = [[self.button("🔄 Refresh", refresh_callback), self.button("🧾 Mint history", "wallet:mints")]]
        for start in range(0, len(chains), 2):
            rows.append([
                self.button(
                    pretty_chain(chain),
                    f"wallet:chain:{chain}" if wallet_id == "primary"
                    else f"wallet:chain:{chain}:{wallet_id}",
                )
                for chain in chains[start:start + 2]
            ])
        if len(available) > 1:
            rows.append([self.button("👛 Switch wallet", "noop")])
            for start in range(0, len(available), 2):
                rows.append([
                    self.button(
                        ("✅ " if item.get("id") == wallet_id else "")
                        + short_text(item.get("label"), 18),
                        f"wallet:profile:{item.get('id')}",
                    )
                    for item in available[start:start + 2]
                ])
        rows.append([self.button("🏠 Home", "home")])
        return self.markup(rows)

    def show_daily(self, chat_id, message_id=None, notice=None):
        text = self.daily_card()
        if notice:
            text = notice + "\n\n" + text
        self._present(chat_id, text, self.daily_keyboard(), message_id)

    def show_candidates(
        self, chat_id, message_id=None, notice=None, page=0, scan_chain=None
    ):
        scan_chain = str(
            scan_chain if scan_chain is not None else self.last_scan_chain or ""
        ).strip().lower()
        if scan_chain == "all" and scan_chain is not None:
            text = self.render_chain_summary(
                self.service.last_candidates,
                self.service.last_errors,
            )
            keyboard = self.chain_summary_keyboard(self.service.last_candidates)
        else:
            visible_candidates = self._candidates_for_chain(
                self.service.last_candidates, scan_chain
            )
            chain_title = (
                f"🎨 <b>OpenSea mints · {esc(pretty_chain(scan_chain))}</b>"
                if scan_chain in self.service.supported_chains()
                else "🎨 <b>Today’s mint options</b>"
            )
            text = self.render_candidates(
                visible_candidates,
                self.service.last_errors,
                title=chain_title,
                page=page,
            )
            keyboard = self.candidates_keyboard(
                self.service.last_candidates,
                page=page,
                scan_chain=scan_chain,
            )
        if notice:
            text = esc(notice) + "\n\n" + text
        self._present(chat_id, text, keyboard, message_id)

    def show_candidate(self, chat_id, index, message_id=None, candidate=None):
        candidate = self._with_quantity(chat_id, candidate or self._candidate_at(index))
        page = self._candidate_group_page(index, candidate)
        self._present(
            chat_id,
            self.render_candidate(candidate, index),
            self.candidate_detail_keyboard(index, candidate, page),
            message_id,
        )

    def show_project(self, chat_id, token, message_id=None):
        group = self._project_from_ref(token)
        candidate = group["candidate"]
        options = group["options"]
        name = embedded_link(
            candidate.get("name", candidate.get("slug", "Project")),
            candidate.get("opensea_url") or candidate.get("url"),
        )
        lines = [
            "<b>🎨 Project</b>",
            f"<b>Name:</b> {name}",
            f"<b>Network:</b> {esc(pretty_chain(candidate.get('chain', 'unknown')))}",
            f"<b>Mint windows today:</b> {esc(len(options))}",
            "",
            "Choose what you want to do:",
            "",
        ]
        for position, (_, option) in enumerate(options, 1):
            timing = self._candidate_time_summary([(position, option)])
            lines.extend([
                f"<b>{position}. {esc(option.get('stage_label', 'Mint window'))}</b> · {esc(option.get('price_display', 'Price unknown'))}",
                f"   {esc(option.get('access_label', 'Eligibility unknown'))} · {esc(timing)}",
                "",
            ])
        route_links = self._mint_links_html(candidate)
        if route_links:
            lines.extend([
                "<b>Open</b>",
                route_links,
                "",
            ])
        lines.append(
            f"<b>Mint method:</b> {esc(self._mint_method(candidate))}\n"
            "Mint now and Schedule both refresh the selected window and run the same safety checks."
        )
        self._present(
            chat_id,
            "\n".join(lines).rstrip(),
            self.project_keyboard(group),
            message_id,
        )

    def show_quantity_picker(
        self, chat_id, candidate, context, index=None, message_id=None, notice=None
    ):
        candidate = self._rich_candidate(self._with_quantity(chat_id, candidate))
        name = embedded_link(
            candidate.get("name", candidate.get("slug", "Mint")),
            candidate.get("opensea_url") or candidate.get("url"),
        )
        current = int(candidate.get("quantity") or config.MINT_QUANTITY)
        limit = quantity_limit(candidate)
        limit_display = candidate.get("max_per_wallet")
        limit_text = f"\n<b>Stage wallet limit:</b> {esc(limit_display)}" if limit_display else ""
        links = self.rich_links(candidate)
        supply = self.supply_text(candidate)
        description = shorten_description(candidate.get("description"))
        text = (
            "<b>📦 Choose mint quantity</b>\n\n"
            f"<b>Collection:</b> {name}\n"
            f"<b>Stage:</b> {esc(candidate.get('stage_label', 'Unknown'))}\n"
            f"<b>Current quantity:</b> {esc(current)}\n"
            f"<b>Allowed by API:</b> 1–{limit}{limit_text}\n\n"
            f"Choose a preset or send a whole number from 1 to {limit}. OpenSea will still "
            "enforce the selected stage's wallet limit and supply.\n\n"
            f"{('<b>Description:</b>' + chr(10) + description + chr(10) + chr(10)) if description else ''}"
            f"{supply + chr(10) if supply else ''}"
            f"<b>Links:</b> {links}"
        )
        if notice:
            text = f"⚠️ <b>Quantity not changed</b>\n\n{esc(notice)}\n\n{text}"
        self._present(
            chat_id,
            text,
            self.quantity_keyboard(candidate, context, index),
            message_id,
        )

    def _public_wallets(self):
        getter = getattr(self.service, "public_wallets", None)
        if callable(getter):
            wallets = getter()
            if wallets:
                return wallets
        address = str(getattr(self.service, "wallet_address", "") or "")
        return [{"id": "primary", "label": "Primary", "address": address}]

    def _selected_wallet_ids(self, chat_id, candidate):
        token = candidate_token(candidate)
        saved = self.wallet_choices.get((int(chat_id), token))
        if saved:
            return list(saved)
        available = self._public_wallets()
        valid = {item["id"] for item in available}
        selected = [
            wallet_id for wallet_id in (candidate.get("wallet_ids") or [])
            if wallet_id in valid
        ]
        return selected or [available[0]["id"]]

    def show_wallet_picker(
        self, chat_id, candidate, context, index=None, message_id=None
    ):
        candidate = self._rich_candidate(self._with_quantity(chat_id, candidate))
        wallets = self._public_wallets()
        selected = set(self._selected_wallet_ids(chat_id, candidate))
        quantity = int(candidate.get("quantity") or 1)
        lines = [
            "<b>👛 Choose mint wallets</b>",
            "",
            f"<b>Selected:</b> {len(selected)} of {len(wallets)}",
            f"<b>Per wallet:</b> {quantity} NFT(s)",
            f"<b>Total requested:</b> {quantity * len(selected)} NFT(s)",
            "",
        ]
        for wallet in wallets:
            icon = "✅" if wallet["id"] in selected else "⬜"
            address = str(wallet.get("address") or "")
            short = f"{address[:6]}…{address[-4:]}" if len(address) >= 12 else "configured"
            lines.append(f"{icon} <b>{esc(wallet.get('label'))}</b> · <code>{esc(short)}</code>")
        lines.extend([
            "",
            "Each selected wallet signs its own transaction in parallel. Price and gas apply to every wallet.",
        ])
        self._present(
            chat_id,
            "\n".join(lines),
            self.wallet_picker_keyboard(candidate, context, index, selected),
            message_id,
        )

    def toggle_wallet(
        self, chat_id, candidate, wallet_id, context, index=None, message_id=None
    ):
        wallets = self._public_wallets()
        valid = {item["id"] for item in wallets}
        selected = set(self._selected_wallet_ids(chat_id, candidate))
        if wallet_id == "all":
            selected = valid
        elif wallet_id not in valid:
            raise ValueError("that wallet is no longer configured")
        elif wallet_id in selected:
            if len(selected) == 1:
                raise ValueError("at least one wallet must remain selected")
            selected.remove(wallet_id)
        else:
            selected.add(wallet_id)
        ordered = [item["id"] for item in wallets if item["id"] in selected]
        self.wallet_choices[(int(chat_id), candidate_token(candidate))] = ordered
        self.show_wallet_picker(chat_id, candidate, context, index, message_id)

    def begin_custom_quantity_input(self, chat_id, candidate, context, index=None, message_id=None):
        token = candidate_token(candidate)
        limit = quantity_limit(candidate)
        with self.input_lock:
            self.pending_inputs[int(chat_id)] = {
                "kind": "quantity",
                "context": context,
                "index": index,
                "token": token,
                "expires_at": time.time() + 10 * 60,
            }
        self._present(
            chat_id,
            "<b>📦 Custom quantity</b>\n\n"
            f"Send one whole number from <b>1</b> to <b>{limit}</b>.\n"
            "Send /cancel to leave the quantity unchanged.",
            self.quantity_cancel_keyboard(context, candidate, index),
            message_id,
        )

    def handle_quantity_input(self, chat_id, text, pending):
        raw = str(text or "").strip()
        context = pending.get("context")
        index = pending.get("index")
        token = pending.get("token")
        if context == "candidate":
            if index is None:
                raise ValueError("quantity candidate index is missing")
            candidate = self._candidate_from_ref(f"candidate:{int(index)}:{token}")[1]
        elif context == "schedule":
            candidate = self._schedule_candidate_from_ref(chat_id, token)
        else:
            raise ValueError("quantity input expired")
        if not raw.isdigit():
            self.show_quantity_picker(
                chat_id,
                candidate,
                context,
                index,
                notice="Send a whole number only.",
            )
            self._restore_quantity_input(chat_id, pending)
            return
        try:
            quantity = validate_quantity(candidate, int(raw))
        except ValueError as exc:
            self.show_quantity_picker(
                chat_id,
                candidate,
                context,
                index,
                notice=str(exc),
            )
            self._restore_quantity_input(chat_id, pending)
            return
        self.set_quantity(chat_id, candidate, context, index, quantity)

    def set_quantity(self, chat_id, candidate, context, index, quantity, message_id=None):
        self._clear_pending_input(chat_id)
        try:
            quantity = validate_quantity(candidate, quantity)
        except ValueError as exc:
            self.show_quantity_picker(
                chat_id,
                candidate,
                context,
                index,
                message_id=message_id,
                notice=str(exc),
            )
            return
        candidate = dict(candidate)
        candidate["quantity"] = quantity
        self.quantity_choices[(int(chat_id), candidate_token(candidate))] = quantity
        if context == "candidate":
            self.show_candidate(chat_id, index, message_id, candidate)
        elif context == "schedule":
            self.show_schedule_stage(chat_id, candidate, message_id)
        else:
            raise ValueError("invalid quantity context")

    def quantity_cancel_keyboard(self, context, candidate, index=None):
        return self.markup([[
            self.button("↩️ Back", self._quantity_back_callback(candidate, context, index))
        ]])

    def quantity_keyboard(self, candidate, context, index=None):
        token = candidate_token(candidate)
        limit = min(MAX_QUANTITY, quantity_limit(candidate))
        values = [value for value in QUANTITY_OPTIONS if value <= limit]
        rows = []
        row = []
        for value in values:
            if context == "candidate":
                callback = f"quantity:set:candidate:{index}:{token}:{value}"
            else:
                callback = f"quantity:set:schedule:{token}:{value}"
            row.append(self.button(str(value), callback))
            if len(row) == 4:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        if context == "candidate":
            custom = f"quantity:custom:candidate:{index}:{token}"
        else:
            custom = f"quantity:custom:schedule:{token}"
        rows.append([self.button(f"✍️ Custom 1–{limit}", custom)])
        rows.append([self.button("↩️ Back", self._quantity_back_callback(candidate, context, index))])
        return self.markup(rows)

    def wallet_picker_keyboard(self, candidate, context, index, selected):
        token = candidate_token(candidate)
        wallets = self._public_wallets()
        rows = []
        for wallet in wallets:
            icon = "✅" if wallet["id"] in selected else "⬜"
            if context == "candidate":
                callback = f"wallets:toggle:candidate:{index}:{token}:{wallet['id']}"
            else:
                callback = f"wallets:toggle:schedule:{token}:{wallet['id']}"
            rows.append([
                self.button(f"{icon} {short_text(wallet['label'], 24)}", callback)
            ])
        if len(wallets) > 1:
            callback = (
                f"wallets:toggle:candidate:{index}:{token}:all"
                if context == "candidate"
                else f"wallets:toggle:schedule:{token}:all"
            )
            rows.append([self.button("✅ Select all wallets", callback)])
        rows.append([
            self.button("↩️ Done", self._quantity_back_callback(candidate, context, index))
        ])
        return self.markup(rows)

    @staticmethod
    def _quantity_back_callback(candidate, context, index=None):
        token = candidate_token(candidate)
        if context == "candidate":
            return f"candidate:{index}:{token}"
        return f"schedule:stage:{token}"

    def show_live_daily_confirmation(self, chat_id, message_id=None):
        if not self.service.live_enabled:
            self._present(
                chat_id,
                "🔒 <b>Live mode is disabled</b>\n\n"
                "Set <code>ENABLE_LIVE_MINTS=true</code> in .env and restart the bot "
                "before enabling live actions.",
                self.daily_keyboard(),
                message_id,
            )
            return
        text = (
            "⚠️ <b>Confirm live daily minting</b>\n\n"
            "This will scan configured chains and attempt each saved stage when "
            "its window opens. Paid stages obey the price cap; OpenSea verifies "
            "allowlist eligibility.\n\n"
            "<b>Safety limits:</b>\n"
            f"• {esc(self.service.max_daily_mints)} candidate attempts per configured day\n"
            f"• {esc(self.service.max_daily_gas_wei / 10**18)} native gas cap per chain\n"
            "• one attempt per drop per configured day\n\n"
            "Use a funded, disposable wallet."
        )
        self._present(chat_id, text, self.confirm_daily_keyboard(), message_id)

    def show_live_mint_confirmation(self, chat_id, index, message_id=None, candidate=None):
        candidate = self._rich_candidate(
            self._with_quantity(chat_id, candidate or self._candidate_at(index))
        )
        if not self.service.live_enabled:
            self._present(
                chat_id,
                "🔒 <b>Live mode is disabled</b>\n\n"
                "Set <code>ENABLE_LIVE_MINTS=true</code> in .env and restart the bot.",
                self.candidate_detail_keyboard(index, candidate),
                message_id,
            )
            return
        collection_link = embedded_link(
            candidate.get("name", candidate.get("slug", "Unknown")),
            candidate.get("opensea_url") or candidate.get("url"),
        )
        links = self.rich_links(candidate)
        funding = self._funding_block(candidate)
        route_note = self._route_guard_text(candidate)
        text = (
            "⚠️ <b>Confirm mint now</b>\n\n"
            f"<b>Collection:</b> {collection_link}\n"
            f"<b>Chain:</b> {esc(pretty_chain(candidate['chain']))}\n"
            f"<b>Price:</b> {esc(candidate.get('price_display', 'Price unknown'))}\n"
            f"<b>Total mint value:</b> {esc(self._total_mint_value(candidate))}\n"
            f"<b>Access:</b> {esc(candidate.get('access_label', candidate.get('stage_label', 'Unknown')))}\n"
            f"<b>Quantity:</b> {esc(candidate.get('quantity', config.MINT_QUANTITY))} per wallet\n"
            f"<b>Wallets:</b> {esc(len(candidate.get('wallet_ids') or ['primary']))}\n"
            f"<b>Price cap:</b> {esc(config.MAX_MINT_PRICE_NATIVE)} native coin\n\n"
            f"{funding}\n\n"
            f"{route_note} The next step can broadcast real transactions and spend gas.\n\n"
            f"{description_block(candidate)}\n\n"
            f"{links}"
        )
        self._present(chat_id, text, self.confirm_mint_keyboard(index, candidate), message_id)

    def home_text(self):
        snapshot = self.service.status_snapshot()
        lines = [
            "<b>\U0001f6f0 Mint Bot</b>",
            "",
        ]
        options = int(snapshot["candidate_count"] or 0)
        if options:
            lines.append(
                f"<b>{esc(snapshot.get('project_count', 0))}</b> projects \u00b7 "
                f"<b>{esc(options)}</b> mint windows from the last scan"
            )
        else:
            lines.append("No scan results yet.")
        armed = int(snapshot.get("schedule_count", 0) or 0)
        if armed:
            lines.append(f"<b>{esc(armed)}</b> armed schedule(s)")
        if snapshot["mode"] != "stopped":
            lines.append("Automatic scanning is <b>on</b>")
        if not snapshot["live_enabled"]:
            lines.append("<i>Live transactions are locked</i>")
        return "\n".join(lines)

    def status_card(self):
        snapshot = self.service.status_snapshot()
        mode = {
            "stopped": "Off",
            "live": "Live and monitoring",
        }.get(snapshot["mode"], snapshot["mode"])
        live = "Enabled" if snapshot["live_enabled"] else "Locked"
        chains = ", ".join(pretty_chain(chain) for chain in snapshot["chains"])
        invalid = snapshot.get("invalid_chains") or []
        chain_note = f"\n<b>Ignored chains:</b> {esc(', '.join(invalid))}" if invalid else ""
        return (
            "<b>📊 Bot status</b>\n\n"
            f"<b>Automatic scanning:</b> {esc(mode)}\n"
            f"<b>Live transactions:</b> {live}\n"
            f"<b>Enabled networks:</b> {esc(chains)}\n"
            f"<b>Signing wallets:</b> {esc(snapshot.get('wallet_count', 1))}\n"
            f"<b>Projects found today:</b> {esc(snapshot.get('project_count', 0))}\n"
            f"<b>Mint options found:</b> {esc(snapshot['candidate_count'])}\n"
            f"<b>Free + public:</b> {esc(snapshot['free_candidate_count'])}\n"
            f"<b>Scan issues:</b> {esc(snapshot['last_error_count'])}\n"
            f"<b>Last scan:</b> {esc(format_saved_time(snapshot.get('last_scan_at')))}\n"
            f"<b>Mint attempts today:</b> {esc(snapshot['attempt_count'])}/{esc(snapshot['max_daily_mints'])}\n"
            f"<b>Gas cap:</b> {esc(snapshot['daily_gas_cap'])} native per chain\n"
            f"<b>Armed schedules:</b> {esc(snapshot.get('schedule_count', 0))}\n"
            f"<b>Schedule watcher:</b> {esc('Running' if snapshot.get('schedule_worker_alive') else 'Waiting')}{chain_note}\n\n"
            "<i>This screen never signs or broadcasts a transaction.</i>"
        )

    def daily_card(self):
        snapshot = self.service.status_snapshot()
        mode = esc(snapshot["mode"].replace("-", " ").title())
        live = "🟢 enabled" if snapshot["live_enabled"] else "🔒 disabled"
        runtime = "online" if snapshot["worker_alive"] else "idle"
        if snapshot["stop_requested"]:
            runtime = "stopping"
        return (
            "<b>⚙️ Daily runner</b>\n\n"
            f"<b>Service:</b> {esc(runtime)}\n"
            f"<b>Current mode:</b> {mode}\n"
            f"<b>Live switch:</b> {live}\n"
            f"<b>Attempt limit:</b> {esc(snapshot['max_daily_mints'])} per configured day\n"
            f"<b>Gas cap:</b> {esc(snapshot['daily_gas_cap'])} native per chain\n"
            f"<b>Chains:</b> {esc(len(snapshot['chains']))} configured\n"
            f"<b>Free + public options:</b> {esc(snapshot['free_candidate_count'])}\n\n"
            f"<b>One-time schedules:</b> {esc(snapshot.get('schedule_count', 0))} armed\n\n"
            "Live mode requires the environment switch and a confirmation button. One-time schedules "
            "run independently of this broad daily scanner."
        )

    def scan_picker_text(self, coverage=None, errors=None, age=None):
        """Describe what the drop calendar actually holds right now."""
        live = {chain: count for chain, count in (coverage or {}).items() if count}
        lines = ["<b>🔎 Pick a network</b>", ""]
        if coverage is None:
            lines.append("Choose a network to scan for OpenSea mints.")
            lines.append("")
            lines.append(
                "<i>Per-network drop counts are unavailable right now.</i>"
            )
        elif live:
            total = sum(live.values())
            word = "network" if len(live) == 1 else "networks"
            lines.append(
                f"<b>{esc(total)}</b> drops across <b>{esc(len(live))}</b> {word} "
                "in the next 24 hours."
            )
            lines.append("")
            lines.append(
                "<i>Counts come from OpenSea's own drop calendar. A network that is "
                "not listed has nothing scheduled.</i>"
            )
        else:
            lines.append("<b>OpenSea has no drops scheduled on any network.</b>")
            lines.append("")
            lines.append(
                "<i>This is OpenSea's calendar, not a failed scan. Tap Refresh "
                "in a few minutes.</i>"
            )
        for error in (errors or [])[:2]:
            lines.append(f"⚠️ <i>{esc(error)}</i>")
        return "\n".join(lines).rstrip()

    def show_chain_picker(self, chat_id, message_id=None, show_empty=False, force=False):
        """Load real per-network drop counts, then render the picker.

        Counts are read from the cached drop calendar, so this costs one API
        read per cache window rather than one per network.
        """
        reader = getattr(self.service, "chain_coverage", None)
        if not callable(reader):
            coverage, errors, age = None, [], None
        else:
            try:
                coverage, errors, age = reader(force_refresh=force)
            except Exception as exc:
                coverage = None
                errors = [f"calendar unavailable ({type(exc).__name__})"]
                age = None

        keyboard = self.chain_keyboard(coverage, show_empty=show_empty)
        caption = self.scan_picker_text(coverage, errors, age)

        # The overflow view is a long button grid; an image of the busy
        # networks would only repeat what the caption already says.
        if coverage is not None and not show_empty:
            path = None
            try:
                path = build_picker_card(
                    coverage, len(self.service.supported_chains())
                )
                self._present_photo(chat_id, path, caption, keyboard, message_id)
                return
            except Exception:
                # Never let picker artwork stand between the operator and the
                # ability to scan.
                pass
            finally:
                self._discard(path)

        self._present(chat_id, caption, keyboard, message_id)

    @staticmethod
    def _candidates_for_chain(candidates, chain):
        chain = str(chain or "").strip().lower()
        if not chain or chain == "all":
            return list(candidates or [])
        return [
            candidate for candidate in (candidates or [])
            if str(candidate.get("chain") or "").strip().lower() == chain
        ]

    def _chain_candidate_groups(self, candidates):
        grouped = {}
        for candidate in candidates or []:
            chain = str(candidate.get("chain") or "unknown").strip().lower() or "unknown"
            grouped.setdefault(chain, []).append(candidate)
        supported = [
            str(chain).strip().lower()
            for chain in self.service.supported_chains()
            if str(chain).strip()
        ]
        ordered = []
        for chain in supported:
            if chain not in ordered:
                ordered.append(chain)
        for chain in grouped:
            if chain not in ordered:
                ordered.append(chain)
        return [(chain, grouped.get(chain, [])) for chain in ordered]

    def render_chain_summary(self, candidates, errors, title="🎨 <b>All OpenSea mints today</b>"):
        candidates = list(candidates or [])
        groups = self._chain_candidate_groups(candidates)
        project_count = len(project_groups(candidates))
        lines = [
            title,
            "<i>Grouped by network · choose a network to see its projects</i>",
            f"<i>{esc(project_count)} projects · {esc(len(candidates))} mint options</i>",
            "",
        ]
        if not candidates:
            lines.extend([
                "<b>No OpenSea mints are live or opening today.</b>",
                "Try a specific network again later. Marketplace listings are not mint routes.",
            ])
        else:
            for chain, chain_candidates in groups:
                if not chain_candidates:
                    summary = "No live or upcoming mint windows"
                    detail = "0 projects · 0 mint options"
                else:
                    chain_projects = len(project_groups(chain_candidates))
                    free_count = sum(1 for item in chain_candidates if item.get("is_free"))
                    public_count = sum(1 for item in chain_candidates if item.get("is_public"))
                    detail = (
                        f"{chain_projects} projects · {len(chain_candidates)} mint options · "
                        f"{free_count} free · {public_count} public"
                    )
                    summary = self._candidate_time_summary(
                        list(enumerate(chain_candidates, 1))
                    )
                lines.extend([
                    f"⛓ <b>{esc(pretty_chain(chain))}</b>",
                    f"   {esc(detail)}",
                    f"   {esc(summary)}",
                    "",
                ])
        if errors:
            lines.extend([
                f"⚠️ <i>{esc(len(errors))} network scan issue(s). Open a network below to retry it.</i>",
            ])
        return "\n".join(lines).rstrip()

    def render_candidates(self, candidates, errors, title="🎨 <b>Today’s mint options</b>", page=0):
        total_options = len(candidates)
        groups = project_groups(candidates)
        total = len(groups)
        total_pages = max(1, (total + CANDIDATE_PAGE_SIZE - 1) // CANDIDATE_PAGE_SIZE)
        page = min(max(0, int(page)), total_pages - 1)
        first = page * CANDIDATE_PAGE_SIZE
        visible = groups[first:first + CANDIDATE_PAGE_SIZE]
        if not candidates:
            text = (
                f"{title}\n\n"
                "<b>No OpenSea mints are live or opening today.</b>\n"
                "Check again later. Collections that are only listed for trading are not included."
            )
        else:
            free_count = sum(1 for candidate in candidates if candidate.get("is_free"))
            public_count = sum(1 for candidate in candidates if candidate.get("is_public"))
            lines = [
                title,
                f"<i>{esc(total)} collections · {esc(total_options)} mint options · "
                f"{esc(free_count)} free · {esc(public_count)} public</i>",
                f"<i>Page {page + 1} of {total_pages} · tap a collection to review its mint windows</i>",
                "",
            ]
            for offset, group in enumerate(visible):
                candidate = group["candidate"]
                options = group["options"]
                option_word = "option" if len(options) == 1 else "options"
                prices = []
                for _, option in options:
                    price = str(option.get("price_display") or "Price unknown")
                    if price not in prices:
                        prices.append(price)
                lines.extend([
                    f"{first + offset + 1}. <b>{embedded_link(short_text(candidate.get('name', candidate.get('slug', 'Unknown')), 42), candidate.get('opensea_url') or candidate.get('url'))}</b>",
                    f"   {esc(len(options))} mint {option_word} · {esc(' / '.join(prices[:2]))}",
                    f"   {esc(self._candidate_time_summary(options))}",
                    "",
                ])
            text = "\n".join(lines).rstrip()
        if errors:
            note = (
                "Some OpenSea data could not load. Try Refresh."
                if not candidates
                else "Some OpenSea stage details used the compact catalogue fallback; refresh for the full stage list."
            )
            text += f"\n\n⚠️ <i>{esc(note)}</i>"
        return text

    @staticmethod
    def _candidate_time_summary(options):
        now = int(time.time())
        active = []
        upcoming = []
        for _, candidate in options:
            start = int(candidate.get("start_time") or 0)
            end = candidate.get("end_time")
            try:
                end = int(end) if end is not None else None
            except (TypeError, ValueError):
                end = None
            if start <= now and (end is None or end >= now):
                active.append(candidate)
            elif start > now:
                upcoming.append(candidate)
        if active and upcoming:
            opens = min(item.get("start_time") or 0 for item in upcoming)
            upcoming_word = "open" if len(upcoming) != 1 else "opens"
            return (
                f"🟢 {len(active)} live · "
                f"🕒 {len(upcoming)} {upcoming_word} {format_time(opens)}"
            )
        if active:
            return f"🟢 {len(active)} live now"
        if upcoming:
            opens = min(item.get("start_time") or 0 for item in upcoming)
            return f"🕒 {len(upcoming)} opens {format_time(opens)}"
        return "Stage timing unavailable"

    def render_candidate(self, candidate, index):
        candidate = self._rich_candidate(candidate)
        end = candidate.get("end_time")
        end_text = format_time(end) if end else "not provided"
        collection_url = candidate.get("opensea_url") or candidate.get("url")
        route = self._mint_method(candidate)
        links = self.rich_links(candidate)
        supply = self.supply_text(candidate)
        collection_link = embedded_link(
            candidate.get("name", candidate.get("slug", "Unknown")), collection_url
        )
        return (
            f"<b>🎯 Mint option {esc(index)}</b>\n\n"
            f"<b>Collection:</b> {collection_link}\n"
            f"<b>Chain:</b> {esc(pretty_chain(candidate.get('chain', 'unknown')))}\n"
            f"<b>Mint window:</b> {esc(candidate.get('stage_label', 'Unknown'))}\n"
            f"<b>Price:</b> {esc(candidate.get('price_display', 'Price unknown'))}\n"
            f"<b>Total mint value:</b> {esc(self._total_mint_value(candidate))}\n"
            f"<b>Access:</b> {esc(candidate.get('access_label', candidate.get('stage_label', 'Unknown')))}\n"
            f"<b>Opens:</b> {esc(format_time(candidate.get('start_time')))}\n"
            f"<b>Ends:</b> {esc(end_text)}\n"
            f"<b>Quantity:</b> {esc(candidate.get('quantity', config.MINT_QUANTITY))} per wallet\n"
            f"<b>Wallets:</b> {esc(len(candidate.get('wallet_ids') or ['primary']))}\n"
            f"<b>Method:</b> {esc(route)}\n\n"
            f"{description_block(candidate)}\n\n"
            f"{supply + chr(10) if supply else ''}"
            f"<b>Links:</b> {links}\n\n"
            f"{embedded_link('Open collection on OpenSea', collection_url)}"
        )

    def _schedule_price_warning(self, candidate):
        price_wei = candidate.get("price_wei")
        try:
            quantity = validate_quantity(
                candidate, candidate.get("quantity") or config.MINT_QUANTITY
            )
            over_cap = (
                price_wei is not None
                and int(price_wei) * quantity > config.MAX_MINT_VALUE_WEI
            )
        except (TypeError, ValueError):
            over_cap = False
        if over_cap:
            return (
                f"⚠️ <b>Price guard:</b> this stage is above the current cap of "
                f"{esc(config.MAX_MINT_PRICE_NATIVE)} native coin, so it will be refused "
                "until you raise the cap deliberately."
            )
        if price_wei is None:
            return (
                "⚪ <b>Price guard:</b> the mint price is unknown. Live execution will "
                "refuse it until a fresh OpenSea preview provides an exact price.\n"
            )
        if candidate.get("is_public") is False:
            return (
                "🟡 <b>Access guard:</b> OpenSea will verify this wallet against the "
                "stage allowlist/holder rule.\n"
            )
        return "🟢 <b>Safety:</b> wallet balance, price, gas, and chain checks run before broadcast.\n"

    @staticmethod
    def _route_guard_text(candidate):
        if candidate.get("route") == "generic_contract":
            return (
                "The verified external contract route is simulated again for each wallet "
                "immediately before signing. Custom proofs or changed contract rules make it stop safely."
            )
        if candidate.get("is_public") is True and candidate.get("contract_address"):
            return (
                "Public SeaDrop stages use the direct on-chain fast path when the contract "
                "values match the preview; other OpenSea stages use OpenSea's signed calldata route."
            )
        return (
            "OpenSea supplies the final mint transaction and performs the stage eligibility check."
        )

    @staticmethod
    def _total_mint_value(candidate):
        price_wei = candidate.get("price_wei")
        if price_wei is None:
            return "Unknown — live mint blocked"
        try:
            quantity = validate_quantity(
                candidate, candidate.get("quantity") or config.MINT_QUANTITY
            )
            wallet_count = len(candidate.get("wallet_ids") or ["primary"])
            total = int(price_wei) * quantity * wallet_count
        except (TypeError, ValueError):
            return "Invalid — live mint blocked"
        if total == 0:
            return "Free (gas still applies)"
        chain = config.chain_config(candidate.get("chain")) or {}
        native = chain.get("native") or "native coin"
        value = format(Decimal(total) / Decimal(10 ** 18), "f").rstrip("0").rstrip(".")
        suffix = f" across {wallet_count} wallets" if wallet_count > 1 else ""
        return f"{value} {native}{suffix}"

    def _funding_block(self, candidate):
        """Render a fresh read-only wallet check for a confirmation screen."""
        try:
            snapshot = self.service.funding_snapshot(candidate)
        except Exception:
            return "<b>💳 Funds needed</b>\n⚠️ Could not check right now. Try again."
        native = snapshot.get("native") or "native"
        estimated_ok = snapshot.get("estimated_shortfall_wei", 0) == 0
        if estimated_ok:
            verdict = "✅ Ready"
        else:
            verdict = (
                "❌ Add about "
                + format_native_wei(snapshot.get("estimated_shortfall_wei"), native)
            )
        return (
            "<b>💳 Funds needed</b>\n"
            f"Wallets: {esc(snapshot.get('wallet_count', 1))}\n"
            f"Mint: {esc(format_native_wei(snapshot.get('mint_value_wei'), native))}\n"
            f"Gas estimate: {esc(format_native_wei(snapshot.get('estimated_gas_wei'), native))}\n"
            f"<b>Total: {esc(format_native_wei(snapshot.get('estimated_total_wei'), native))}</b>\n"
            f"Wallet: {esc(format_native_wei(snapshot.get('balance_wei'), native))}\n"
            f"{esc(verdict)}\n"
            "<i>Rechecked before minting.</i>"
        )

    def _rich_candidate(self, candidate):
        """Best-effort metadata enrichment for a detail/preview screen."""
        candidate = dict(candidate or {})
        if not candidate.get("description") and not candidate.get("metadata_loaded"):
            try:
                candidate = self.service.enrich_candidate(candidate)
            except Exception:
                # A detail card must remain usable if the metadata endpoint is
                # rate-limited; the stage data and OpenSea link still render.
                candidate["metadata_loaded"] = True
        return candidate

    def _fresh_candidate(self, candidate):
        """Refresh one stage immediately before an arm or broadcast action."""
        return self.service.refresh_candidate(candidate, require_same_terms=True)

    @staticmethod
    def _research_candidate(research):
        if not isinstance(research, dict):
            return None
        candidate = research.get("candidate")
        if isinstance(candidate, dict) and candidate.get("slug"):
            return dict(candidate)
        return None

    def card_caption(self, candidate, research=None):
        research = research or {}
        name = str(candidate.get("name") or research.get("name") or "NFT mint")
        collection = embedded_link(
            short_text(name, 48),
            candidate.get("opensea_url") or research.get("opensea_url") or candidate.get("url"),
        )
        return (
            f"<b>🛰 {collection}</b>\n"
            f"{esc(pretty_chain(candidate.get('chain', 'unknown')))} · "
            f"{esc(candidate.get('stage_label', 'Mint stage'))} · "
            f"{esc(candidate.get('price_display', 'Price unknown'))}\n"
            f"Quantity: <b>{esc(candidate.get('quantity', 1))}</b> · "
            f"Opens: {esc(format_time(candidate.get('start_time')))}"
        )

    def rich_links(self, candidate):
        """Render safe embedded links with readable Telegram labels."""
        links = []
        for label, key in (
            ("Website", "project_url"),
            ("X", "twitter_url"),
            ("Discord", "discord_url"),
            ("Telegram", "telegram_url"),
            ("Wiki", "wiki_url"),
        ):
            href = safe_http_url(candidate.get(key))
            if href:
                links.append(f'<a href="{href}">{esc(label)}</a>')
        address = str(candidate.get("contract_address") or "").strip()
        template = CHAIN_EXPLORER_ADDRESS_URLS.get(str(candidate.get("chain") or "").lower())
        if address and template and re.fullmatch(r"0x[a-fA-F0-9]{40}", address):
            links.append(
                f'<a href="{esc(template.format(address=address))}">🔗 Contract</a>'
            )
        return " · ".join(links) or "<i>No project links supplied by OpenSea</i>"

    @staticmethod
    def supply_text(candidate):
        total = candidate.get("total_supply")
        maximum = candidate.get("max_supply")
        if total is None and maximum is None:
            return ""
        if total is not None and maximum is not None:
            return f"<b>Supply:</b> {esc(total)} minted / {esc(maximum)} max"
        return f"<b>Supply:</b> {esc(total if total is not None else maximum)}"

    def _with_quantity(self, chat_id, candidate):
        candidate = dict(candidate or {})
        token = candidate_token(candidate)
        selected = self.quantity_choices.get((int(chat_id), token)) if chat_id is not None else None
        quantity = int(selected or candidate.get("quantity") or config.MINT_QUANTITY)
        try:
            candidate["quantity"] = validate_quantity(candidate, quantity)
        except ValueError:
            # A saved default can be higher than a stage's newly reported
            # wallet limit. Start at one so the next action remains usable.
            candidate["quantity"] = 1
        if chat_id is not None:
            candidate["wallet_ids"] = self._selected_wallet_ids(chat_id, candidate)
        return candidate

    def _ordered_schedule_candidates(self, candidates):
        chain_order = {
            str(chain).strip().lower(): position
            for position, chain in enumerate(self.service.supported_chains())
        }
        return sorted(
            [dict(candidate) for candidate in candidates or []],
            key=lambda candidate: (
                chain_order.get(str(candidate.get("chain") or "").lower(), 999),
                str(candidate.get("chain") or "").lower(),
                str(candidate.get("name") or candidate.get("slug") or "").lower(),
                int(candidate.get("start_time") or 0),
                str(candidate.get("stage_label") or "").lower(),
            ),
        )

    def render_schedule_stages(self, candidates, page=0):
        candidates = self._ordered_schedule_candidates(candidates)
        total_pages = max(1, (len(candidates) + SCHEDULE_STAGE_PAGE_SIZE - 1) // SCHEDULE_STAGE_PAGE_SIZE)
        page = min(max(0, int(page)), total_pages - 1)
        first = page * SCHEDULE_STAGE_PAGE_SIZE
        visible = candidates[first:first + SCHEDULE_STAGE_PAGE_SIZE]
        lines = [
            "<b>📌 Choose one mint to schedule</b>",
            f"<i>{esc(len(candidates))} active/upcoming mint window(s) · page {page + 1}/{total_pages}</i>",
            "",
        ]
        last_chain = None
        for index, candidate in enumerate(visible, first + 1):
            chain = str(candidate.get("chain") or "unknown").lower()
            if chain != last_chain:
                lines.extend([
                    f"⛓ <b>{esc(pretty_chain(chain))}</b>",
                ])
                last_chain = chain
            lines.extend([
                f"<b>{index}. {esc(short_text(candidate.get('name', candidate.get('slug')), 44))}</b>",
                f"   {candidate_badge(candidate)} · {esc(candidate.get('stage_label', 'Mint window'))}",
                f"   💵 {esc(candidate.get('price_display', 'Price unknown'))} · 🕒 {esc(format_time(candidate.get('start_time')))}",
                "",
            ])
        lines.append("Tap a mint to review it, then confirm the schedule.")
        return "\n".join(lines).rstrip()

    def render_schedule_candidate(self, candidate):
        candidate = self._rich_candidate(candidate)
        end = candidate.get("end_time")
        collection_url = candidate.get("opensea_url") or candidate.get("url")
        warning = self._schedule_price_warning(candidate)
        access = candidate.get("access_label", candidate.get("stage_label", "Unknown"))
        links = self.rich_links(candidate)
        supply = self.supply_text(candidate)
        quantity = int(candidate.get("quantity") or config.MINT_QUANTITY)
        collection_link = embedded_link(
            candidate.get("name", candidate.get("slug", "Unknown")), collection_url
        )
        return (
            "<b>📌 Schedule preview</b>\n\n"
            f"<b>Collection:</b> {collection_link}\n"
            f"<b>Chain:</b> {esc(pretty_chain(candidate.get('chain', 'unknown')))}\n"
            f"<b>Stage:</b> {esc(candidate.get('stage_label', 'Unknown'))}\n"
            f"<b>Price:</b> {esc(candidate.get('price_display', 'Price unknown'))}\n"
            f"<b>Total mint value:</b> {esc(self._total_mint_value(candidate))}\n"
            f"<b>Access:</b> {esc(access)}\n"
            f"<b>Opens:</b> {esc(format_time(candidate.get('start_time')))}\n"
            f"<b>Ends:</b> {esc(format_time(end) if end else 'not provided')}\n"
            f"<b>Quantity:</b> {esc(quantity)} per wallet\n"
            f"<b>Wallets:</b> {esc(len(candidate.get('wallet_ids') or ['primary']))}\n\n"
            f"{warning}\n"
            f"{self._route_guard_text(candidate)}\n\n"
            "Check the details, then confirm the schedule.\n\n"
            f"{description_block(candidate)}\n\n"
            f"{supply + chr(10) if supply else ''}"
            f"<b>Links:</b> {links}\n\n"
            f"{embedded_link('Open collection on OpenSea', collection_url)}"
        )

    def render_schedule(self, schedule, notice=None):
        candidate = self._rich_candidate(schedule.get("candidate") or {})
        status = str(schedule.get("status") or "unknown")
        status_label = {
            "armed": "🟢 Armed",
            "running": "⏳ Running",
            "completed": "✅ Completed",
            "failed": "🔴 Failed",
            "cancelled": "⏹ Cancelled",
        }.get(status, f"⚪ {status.title()}")
        result = schedule.get("result") or {}
        error = schedule.get("error")
        warning = self._schedule_price_warning(candidate)
        text = (
            (notice + "\n\n" if notice else "")
            + "<b>📌 Mint schedule</b>\n\n"
            f"<b>Status:</b> {status_label}\n"
            f"<b>Collection:</b> {embedded_link(candidate.get('name', candidate.get('slug', 'Unknown')), candidate.get('opensea_url') or candidate.get('url'))}\n"
            f"<b>Chain:</b> {esc(pretty_chain(candidate.get('chain', 'unknown')))}\n"
            f"<b>Stage:</b> {esc(candidate.get('stage_label', 'Unknown'))}\n"
            f"<b>Price:</b> {esc(candidate.get('price_display', 'Price unknown'))}\n"
            f"<b>Total mint value:</b> {esc(self._total_mint_value(candidate))}\n"
            f"<b>Access:</b> {esc(candidate.get('access_label', 'Unknown'))}\n"
            f"<b>Fire time:</b> {esc(format_time(schedule.get('run_at')))}\n"
            f"<b>Quantity:</b> {esc(candidate.get('quantity', config.MINT_QUANTITY))} per wallet\n"
            f"<b>Wallets:</b> {esc(len(candidate.get('wallet_ids') or ['primary']))}\n\n"
            f"{warning}\n"
            f"{self._route_guard_text(candidate)}\n"
            "The bot process must remain online for an armed schedule. Fresh transaction data "
            "and simulation are required at fire time; nothing is broadcast when this screen is shown."
        )
        links = self.rich_links(candidate)
        supply = self.supply_text(candidate)
        text += (
            f"\n\n{description_block(candidate)}"
        )
        text += f"\n\n{supply}" if supply else ""
        text += f"\n\n<b>Links:</b> {links}"
        if error:
            text += f"\n\n<b>Failure:</b> {esc(error)}"
        if result.get("tx_hash"):
            tx_url = explorer_tx_url(candidate.get("chain"), result.get("tx_hash"))
            text += (
                f"\n\n<b>Transaction:</b> <code>{esc(result['tx_hash'])}</code> · "
                f"{embedded_link('View transaction', tx_url)}"
            )
        return text

    def render_schedules(self, schedules, notice=None):
        active = [item for item in schedules if item.get("status") in {"armed", "running"}]
        lines = []
        if notice:
            lines.extend([notice, ""])
        lines.extend([
            "<b>📌 Mint schedules</b>",
            f"<i>{esc(len(active))} armed · {esc(len(schedules))} total saved locally</i>",
            "",
        ])
        if not schedules:
            lines.extend([
                "No one-time schedules yet.",
                "Tap <b>New schedule</b> and paste any OpenSea collection, drop, item, or asset URL.",
            ])
        else:
            for item in schedules[:10]:
                candidate = item.get("candidate") or {}
                status = str(item.get("status") or "unknown")
                icon = {
                    "armed": "🟢", "running": "⏳", "completed": "✅",
                    "failed": "🔴", "cancelled": "⏹",
                }.get(status, "⚪")
                lines.extend([
                    f"{icon} <b>{esc(short_text(candidate.get('name', candidate.get('slug', 'Mint')), 38))}</b>",
                    f"   {esc(status.title())} · {esc(pretty_chain(candidate.get('chain', 'unknown')))} · {esc(format_time(item.get('run_at')))}",
                    f"   {esc(candidate.get('price_display', 'Price unknown'))} · {esc(item.get('id', 'unknown'))}",
                    "",
                ])
            if len(schedules) > 10:
                lines.append(f"<i>Showing 10 of {esc(len(schedules))}; completed history remains local.</i>")
        return "\n".join(lines).rstrip()

    def render_result(self, result):
        candidate = self._rich_candidate(result.get("candidate") or {})
        collection_link = embedded_link(
            candidate.get("name", candidate.get("slug", "Candidate")),
            candidate.get("opensea_url") or candidate.get("url"),
        )
        links = self.rich_links(candidate)
        supply = self.supply_text(candidate)
        supply_block = f"{supply}\n\n" if supply else ""
        details = (
            f"\n\n{description_block(candidate)}"
            f"\n\n{supply_block}"
            f"<b>Links:</b> {links}"
        )
        speed = (
            f"\n<b>Broadcast delay:</b> {esc(result['launch_delay_ms'])} ms"
            if result.get("launch_delay_ms") is not None else ""
        )
        execution_route = str(result.get("execution_route") or "").strip()
        if execution_route == "direct_seadrop":
            route_line = "\n<b>Execution:</b> ⚡ Direct SeaDrop fast path"
        elif execution_route == "mixed":
            route_line = "\n<b>Execution:</b> Mixed direct/OpenSea routes"
        elif execution_route:
            route_line = "\n<b>Execution:</b> OpenSea verified calldata"
        else:
            route_line = ""
        rpc_count = int(result.get("broadcast_rpc_count") or 0)
        rpc_line = f"\n<b>RPC broadcast:</b> {rpc_count} endpoint(s)" if rpc_count > 1 else ""
        tx_hash = result.get("tx_hash")
        tx_link = embedded_link(
            "View transaction", explorer_tx_url(candidate.get("chain"), tx_hash)
        )
        transaction = (
            f"<code>{esc(tx_hash)}</code> · {tx_link}" if tx_hash else "No transaction hash"
        )
        wallet_results = result.get("wallet_results") or []
        if len(wallet_results) > 1:
            lines = [
                "<b>👛 Multi-wallet mint result</b>",
                "",
                f"<b>Collection:</b> {collection_link}",
                f"<b>Wallets:</b> {len(wallet_results)}",
                "",
            ]
            for item in wallet_results:
                wallet = item.get("wallet") or {}
                if item.get("confirmed") is True:
                    status = "✅ Confirmed"
                elif item.get("confirmed") is False:
                    status = "❌ Reverted"
                elif item.get("tx_hash"):
                    status = "⏳ Sent"
                else:
                    status = "⚠️ Failed"
                link = embedded_link(
                    "Transaction",
                    explorer_tx_url(candidate.get("chain"), item.get("tx_hash")),
                ) if item.get("tx_hash") else esc(short_text(item.get("error", "No transaction"), 80))
                lines.append(f"<b>{esc(wallet.get('label', 'Wallet'))}</b> · {status} · {link}")
            lines.extend([
                "",
                "Each wallet used its own nonce, balance, gas check, and transaction.",
                (f"Execution: {execution_route.replace('_', ' ')}" if execution_route else ""),
            ])
            return "\n".join(lines)
        if result.get("tx_hash") and result.get("confirmed") is True:
            return (
                "<b>✅ Mint confirmed</b>\n\n"
                f"<b>Collection:</b> {collection_link}\n"
                f"<b>Transaction:</b> {transaction}{speed}{route_line}{rpc_line}\n"
                "<b>Ownership:</b> confirmed on-chain; OpenSea may still be indexing it."
                f"{details}"
            )
        if result.get("tx_hash") and result.get("confirmed") is False:
            return (
                "<b>❌ Mint transaction reverted</b>\n\n"
                f"<b>Collection:</b> {collection_link}\n"
                f"<b>Transaction:</b> {transaction}{speed}{route_line}{rpc_line}"
                f"{details}"
            )
        if result.get("tx_hash"):
            return (
                "<b>⏳ Transaction sent; confirmation pending</b>\n\n"
                f"<b>Collection:</b> {collection_link}\n"
                f"<b>Transaction:</b> {transaction}{speed}{route_line}{rpc_line}\n\n"
                "Do not retry this mint. Check the transaction in the chain explorer."
                f"{details}"
            )
        return self.error_card("The mint finished without a transaction hash. Check the saved attempt before retrying.")

    def render_research(self, research):
        """Render only actionable facts from OpenSea's hosted-drop record."""
        research = dict(research or {})
        if research.get("known_to_opensea") is False:
            return (
                "\u26a0\ufe0f <b>Not found on OpenSea</b>\n\n"
                f"OpenSea has no collection, drop, or item for "
                f"<code>{esc(research.get('slug') or 'that reference')}</code>.\n\n"
                "Check the spelling, or copy the URL straight from the "
                "collection page in your browser."
            )
        candidates = [
            dict(item) for item in (research.get("mint_candidates") or [])
            if isinstance(item, dict)
        ]
        candidate = research.get("candidate") or (candidates[0] if len(candidates) == 1 else {})
        name = research.get("name") or candidate.get("name") or research.get("slug") or "OpenSea drop"
        collection_url = research.get("opensea_url") or candidate.get("opensea_url")
        chain = research.get("chain") or candidate.get("chain") or "unknown"
        now = int(time.time())
        live_count = sum(
            1 for item in candidates
            if int(item.get("start_time") or 0) <= now
            and (item.get("end_time") is None or int(item.get("end_time") or 0) >= now)
        )
        if research.get("is_sold_out") is True:
            status = "⛔ Sold out"
        elif live_count:
            status = f"🟢 Live · {live_count} stage{'s' if live_count != 1 else ''}"
        elif candidates:
            status = f"🕒 Scheduled · {len(candidates)} stage{'s' if len(candidates) != 1 else ''}"
        else:
            status = "⚪ No active or upcoming stage"

        lines = [
            "<b>ℹ️ OpenSea mint info</b>",
            f"<b>Collection:</b> {embedded_link(name, collection_url)}",
            f"<b>Network:</b> {esc(pretty_chain(chain))}",
            f"<b>Status:</b> {esc(status)}",
        ]
        supply = self._research_supply(research)
        if supply != "Unknown":
            lines.append(f"<b>Supply:</b> {esc(supply)}")
        contract = str(research.get("contract_address") or candidate.get("contract_address") or "").strip()
        explorer = CHAIN_EXPLORER_ADDRESS_URLS.get(str(chain).lower())
        if contract:
            contract_label = f"{contract[:8]}…{contract[-6:]}" if len(contract) > 18 else contract
            contract_url = explorer.format(address=contract) if explorer else ""
            lines.append(f"<b>Contract:</b> {embedded_link(contract_label, contract_url)}")

        lines.extend(["", "<b>Mint stages</b>"])
        if not candidates:
            lines.append("<i>OpenSea currently exposes no live or upcoming mint stage.</i>")
        for position, item in enumerate(candidates, 1):
            start = int(item.get("start_time") or 0)
            end = item.get("end_time")
            is_live = start <= now and (end is None or int(end or 0) >= now)
            timing = "Live now" if is_live else f"Opens {format_time(start)}"
            if is_live and end:
                timing += f" · ends {format_time(end)}"
            sold_out = " · SOLD OUT" if item.get("is_sold_out") is True else ""
            lines.extend([
                f"{position}. <b>{esc(item.get('stage_label') or 'Mint stage')}</b>",
                f"   {esc(item.get('price_display') or 'Price unknown')} · "
                f"{esc(item.get('access_label') or 'Access unknown')}",
                f"   {esc(timing + sold_out)}",
            ])

        description = shorten_description(research.get("description") or candidate.get("description"), 500)
        if description:
            lines.extend(["", f"<b>Description:</b>\n{esc(description)}"])
        source = str(research.get("source") or "OpenSea Drops API")
        lines.extend(["", f"<i>Source: {esc(source)} · refreshed for this request</i>"])
        return "\n".join(lines)

    @staticmethod
    def _mint_method(candidate):
        """Return a human label for the transaction path, without raw diagnostics."""
        candidate = candidate if isinstance(candidate, dict) else {}
        route = str(candidate.get("route") or "opensea_drop").strip().lower()
        if route == "generic_contract":
            return "Verified contract · simulated before signing"
        if route == "opensea_drop" and candidate.get("route_label"):
            return str(candidate.get("route_label"))
        if candidate.get("is_public") is True and candidate.get("contract_address"):
            return "Direct public SeaDrop · checked on-chain"
        return "OpenSea drop transaction · refreshed at confirmation"

    def _mint_links_html(self, candidate=None, research=None):
        """Return one non-duplicated set of useful project/mint links."""
        candidate = candidate if isinstance(candidate, dict) else {}
        research = research if isinstance(research, dict) else {}
        collection_url = candidate.get("opensea_url") or candidate.get("url") or research.get("opensea_url")
        explicit_mint_url = (
            candidate.get("mint_url")
            or candidate.get("drop_url")
            or research.get("mint_url")
            or research.get("drop_url")
        )
        links = []
        if safe_http_url(explicit_mint_url) and str(explicit_mint_url).strip() != str(collection_url or "").strip():
            links.append(embedded_link("Mint page", explicit_mint_url))
        # A collection URL is only an actionable drop page when a candidate
        # exists. For research with no verified stage it remains the title
        # link above, not a falsely labelled mint route.
        if candidate and safe_http_url(collection_url):
            links.append(embedded_link("OpenSea drop", collection_url))
        project_url = candidate.get("project_url") or research.get("project_url")
        if safe_http_url(project_url) and str(project_url).strip() not in {
            str(collection_url or "").strip(), str(explicit_mint_url or "").strip()
        }:
            links.append(embedded_link("Project site", project_url))
        return " · ".join(links)

    @staticmethod
    def _append_research_row(lines, label, value, hide_zero=False):
        text = str(value or "").strip()
        if not text or text.lower() == "unknown":
            return
        numeric = text.split()[0].replace(",", "")
        if hide_zero:
            try:
                if float(numeric) == 0:
                    return
            except ValueError:
                pass
        lines.append(f"<b>{esc(label)}:</b> {esc(text)}")

    @staticmethod
    def _unique_wallets(values, exclude=None):
        excluded = {str(value).strip().lower() for value in (exclude or set()) if value}
        seen = set(excluded)
        result = []
        for value in values if isinstance(values, list) else []:
            wallet = str(value or "").strip()
            identity = wallet.lower()
            if not wallet or identity in seen:
                continue
            seen.add(identity)
            result.append(wallet)
        return result

    @staticmethod
    def _research_metric(research, bucket, key, currency=False):
        data = research.get(bucket) or {}
        if not isinstance(data, dict):
            return "Unknown"
        value = data.get(key)
        if value is None:
            return "Unknown"
        formatted = TelegramBot._format_research_number(value, count=not currency)
        symbol = ""
        if currency:
            total = research.get("stats_total") or {}
            symbol = (
                data.get("symbol")
                or data.get("volume_symbol")
                or research.get("stats_currency")
                or (total.get("floor_price_symbol") if isinstance(total, dict) else "")
                or ""
            )
        return f"{formatted} {str(symbol).upper()}".strip()

    @staticmethod
    def _format_research_number(value, count=False):
        try:
            number = float(value)
        except (TypeError, ValueError):
            return str(value)
        if count:
            return f"{int(round(number)):,}"
        if number >= 1_000_000:
            return f"{number / 1_000_000:.2f}".rstrip("0").rstrip(".") + "M"
        if number >= 1_000:
            return f"{number / 1_000:.2f}".rstrip("0").rstrip(".") + "K"
        if number >= 1:
            return f"{number:,.2f}".rstrip("0").rstrip(".")
        return f"{number:.6f}".rstrip("0").rstrip(".")

    @staticmethod
    def _research_supply(research):
        total = research.get("total_supply")
        if total is None or total == "":
            total = research.get("unique_item_count")
        maximum = research.get("max_supply")
        if total in (None, "") and maximum in (None, ""):
            return "Unknown"
        if total not in (None, "") and maximum not in (None, ""):
            minted = TelegramBot._format_research_number(total, count=True)
            cap = TelegramBot._format_research_number(maximum, count=True)
            return f"{minted} / {cap} minted"
        value = total if total not in (None, "") else maximum
        return TelegramBot._format_research_number(value, count=True)

    @staticmethod
    def _research_owners(research):
        owners = TelegramBot._research_metric(research, "stats_total", "num_owners")
        try:
            owner_count = float(str(owners).replace(",", ""))
            supply = float(research.get("total_supply") or 0)
        except (TypeError, ValueError):
            return owners
        if owner_count == 0 and supply > 0:
            return "Indexing…"
        return owners

    @staticmethod
    def _research_floor(research):
        latest = research.get("latest_floor") or {}
        if not isinstance(latest, dict):
            latest = {}
        amount = latest.get("token_unit") or latest.get("floor_price") or latest.get("price")
        if amount is None:
            total = research.get("stats_total") or {}
            if isinstance(total, dict) and total.get("floor_price") is not None:
                amount = total.get("floor_price")
                unit = total.get("floor_price_symbol") or ""
            else:
                amount = latest.get("usd_price")
                unit = "USD"
        else:
            unit = latest.get("symbol") or ""
        if amount is None:
            return "Unknown"
        if not unit:
            unit = research.get("stats_currency") or ""
        return f"{TelegramBot._format_research_number(amount)} {str(unit).upper()}".strip()

    def research_links(self, research):
        links = []
        for label, key in (
            ("Website", "project_url"),
            ("X", "twitter_url"),
            ("Instagram", "instagram_url"),
            ("Discord", "discord_url"),
            ("Telegram", "telegram_url"),
            ("Wiki", "wiki_url"),
        ):
            href = safe_http_url(research.get(key))
            if href:
                links.append(f'<a href="{href}">{esc(label)}</a>')
        username = str(research.get("instagram_username") or "").strip()
        if username and not any("Instagram" in item for item in links):
            links.append(
                f'<a href="https://instagram.com/{esc(username)}">Instagram</a>'
            )
        # The collection title at the top already links to OpenSea. Keep this
        # section for external project/social destinations only.
        return " · ".join(links)

    def owner_link(self, research, address, role="Attributed wallet"):
        """Render a complete wallet address with explicit research links."""
        address = str(address or "").strip()
        profiles = research.get("owner_profiles") or {}
        profile = profiles.get(address) if isinstance(profiles, dict) else None
        if profile is None and isinstance(profiles, dict):
            profile = next(
                (value for key, value in profiles.items() if str(key).lower() == address.lower()),
                None,
            )
        username = str(
            (profile.get("display_name") or profile.get("username") or "")
            if isinstance(profile, dict) else ""
        ).strip()
        valid_evm = re.fullmatch(r"0x[a-fA-F0-9]{40}", address) is not None
        if not valid_evm:
            identity = f"{esc(username)}\n" if username else ""
            return f"<b>{esc(role)}:</b>\n{identity}<code>{esc(address)}</code>"

        chain = str(
            (research.get("candidate") or {}).get("chain")
            or research.get("chain")
            or ""
        ).lower()
        links = [
            embedded_link("OpenSea profile", f"https://opensea.io/{address}"),
            embedded_link("Arkham", f"https://intel.arkm.com/explorer/address/{address}"),
        ]
        template = CHAIN_EXPLORER_ADDRESS_URLS.get(chain)
        if template:
            links.append(embedded_link("Block explorer", template.format(address=address)))
        identity = f"{esc(username)}\n" if username else ""
        return (
            f"<b>{esc(role)}:</b>\n"
            f"{identity}<code>{esc(address)}</code>\n"
            f"{' · '.join(links)}"
        )

    def error_card(self, message):
        return f"⚠️ <b>Notice</b>\n\n{esc(message)}"

    # Ordered most specific first. Each entry maps a substring of the raw
    # error to (title, explanation, retry_action).
    ERROR_PATTERNS = (
        (
            ("cannot reach the blockchain", "rpc chain mismatch",
             "no configured rpc"),
            "\u26d3\ufe0f Network unreachable",
            "The RPC endpoint for that chain did not answer.",
            "Check ALCHEMY_API_KEY and the network overrides in .env.",
        ),
        (
            ("network error", "connecterror", "timeout", "timed out",
             "connection", "getaddrinfo"),
            "\U0001f4e1 Network hiccup",
            "The request to OpenSea did not get through. This is almost always "
            "temporary.",
            "Try again in a moment.",
        ),
        (
            ("429", "rate limit", "too many requests"),
            "\u23f3 Rate limited",
            "OpenSea is throttling requests right now.",
            "Wait a minute, then try again.",
        ),
        (
            ("401", "403", "api key", "authentication"),
            "\U0001f511 API key rejected",
            "OpenSea refused the API key.",
            "Check OPENSEA_API_KEY in .env, then restart the bot.",
        ),
        (
            ("no active/upcoming mint stage", "exposes no", "no supported evm",
             "no hosted stage", "not expose this collection"),
            "\u26aa No mint available",
            "OpenSea knows this collection, but it has no live or upcoming "
            "mint stage, and no safe on-chain mint route could be verified.",
            "It may be sold out, finished, or sold only on the secondary "
            "market.",
        ),
        (
            ("invalid", "must point to opensea.io", "paste a supported",
             "cannot be empty", "invalid token id", "invalid contract"),
            "\U0001f517 Link not recognised",
            "That does not look like a supported OpenSea collection, drop, "
            "item, or asset link.",
            "Copy the URL straight from the collection page in your browser.",
        ),
        (
            ("hard cap", "price cap", "above the configured"),
            "\U0001f6d1 Blocked by your price cap",
            "This mint costs more than the cap you configured, so nothing was "
            "signed or sent.",
            "Raise the cap under More \u2192 Maximum mint price if you want "
            "to allow it.",
        ),
        (
            ("balance is below", "no native coin", "insufficient"),
            "\U0001f4b8 Not enough gas",
            "The wallet cannot cover the mint value plus its gas envelope.",
            "Top the wallet up, then try again.",
        ),
    )

    def _explain_error(self, exc, safe_message):
        """Return ``(title, body)`` in plain language for a raw exception."""
        haystack = f"{type(exc).__name__} {safe_message}".lower()
        for needles, title, explanation, action in self.ERROR_PATTERNS:
            if any(needle in haystack for needle in needles):
                return title, f"{explanation}\n\n{action}"
        # Anything unclassified still shows the real message, because hiding it
        # would leave the operator with nothing to act on.
        return (
            "\u26a0\ufe0f Something went wrong",
            f"{safe_message}\n\nIf this keeps happening, check the bot log.",
        )

    def _send_error(self, chat_id, exc, message_id=None):
        safe_message = redact_secrets(exc)
        if "stale" in safe_message.lower() or "expired candidate" in safe_message.lower():
            self._present(
                chat_id,
                "\U0001f504 <b>Candidate list refreshed</b>\n\n"
                "That button belongs to an older scan. Review the current "
                "candidates before taking action.",
                self.candidates_keyboard(),
                message_id,
            )
            return
        title, body = self._explain_error(exc, safe_message)
        self._present(
            chat_id,
            f"<b>{title}</b>\n\n{esc(body)}",
            self.home_keyboard(),
            message_id,
        )

    def _send(self, chat_id, text, keyboard=None):
        return self.api.send_message(chat_id, text, keyboard, HTML_MODE)

    @staticmethod
    def _discard(path):
        """Delete a rendered temporary image, if one was produced."""
        if not path:
            return
        try:
            Path(path).unlink()
        except OSError:
            pass

    def _present_photo(self, chat_id, path, caption, keyboard, message_id=None):
        """Show an image screen, replacing whatever screen it supersedes.

        A text message cannot be edited into a photo, so the previous screen is
        removed and a new one sent. Tracking the last image per chat keeps a
        repeated refresh from filling the chat with near-identical pictures.
        """
        key = int(chat_id)
        for stale in (message_id, self.photo_screens.get(key)):
            if stale:
                self.api.delete_message(chat_id, stale)
        result = self.api.send_photo(
            chat_id,
            path,
            caption=caption,
            reply_markup=keyboard,
            parse_mode=HTML_MODE,
        )
        sent_id = (result or {}).get("message_id")
        if sent_id:
            self.photo_screens[key] = sent_id
        else:
            self.photo_screens.pop(key, None)
        return result

    def _present(self, chat_id, text, keyboard=None, message_id=None):
        keyboard = keyboard if keyboard is not None else self.home_keyboard()
        if message_id is not None:
            try:
                self.api.edit_message_text(
                    chat_id,
                    message_id,
                    text,
                    keyboard,
                    HTML_MODE,
                )
                return {"message_id": message_id}
            except RuntimeError as exc:
                if "not modified" in str(exc).lower():
                    return {"message_id": message_id}
        return self._send(chat_id, text, keyboard)

    def _answer_callback(self, callback_id, text=None, show_alert=False):
        if not callback_id:
            return
        try:
            self.api.answer_callback(callback_id, text, show_alert)
        except Exception:
            pass

    def _candidate_at(self, index):
        return self.service.candidate_at(index)

    def _candidate_from_ref(self, data):
        parts = str(data).split(":")
        if len(parts) != 3 or parts[0] != "candidate" or not parts[1].isdigit():
            raise ValueError("invalid or expired candidate button")
        index = int(parts[1])
        candidate = self._candidate_at(index)
        if candidate_token(candidate) != parts[2]:
            raise ValueError("that candidate button is stale; run a fresh scan")
        return index, candidate

    def _project_from_ref(self, token):
        groups = project_groups(self.service.last_candidates)
        for group in groups:
            if project_token(group["candidate"]) == str(token):
                return group
        raise ValueError("that project button is stale; run a fresh scan")

    def _project_option_from_ref(self, project_ref, candidate_ref):
        group = self._project_from_ref(project_ref)
        for index, candidate in group["options"]:
            if candidate_token(candidate) == str(candidate_ref):
                return group, index, dict(candidate)
        raise ValueError("that mint window is stale; run a fresh scan")

    def _candidate_group_page(self, index, candidate):
        chain = str(candidate.get("chain") or "").strip().lower()
        if chain in self.service.supported_chains():
            source = self.service.last_candidates
            visible = self._candidates_for_chain(source, chain)
            absolute_indexes = [
                position
                for position, item in enumerate(source or [], 1)
                if str(item.get("chain") or "").strip().lower() == chain
            ]
            for page, group in enumerate(
                project_groups(visible, absolute_indexes)
            ):
                if any(option_index == index for option_index, _ in group["options"]):
                    return page // CANDIDATE_PAGE_SIZE
        return max(0, (int(index) - 1) // CANDIDATE_PAGE_SIZE)

    def _project_group_page(self, group):
        candidate = group.get("candidate") or {}
        chain = str(candidate.get("chain") or "").strip().lower()
        source = self.service.last_candidates
        if chain in self.service.supported_chains():
            visible = self._candidates_for_chain(source, chain)
            absolute_indexes = [
                position
                for position, item in enumerate(source or [], 1)
                if str(item.get("chain") or "").strip().lower() == chain
            ]
            groups = project_groups(visible, absolute_indexes)
        else:
            groups = project_groups(source)
        target = project_token(candidate)
        for position, item in enumerate(groups):
            if project_token(item["candidate"]) == target:
                return position // CANDIDATE_PAGE_SIZE
        return 0

    def _save_schedule_draft(self, chat_id, candidates):
        with self.input_lock:
            self.schedule_drafts[int(chat_id)] = {
                "expires_at": time.time() + 10 * 60,
                "candidates": [dict(candidate) for candidate in candidates],
            }

    def _save_research_draft(self, chat_id, research):
        token = hashlib.sha256(
            f"{chat_id}:{time.time_ns()}".encode("utf-8")
        ).hexdigest()[:10]
        with self.input_lock:
            self.research_drafts.setdefault(int(chat_id), {})[token] = {
                "expires_at": time.time() + 15 * 60,
                "research": dict(research or {}),
                **dict(research or {}),
            }
        return token

    def _research_draft(self, chat_id, token):
        with self.input_lock:
            drafts = self.research_drafts.get(int(chat_id)) or {}
            draft = drafts.get(str(token))
            if not draft or float(draft.get("expires_at", 0)) < time.time():
                drafts.pop(str(token), None)
                raise ValueError("that research result expired; use /info again")
            return dict(draft)

    def show_research_draft(self, chat_id, token, message_id=None):
        draft = self._research_draft(chat_id, token)
        research = draft.get("research") or draft
        self._present(
            chat_id,
            self.render_research(research),
            self.research_keyboard(token, research),
            message_id,
        )

    def _schedule_candidate_from_ref(self, chat_id, token):
        with self.input_lock:
            draft = self.schedule_drafts.get(int(chat_id))
            if not draft or float(draft.get("expires_at", 0)) < time.time():
                self.schedule_drafts.pop(int(chat_id), None)
                raise ValueError("that schedule preview expired; start a new schedule")
            for candidate in draft.get("candidates", []):
                if candidate_token(candidate) == token:
                    return dict(candidate)
        raise ValueError("that schedule button is stale; start a new schedule")

    def _consume_pending_input(self, chat_id):
        with self.input_lock:
            item = self.pending_inputs.get(int(chat_id))
            if not item:
                return False
            if float(item.get("expires_at", 0)) < time.time():
                self.pending_inputs.pop(int(chat_id), None)
                return None
            self.pending_inputs.pop(int(chat_id), None)
            return item

    def _pending_input(self, chat_id):
        with self.input_lock:
            item = self.pending_inputs.get(int(chat_id))
            if not item:
                return None
            if float(item.get("expires_at", 0)) < time.time():
                self.pending_inputs.pop(int(chat_id), None)
                return None
            return dict(item)

    def _clear_pending_input(self, chat_id):
        with self.input_lock:
            self.pending_inputs.pop(int(chat_id), None)

    def _restore_quantity_input(self, chat_id, pending):
        pending = dict(pending or {})
        pending["expires_at"] = time.time() + 10 * 60
        with self.input_lock:
            self.pending_inputs[int(chat_id)] = pending

    # ------------------------------------------------------------------
    # Keyboards and command menu
    # ------------------------------------------------------------------

    @staticmethod
    def button(text, callback_data):
        return {"text": text, "callback_data": callback_data}

    @staticmethod
    def url_button(text, url):
        href = str(url or "").strip()
        if not href.startswith(("https://", "http://")):
            raise ValueError("button URL must use http or https")
        return {"text": text, "url": href}

    @staticmethod
    def markup(rows):
        return {"inline_keyboard": rows}

    def home_keyboard(self):
        return self.markup([
            [self.button("\U0001f50e Scan for mints", "chains")],
            [
                self.button("\U0001f517 Paste a link", "research:new"),
                self.button("\u23f0 Schedule", "schedule:new"),
            ],
            [
                self.button("\U0001f3a8 Last scan", "candidates"),
                self.button("\U0001f4bc Wallet", "wallet"),
            ],
            [
                self.button("\U0001f4cb Schedules", "schedules"),
                self.button("\u2699\ufe0f More", "settings"),
            ],
        ])

    def status_keyboard(self):
        return self.markup([
            [self.button("🔄 Refresh", "status"), self.button("🔎 Scan a network", "chains")],
            [self.button("⏰ New schedule", "schedule:new"), self.button("📋 My schedules", "schedules")],
            [self.button("⚙️ Automatic mode", "daily"), self.button("🏠 Home", "home")],
        ])

    def chain_keyboard(self, coverage=None, show_empty=False):
        """Network picker ordered by how many drops each network actually has.

        Listing every supported network as equally clickable was the main
        reason /scan felt broken: most have no OpenSea drop on a given day, so
        an honest empty result looked like a failure. Counts come from the
        cached drop calendar and cost no extra API call.
        """
        chains = self.service.supported_chains()
        if coverage is None:
            # Counts could not be read. That is not the same as "no drops", so
            # fall back to the plain list rather than hiding every network.
            rows = [[self.button("\U0001f310 Scan all networks", "scan:all")]]
            for position in range(0, len(chains), 2):
                rows.append([
                    self.button(
                        f"{config.chain_icon(chain)} {pretty_chain(chain)}",
                        f"scan:{chain}",
                    )
                    for chain in chains[position:position + 2]
                ])
            rows.append([
                self.button("\U0001f504 Refresh", "chains:refresh"),
                self.button("\U0001f3e0 Home", "home"),
            ])
            return self.markup(rows)

        counts = {
            str(chain).strip().lower(): int(count)
            for chain, count in coverage.items()
        }
        live = sorted(
            (chain for chain in chains if counts.get(chain)),
            key=lambda chain: (-counts[chain], chain),
        )
        empty = [chain for chain in chains if not counts.get(chain)]

        rows = []
        if live:
            total = sum(counts[chain] for chain in live)
            rows.append([self.button(f"\U0001f310 All networks \u00b7 {total}", "scan:all")])
            for position in range(0, len(live), 2):
                rows.append([
                    self.button(
                        f"{config.chain_icon(chain)} {pretty_chain(chain)} \u00b7 {counts[chain]}",
                        f"scan:{chain}",
                    )
                    for chain in live[position:position + 2]
                ])
        else:
            rows.append([self.button("\U0001f310 Scan all networks", "scan:all")])

        if show_empty:
            for position in range(0, len(empty), 3):
                rows.append([
                    self.button(
                        f"{config.chain_icon(chain)} {pretty_chain(chain)}",
                        f"scan:{chain}",
                    )
                    for chain in empty[position:position + 3]
                ])
        elif empty:
            rows.append([self.button(f"\u2795 Other networks ({len(empty)})", "chains:all")])

        rows.append([
            self.button("\U0001f504 Refresh", "chains:refresh"),
            self.button("\U0001f3e0 Home", "home"),
        ])
        return self.markup(rows)

    def chain_summary_keyboard(self, candidates=None):
        counts = {
            chain: len(items)
            for chain, items in self._chain_candidate_groups(candidates)
        }
        rows = []
        for chain in [
            str(value).strip().lower()
            for value in self.service.supported_chains()
            if str(value).strip()
        ]:
            count = counts.get(chain, 0)
            project_count = len(
                project_groups(self._candidates_for_chain(candidates, chain))
            )
            rows.append([
                self.button(
                    f"⛓ {pretty_chain(chain)} · {project_count} projects / {count} mints",
                    f"candidates:chain:{chain}:page:0",
                )
            ])
        rows.extend([
            [self.button("🔄 Scan all again", "scan:all")],
            [self.button("🔎 Choose one network", "chains"), self.button("🏠 Home", "home")],
        ])
        return self.markup(rows)

    def candidates_keyboard(self, candidates=None, page=0, scan_chain=None):
        source_candidates = self.service.last_candidates if candidates is None else candidates
        scan_chain = str(scan_chain or self.last_scan_chain or "").strip().lower()
        if scan_chain in self.service.supported_chains():
            visible_candidates = self._candidates_for_chain(source_candidates, scan_chain)
            absolute_indexes = [
                index
                for index, candidate in enumerate(source_candidates or [], 1)
                if str(candidate.get("chain") or "").strip().lower() == scan_chain
            ]
            groups = project_groups(visible_candidates, absolute_indexes)
        else:
            visible_candidates = list(source_candidates or [])
            groups = project_groups(visible_candidates)
        total_pages = max(1, (len(groups) + CANDIDATE_PAGE_SIZE - 1) // CANDIDATE_PAGE_SIZE)
        page = min(max(0, int(page)), total_pages - 1)
        first = page * CANDIDATE_PAGE_SIZE
        visible = groups[first:first + CANDIDATE_PAGE_SIZE]
        rows = []
        for group in visible:
            candidate = group["candidate"]
            label = short_text(candidate.get("name", candidate.get("slug", "Candidate")), 30)
            option_count = len(group["options"])
            icon = "🟢" if any(is_free_public_candidate(item) for _, item in group["options"]) else "🎨"
            rows.append([self.button(
                f"{icon} {label} · {option_count}",
                f"project:{project_token(candidate)}",
            )])
        if total_pages > 1:
            navigation = []
            if page > 0:
                callback = (
                    f"candidates:chain:{scan_chain}:page:{page - 1}"
                    if scan_chain in self.service.supported_chains()
                    else f"candidates:page:{page - 1}"
                )
                navigation.append(self.button("⬅️ Previous", callback))
            if page < total_pages - 1:
                callback = (
                    f"candidates:chain:{scan_chain}:page:{page + 1}"
                    if scan_chain in self.service.supported_chains()
                    else f"candidates:page:{page + 1}"
                )
                navigation.append(self.button("Next ➡️", callback))
            rows.append(navigation)
        if scan_chain == "all":
            scan_button = self.button("🔄 Scan all networks again", "scan:all")
        elif scan_chain in self.service.supported_chains():
            scan_button = self.button(f"🔄 Scan {pretty_chain(scan_chain)} again", f"scan:{scan_chain}")
        else:
            scan_button = self.button("🔎 Scan another network", "chains")
        rows.append([scan_button])
        rows.append([self.button("⛓ Change network", "chains"), self.button("🏠 Home", "home")])
        return self.markup(rows)

    def project_keyboard(self, group):
        rows = []
        project_ref = project_token(group["candidate"])
        multiple = len(group["options"]) > 1
        for _, candidate in group["options"]:
            candidate_ref = candidate_token(candidate)
            stage = short_text(candidate.get("stage_label", "Mint"), 14)
            price = short_text(candidate.get("price_display", "Unknown"), 12)
            if multiple:
                mint_label = f"🚀 Mint {stage} · {price}"
                schedule_label = f"⏰ Schedule {stage} · {price}"
            else:
                mint_label = "🚀 Mint now"
                schedule_label = "⏰ Schedule"
            rows.append([
                self.button(mint_label, f"project:mint:{project_ref}:{candidate_ref}"),
                self.button(schedule_label, f"project:schedule:{project_ref}:{candidate_ref}"),
            ])
        candidate = group["candidate"]
        collection_url = candidate.get("opensea_url") or candidate.get("url")
        explicit_mint_url = candidate.get("mint_url")
        if safe_http_url(explicit_mint_url):
            rows.append([self.url_button("🚀 Open mint page", explicit_mint_url)])
        if safe_http_url(collection_url):
            rows.append([self.url_button("🌊 OpenSea drop page", collection_url)])
        project_url = candidate.get("project_url")
        if safe_http_url(project_url) and str(project_url).strip() != str(collection_url or "").strip():
            rows.append([self.url_button("🌐 Project site", project_url)])
        chain = str(candidate.get("chain") or "").strip().lower()
        page = self._project_group_page(group)
        back = (
            self.button(f"↩️ {pretty_chain(chain)} projects", f"candidates:chain:{chain}:page:{page}")
            if chain in self.service.supported_chains()
            else self.button("↩️ All projects", "candidates")
        )
        rows.append([back, self.button("🏠 Home", "home")])
        return self.markup(rows)

    def candidate_detail_keyboard(self, index, candidate=None, page=None):
        candidate = candidate or self._candidate_at(index)
        token = candidate_token(candidate)
        if page is None:
            page = self._candidate_group_page(index, candidate)
        quantity = int(candidate.get("quantity") or config.MINT_QUANTITY)
        wallet_count = len(candidate.get("wallet_ids") or ["primary"])
        rows = []
        if candidate.get("is_sold_out") is not True:
            rows.extend([
                [
                    self.button("🚀 Mint now", f"mint:{index}:{token}:live"),
                    self.button("⏰ Schedule", f"schedule:candidate:{index}:{token}"),
                ],
                [self.button(f"📦 Quantity: {quantity}", f"quantity:candidate:{index}:{token}"),
                 self.button(f"👛 Wallets: {wallet_count}", f"wallets:candidate:{index}:{token}")],
            ])
        rows.append([
            self.button("ℹ️ Mint info", f"info:candidate:{index}:{token}"),
            self.button("🖼 Mint card", f"card:candidate:{index}:{token}"),
        ])
        chain = str(candidate.get("chain") or "").strip().lower()
        back_callback = (
            f"candidates:chain:{chain}:page:{page}"
            if chain in self.service.supported_chains()
            else f"candidates:page:{page}"
        )
        rows.append([
            self.button("↩️ Back to results", back_callback),
            self.button("🏠 Home", "home"),
        ])
        return self.markup(rows)

    def daily_keyboard(self):
        return self.markup([
            [self.button("🚀 Start automatic minting", "daily:live")],
            [self.button("⏹ Stop", "daily:stop"), self.button("📊 Refresh", "daily")],
            [self.button("🏠 Home", "home")],
        ])

    def settings_keyboard(self, custom_background=False):
        """Everything the home screen no longer shows lives one tap in here."""
        rows = [
            [
                self.button("\U0001f4ca Status", "status"),
                self.button("\U0001f9fe Mint history", "wallet:mints"),
            ],
            [
                self.button("\U0001f916 Automatic mode", "daily"),
                self.button("\u2753 Help", "help"),
            ],
            [self.button("\U0001f4b0 Maximum mint price", "settings:cap")],
            [
                self.button("\U0001f441 Preview card", "settings:preview"),
                self.button("\U0001f5bc Card background", "settings:bg"),
            ],
            [
                self.button("\U0001f3a8 Accent color", "settings:accent"),
                self.button("\u270f\ufe0f Card brand", "settings:brand"),
            ],
        ]
        if custom_background:
            rows.append([self.button("\u21a9\ufe0f Reset card background", "settings:bg:reset")])
        rows.append([self.button("\U0001f3e0 Home", "home")])
        return self.markup(rows)

    def schedule_input_keyboard(self):
        return self.markup([
            [self.button("🔬 Research instead", "research:new")],
            [self.button("📋 My schedules", "schedules"), self.button("🏠 Home", "home")],
        ])

    def research_input_keyboard(self):
        return self.markup([
            [self.button("📌 Schedule a mint", "schedule:new")],
            [self.button("🏠 Home", "home")],
        ])

    def schedule_stage_keyboard(self, candidates, page=0):
        candidates = self._ordered_schedule_candidates(candidates)
        total_pages = max(1, (len(candidates) + SCHEDULE_STAGE_PAGE_SIZE - 1) // SCHEDULE_STAGE_PAGE_SIZE)
        page = min(max(0, int(page)), total_pages - 1)
        first = page * SCHEDULE_STAGE_PAGE_SIZE
        visible = candidates[first:first + SCHEDULE_STAGE_PAGE_SIZE]
        rows = []
        for index, candidate in enumerate(visible, first + 1):
            token = candidate_token(candidate)
            name = short_text(candidate.get("name", candidate.get("slug", "Mint")), 20)
            label = short_text(candidate.get("stage_label", "Mint"), 14)
            rows.append([
                self.button(
                    f"{index}. {name} · {label} · {short_text(candidate.get('price_display', 'unknown'), 12)}",
                    f"schedule:stage:{token}",
                )
            ])
        if total_pages > 1:
            navigation = []
            if page > 0:
                navigation.append(self.button("⬅️ Previous", f"schedule:page:{page - 1}"))
            if page < total_pages - 1:
                navigation.append(self.button("Next ➡️", f"schedule:page:{page + 1}"))
            rows.append(navigation)
        rows.append([
            self.button("📌 New URL", "schedule:new"),
            self.button("📋 Schedules", "schedules"),
        ])
        return self.markup(rows)

    def schedule_candidate_keyboard(self, candidate):
        token = candidate_token(candidate)
        quantity = int(candidate.get("quantity") or config.MINT_QUANTITY)
        wallet_count = len(candidate.get("wallet_ids") or ["primary"])
        rows = []
        if candidate.get("is_sold_out") is not True:
            rows.extend([
                [
                    self.button("✅ Schedule mint", f"schedule:live:{token}"),
                    self.button("🚀 Mint now", f"schedule:mint:live:{token}"),
                ],
                [self.button(f"📦 Quantity: {quantity}", f"quantity:schedule:{token}"),
                 self.button(f"👛 Wallets: {wallet_count}", f"wallets:schedule:{token}")],
            ])
        rows.append([
            self.button("ℹ️ Mint info", f"info:schedule:{token}"),
            self.button("🖼 Mint card", f"card:schedule:{token}"),
        ])
        rows.extend([
            [self.button("↩️ Choose another mint", "schedule:stages")],
            [self.button("📋 My schedules", "schedules"), self.button("🏠 Home", "home")],
        ])
        return self.markup(rows)

    def confirm_schedule_keyboard(self, candidate):
        token = candidate_token(candidate)
        return self.markup([
            [self.button("✅ Confirm schedule", f"schedule:live:confirm:{token}")],
            [self.button("↩️ Back", f"schedule:stage:{token}")],
        ])

    def schedules_keyboard(self, schedules):
        rows = [[self.button("➕ New schedule", "schedule:new")]]
        for item in schedules[:10]:
            candidate = item.get("candidate") or {}
            label = short_text(candidate.get("name", candidate.get("slug", "Mint")), 28)
            rows.append([
                self.button(
                    f"📌 {label} · {str(item.get('status', 'unknown')).title()}",
                    f"schedule:id:{item.get('id')}",
                )
            ])
        rows.append([self.button("🔄 Refresh", "schedules"), self.button("🏠 Home", "home")])
        return self.markup(rows)

    def schedule_detail_keyboard(self, schedule):
        rows = []
        if schedule.get("status") in {"armed", "running"}:
            rows.append([self.button("⏹ Cancel schedule", f"schedule:cancel:{schedule.get('id')}")])
        rows.append([
            self.button("📋 My schedules", "schedules"),
            self.button("🏠 Home", "home"),
        ])
        return self.markup(rows)

    def confirm_daily_keyboard(self):
        return self.markup([
            [self.button("✅ Confirm live daily", "daily:live:confirm")],
            [self.button("↩️ Cancel", "daily")],
        ])

    def confirm_mint_keyboard(self, index, candidate=None):
        candidate = candidate or self._candidate_at(index)
        token = candidate_token(candidate)
        return self.markup([
            [self.button("✅ Confirm mint now", f"mint:{index}:{token}:live:confirm")],
            [self.button("↩️ Back", f"candidate:{index}:{token}")],
        ])

    def candidate_card_keyboard(self, index, candidate):
        token = candidate_token(candidate)
        return self.markup([
            [self.button("ℹ️ Mint info", f"info:candidate:{index}:{token}")],
            [self.button("🚀 Mint now", f"mint:{index}:{token}:live")],
            [self.button("🏠 Home", "home")],
        ])

    def schedule_card_keyboard(self, candidate, token):
        return self.markup([
            [self.button("ℹ️ Mint info", f"info:schedule:{token}"), self.button("📌 Schedule preview", f"schedule:stage:{token}")],
            [self.button("🏠 Home", "home")],
        ])

    def research_keyboard(self, token, research):
        candidate = self._research_candidate(research)
        rows = []
        collection_url = (candidate or {}).get("opensea_url") or research.get("opensea_url")
        if safe_http_url(collection_url):
            rows.append([self.url_button("🌊 OpenSea drop page", collection_url)])
        if candidate and candidate.get("is_sold_out") is not True:
            rows.append([
                self.button("🖼 Mint card", f"research:card:{token}"),
                self.button("📦 Quantity", f"quantity:schedule:{candidate_token(candidate)}"),
            ])
            rows.append([
                self.button("👛 Wallets", f"wallets:schedule:{candidate_token(candidate)}")
            ])
            rows.append([self.button("🚀 Mint now", f"research:live:{token}")])
        if research.get("mint_candidates"):
            rows.append([self.button("📌 Choose a mint", f"research:stages:{token}")])
        rows.append([self.button("ℹ️ New mint info", "research:new"), self.button("🏠 Home", "home")])
        return self.markup(rows)

    def research_card_keyboard(self, token, candidate, research):
        rows = [[self.button("ℹ️ Mint info", f"research:view:{token}")]]
        if candidate:
            rows.append([self.button("🚀 Mint now", f"research:live:{token}")])
        rows.append([self.button("🏠 Home", "home")])
        return self.markup(rows)

    def help_keyboard(self):
        return self.markup([[self.button("🏠 Open dashboard", "home")]])

    @staticmethod
    def command_menu():
        return [
            {"command": "start", "description": "open the control center"},
            {"command": "status", "description": "show safe runner status"},
            {"command": "wallet", "description": "balances, NFT counts, and mint status"},
            {"command": "mints", "description": "show recent mint transactions"},
            {"command": "scan", "description": "choose a network and scan OpenSea mints"},
            {"command": "candidates", "description": "review scan results"},
            {"command": "info", "description": "show concise OpenSea hosted-mint info"},
            {"command": "schedule", "description": "arm a one-time mint schedule"},
            {"command": "schedules", "description": "view or cancel schedules"},
            {"command": "daily", "description": "open daily runner controls"},
            {"command": "mint", "description": "review one candidate"},
            {"command": "settings", "description": "mint cap, wallet, and card controls"},
            {"command": "stop", "description": "stop the daily runner"},
            {"command": "help", "description": "show command help"},
        ]

    @staticmethod
    def help_text():
        return (
            "<b>\u2753 How this bot works</b>\n\n"
            "<b>1. Scan</b> \u2014 <code>/scan</code> shows which networks have OpenSea "
            "drops right now, with a count each. Pick one.\n\n"
            "<b>2. Review</b> \u2014 tap a project to see its mint windows, price, "
            "eligibility, and wallet limit.\n\n"
            "<b>3. Mint or schedule</b> \u2014 mint now, or arm a window that has not "
            "opened yet. Both re-check the live drop before signing.\n\n"
            "<b>Have a link already?</b> <code>/info &lt;OpenSea URL&gt;</code> jumps "
            "straight to a drop.\n\n"
            "<b>Other commands</b>\n"
            "<code>/wallet</code> \u00b7 <code>/mints</code> \u00b7 "
            "<code>/schedules</code> \u00b7 <code>/status</code> \u00b7 "
            "<code>/settings</code> \u00b7 <code>/stop</code>\n\n"
            "<i>Live transactions need ENABLE_LIVE_MINTS=true plus a confirmation tap. "
            "Automatic mode only ever attempts free public stages. Schedules run only "
            "while this process is online. The same mint engine is also available from "
            "the terminal with python cli.py.</i>"
        )


def run_from_env():
    from resolver import install as install_secure_dns
    install_secure_dns()
    load_dotenv(ROOT / ".env")
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token or "PASTE_" in token.upper() or "YOUR_" in token.upper():
        print("Set TELEGRAM_BOT_TOKEN in .env first.")
        return 1
    required = [os.getenv(name, "").strip() for name in (
        "ALCHEMY_API_KEY", "PRIVATE_KEY", "WALLET_ADDRESS", "OPENSEA_API_KEY")]
    if any(not value or "PASTE_" in value.upper() or "YOUR_" in value.upper() for value in required):
        print("Fill ALCHEMY_API_KEY, PRIVATE_KEY, WALLET_ADDRESS, and OPENSEA_API_KEY in .env first.")
        return 1
    allowed_chat_id = os.getenv("TELEGRAM_ALLOWED_CHAT_ID", "").strip()
    if allowed_chat_id:
        try:
            int(allowed_chat_id)
        except ValueError:
            print("TELEGRAM_ALLOWED_CHAT_ID must be an integer from Telegram.")
            return 1
    api = TelegramAPI(token)
    try:
        service = DailyMintService(
            *required, notify=lambda message: print(message, flush=True)
        )
    except ValueError as exc:
        api.close()
        print(f"Wallet configuration error: {redact_secrets(exc)}")
        return 1
    bot = TelegramBot(api, service, allowed_chat_id)
    service.notify = bot.notify_operator
    service.notify_result = bot.notify_mint_result
    try:
        completed = bot.run_forever()
    finally:
        service.shutdown()
        api.close()
    return 0 if completed is not False else 1


if __name__ == "__main__":
    raise SystemExit(run_from_env())
