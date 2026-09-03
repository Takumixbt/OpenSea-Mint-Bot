"""Terminal control panel for the OpenSea mint bot.

This is the Telegram bot's sibling: same DailyMintService, same confirmation
gates, same price caps. Run it as a one-shot command or as an interactive
shell that can stay online for schedules.

    python cli.py
    python cli.py scan base
    python main.py              (same CLI; --confirm-live still fires config.py)
    python telegram_bot.py      (Telegram)
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from decimal import Decimal
import getpass
import os
import shlex
import sys
import time
from pathlib import Path

import shutil

from dotenv import load_dotenv
from eth_account import Account
from types import SimpleNamespace

import config
from daily_runner import (
    DailyMintService,
    quantity_limit,
    redact_secrets,
    validate_quantity,
)


ROOT = Path(__file__).resolve().parent
PROMPT = "mint> "
LIVE_WORDS = {"yes", "y", "mint", "confirm", "arm", "buy"}
ENV_PATH = ROOT / ".env"
REQUIRED_ENV = ("ALCHEMY_API_KEY", "PRIVATE_KEY", "WALLET_ADDRESS", "OPENSEA_API_KEY")
SECRET_ENV = {"PRIVATE_KEY", "ALCHEMY_API_KEY", "OPENSEA_API_KEY", "TELEGRAM_BOT_TOKEN", "MINT_WALLETS"}


def _enable_windows_ansi():
    if os.name != "nt":
        return
    try:
        import ctypes
        handle = ctypes.windll.kernel32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if ctypes.windll.kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            ctypes.windll.kernel32.SetConsoleMode(handle, mode.value | 0x0004)
    except Exception:
        return


_enable_windows_ansi()


def _color_enabled():
    if os.getenv("NO_COLOR"):
        return False
    return bool(sys.stdout.isatty())


def _paint(text, code):
    if not _color_enabled():
        return str(text)
    return f"\033[{code}m{text}\033[0m"


def bold(text):
    return _paint(text, "1")


def dim(text):
    return _paint(text, "90")


def green(text):
    return _paint(text, "32")


def yellow(text):
    return _paint(text, "33")


def red(text):
    return _paint(text, "31")


def cyan(text):
    return _paint(text, "36")


def white(text):
    return _paint(text, "97")


OPENSEA_ART = (
    "  ___  ___ ___ _  _ ___ ___   _   ",
    " / _ \\| _ \\ __| \\| / __| __| /_\\  ",
    "| (_) |  _/ _|| .` \\__ \\ _| / _ \\ ",
    " \\___/|_| |___|_|\\_|___/___/_/ \\_\\",
)
MINTBOT_ART = (
    " __  __ ___ _  _ _____   ___  ___ _____",
    "|  \\/  |_ _| \\| |_   _| | _ )/ _ \\_   _|",
    "| |\\/| || || .` | | |   | _ \\ (_) || |  ",
    "|_|  |_|___|_|\\_| |_|   |___/\\___/ |_|  ",
)


def _ascii_title_lines():
    paired = [left + "  " + right for left, right in zip(OPENSEA_ART, MINTBOT_ART)]
    try:
        width = shutil.get_terminal_size((120, 24)).columns
    except Exception:
        width = 120
    if width >= max(len(line) for line in paired) + 14:
        return paired
    return list(OPENSEA_ART) + [""] + list(MINTBOT_ART)


def banner(compact=False):
    credit = dim("by Takumi")
    if compact:
        return "\n" + bold("  OPENSEA MINT BOT") + "  " + credit
    lines = _ascii_title_lines()
    rows = []
    for index, line in enumerate(lines):
        if index == len(lines) - 1:
            rows.append(white(line.rstrip()) + "  " + credit)
        else:
            rows.append(white(line))
    return "\n" + "\n".join(rows)


def format_latency(ms):
    try:
        value = float(ms)
    except (TypeError, ValueError):
        return ""
    if value < 1000:
        label = f"{value:.0f} ms"
    else:
        label = f"{value / 1000:.2f} s"
    if value < 500:
        return green(label)
    if value < 2000:
        return yellow(label)
    return red(label)


def timed(function, *args, **kwargs):
    started = time.perf_counter()
    result = function(*args, **kwargs)
    return result, (time.perf_counter() - started) * 1000


def env_filled(name, environ=None):
    value = (environ or os.environ).get(name, "").strip()
    upper = value.upper()
    return bool(value) and "PASTE_" not in upper and "YOUR_" not in upper


def missing_env_names(environ=None):
    return [name for name in REQUIRED_ENV if not env_filled(name, environ)]


def clock(epoch):
    try:
        value = int(epoch or 0)
    except (TypeError, ValueError):
        return "unknown"
    if value <= 0:
        return "unknown"
    when = datetime.fromtimestamp(value, timezone.utc)
    now = datetime.now(timezone.utc)
    label = when.strftime("%Y-%m-%d %H:%M UTC")
    delta = int((when - now).total_seconds())
    if delta <= 0:
        return f"{label} · live"
    if delta < 120:
        return f"{label} · in {delta}s"
    if delta < 3600:
        return f"{label} · in {delta // 60}m"
    if delta < 86400:
        return f"{label} · in {delta // 3600}h{(delta % 3600) // 60:02d}m"
    return f"{label} · in {delta // 86400}d"


def window_label(candidate):
    start = clock(candidate.get("start_time"))
    if candidate.get("is_sold_out") is True:
        return red("sold out")
    end_at = candidate.get("end_time")
    try:
        if end_at and int(end_at) < time.time():
            return dim("ended")
    except (TypeError, ValueError):
        pass
    return start


def wei_to_native(value, symbol="ETH"):
    try:
        amount = Decimal(int(value)) / Decimal(10 ** 18)
    except (TypeError, ValueError):
        return "unknown"
    if amount == 0:
        return f"0 {symbol}"
    text = format(amount, ".8f").rstrip("0").rstrip(".")
    return f"{text} {symbol}"


def explorer_tx_url(chain, tx_hash):
    settings = config.chain_config(chain) or {}
    base = str(settings.get("explorer") or "").rstrip("/")
    value = str(tx_hash or "").strip()
    if not base or not value.startswith("0x"):
        return ""
    return f"{base}/tx/{value}"


def short_address(value):
    text = str(value or "")
    if len(text) < 12:
        return text or "—"
    return f"{text[:6]}…{text[-4:]}"


def access_style(candidate):
    label = str(candidate.get("access_label") or candidate.get("stage_label") or "Unknown")
    if candidate.get("is_sold_out") is True:
        return red(label)
    if candidate.get("is_free") and candidate.get("is_public"):
        return green(label)
    if candidate.get("is_public"):
        return yellow(label)
    return cyan(label)


def price_style(candidate):
    text = str(candidate.get("price_display") or "unknown")
    if candidate.get("is_free") is True or candidate.get("price_wei") == 0:
        return green(text)
    return yellow(text)


def required_env():
    load_dotenv(ENV_PATH)
    missing = missing_env_names()
    if missing:
        raise RuntimeError(
            "fill these in .env first, or run python cli.py and use the setup wizard: "
            + ", ".join(missing)
        )
    return tuple(os.getenv(name, "").strip() for name in REQUIRED_ENV)


def upsert_env(updates, path=None):
    """Write or update keys in .env without printing secret values."""
    path = Path(path or ENV_PATH)
    if path.exists():
        text = path.read_text(encoding="utf-8")
    elif (ROOT / ".env.example").exists():
        text = (ROOT / ".env.example").read_text(encoding="utf-8")
    else:
        text = ""
    lines = text.splitlines()
    seen = set()
    rewritten = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in line:
            key = line.split("=", 1)[0].strip()
            if key in updates:
                value = updates[key]
                if key in SECRET_ENV and not str(value or "").strip():
                    rewritten.append(line)
                    seen.add(key)
                    continue
                rewritten.append(f"{key}={value}")
                seen.add(key)
                continue
        rewritten.append(line)
    for key, value in updates.items():
        if key not in seen:
            if key in SECRET_ENV and not str(value or "").strip():
                continue
            rewritten.append(f"{key}={value}")
    path.write_text("\n".join(rewritten) + "\n", encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


def derive_wallet_address(private_key):
    key = str(private_key or "").strip()
    if not key.startswith("0x"):
        key = "0x" + key
    try:
        return Account.from_key(key).address, key
    except Exception as exc:
        raise ValueError("that private key is not a valid EVM key") from exc


def group_projects(candidates):
    grouped = []
    positions = {}
    for index, candidate in enumerate(candidates or [], 1):
        key = (
            str(candidate.get("chain") or "").lower(),
            str(candidate.get("slug") or "").lower(),
        )
        if key not in positions:
            positions[key] = len(grouped)
            grouped.append({"candidate": candidate, "options": []})
        grouped[positions[key]]["options"].append((index, candidate))
    return grouped


def format_table(headers, rows, max_width=36):
    str_rows = [[str(cell) for cell in row] for row in rows]
    widths = [len(header) for header in headers]
    for row in str_rows:
        for index, cell in enumerate(row):
            widths[index] = min(max_width, max(widths[index], len(cell)))

    def clip(cell, width):
        if len(cell) <= width:
            return cell.ljust(width)
        if width <= 1:
            return cell[:width]
        return cell[: width - 1] + "…"

    lines = [
        "  ".join(clip(header, widths[i]) for i, header in enumerate(headers)),
        "  ".join("─" * widths[i] for i in range(len(headers))),
    ]
    for row in str_rows:
        lines.append("  ".join(clip(row[i], widths[i]) for i in range(len(headers))))
    return "\n".join(lines)


def build_parser(prog=None):
    parser = argparse.ArgumentParser(
        prog=prog or f"python {Path(sys.argv[0]).name}",
        description=(
            "Control OpenSea discovery, research, minting, and schedules from "
            "the terminal. Live sends still need ENABLE_LIVE_MINTS=true and an "
            "explicit confirmation."
        ),
        epilog=(
            "Examples:\n"
            "  python cli.py\n"
            "  python cli.py scan base\n"
            "  python cli.py info https://opensea.io/collection/example\n"
            "  python cli.py mint 1 --qty 1\n"
            "  python cli.py schedule 2 --yes\n"
            "  python cli.py watch\n"
            "Telegram remains available via python telegram_bot.py"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="cmd", metavar="command")

    sub.add_parser("help", help="show commands")
    sub.add_parser("status", help="safe runner snapshot")

    wallet = sub.add_parser("wallet", help="balances, NFT counts, recent mints")
    wallet.add_argument("chain", nargs="?", help="eth, base, all, or another network")

    scan = sub.add_parser("scan", help="scan OpenSea drops")
    scan.add_argument("chain", nargs="?", help="network slug, all, or omit for the picker")
    scan.add_argument("--refresh", action="store_true", help="bypass the drop calendar cache")

    sub.add_parser("list", help="show the last scan")
    sub.add_parser("networks", help="show drop counts per network")

    show = sub.add_parser("show", help="show one scan result")
    show.add_argument("target", help="scan number, starting at 1")

    info = sub.add_parser("info", aliases=["research"], help="research a scan row or OpenSea URL")
    info.add_argument("target", help="scan number or OpenSea collection/drop/item/asset URL")

    mint = sub.add_parser("mint", help="mint now after a confirmation preview")
    _add_action_flags(mint)

    schedule = sub.add_parser("schedule", help="arm a one-time mint at the stage opening")
    _add_action_flags(schedule)

    sub.add_parser("schedules", help="list armed and recent schedules")
    cancel = sub.add_parser("cancel", help="cancel an armed schedule")
    cancel.add_argument("schedule_id")

    history = sub.add_parser("history", aliases=["mints"], help="recent local mint/buy attempts")
    history.add_argument("limit", nargs="?", type=int, default=10)

    buy = sub.add_parser("buy", help="preview or buy the cheapest OpenSea listing")
    buy.add_argument("url", help="OpenSea collection URL or slug")
    buy.add_argument("--yes", action="store_true", help="confirm the purchase")

    daily = sub.add_parser("daily", help="start or stop automatic free-public minting")
    daily.add_argument("action", choices=("start", "stop", "status"))
    daily.add_argument("--yes", action="store_true", help="confirm starting live automatic mode")

    sub.add_parser("watch", help="stay online so armed schedules can fire")
    sub.add_parser("settings", help="show live switch, caps, and wallets")
    sub.add_parser("setup", help="interactive first-run wizard for .env keys")

    cap = sub.add_parser("cap", help="change the in-process mint or buy cap")
    cap.add_argument("kind", choices=("mint", "buy"))
    cap.add_argument("amount")

    return parser


def _add_action_flags(parser):
    parser.add_argument("target", nargs="?", help="scan number or OpenSea URL")
    parser.add_argument("--stage", type=int, help="stage number when a URL has several windows")
    parser.add_argument("--qty", "--quantity", dest="qty", type=int, help="tokens to mint")
    parser.add_argument("--wallets", help="comma-separated wallet labels or ids")
    parser.add_argument("--yes", action="store_true", help="confirm the live action")


class Operator:
    """Thin terminal adapter over DailyMintService."""

    def __init__(self, service, confirm=None, output=None):
        self.service = service
        self.confirm = confirm
        self.out = output or (lambda text: print(text, flush=True))
        self.context_candidates = []
        self.context_label = ""
        self.last_shown = []
        self.last_shown_networks = []
        self.last_scan_chain = None

    def emit(self, text=""):
        self.out(str(text))

    def fail(self, exc):
        self.emit(red(redact_secrets(exc)))
        return 1

    def cmd_help(self, _args=None):
        self.emit(HELP_TEXT)
        return 0

    def cmd_status(self, _args=None):
        snap = self.service.status_snapshot()
        live = green("off") if not snap["live_enabled"] else red("ON")
        rows = [
            ("Live switch", live),
            ("Mode", snap["mode"]),
            ("Wallets", str(snap.get("wallet_count") or 1)),
            ("Projects in last scan", str(snap["project_count"])),
            ("Mint windows in last scan", str(snap["candidate_count"])),
            ("Free + public", str(snap["free_candidate_count"])),
            ("Last scan", snap["last_scan_at"] or "never"),
            ("Today's attempts", f"{snap['attempt_count']}/{snap['max_daily_mints']}"),
            ("Daily gas cap", f"{snap['daily_gas_cap']} native"),
            ("Armed schedules", str(snap["schedule_count"])),
            ("Schedule worker", "running" if snap.get("schedule_worker_alive") else "idle"),
        ]
        if snap.get("next_schedule_name"):
            rows.append(("Next schedule", f"{snap['next_schedule_name']} · {clock(snap.get('next_schedule_at'))}"))
        if snap.get("invalid_chains"):
            rows.append(("Ignored chains", ", ".join(snap["invalid_chains"])))
        self.emit(bold("Status"))
        width = max(len(label) for label, _ in rows)
        for label, value in rows:
            self.emit(f"  {label.ljust(width)}  {value}")
        return 0

    def status_strip(self):
        snap = self.service.status_snapshot()
        live = "LIVE ON" if snap["live_enabled"] else "live off"
        next_item = ""
        if snap.get("next_schedule_name"):
            next_item = f"  ·  next {snap['next_schedule_name']} {clock(snap.get('next_schedule_at'))}"
        return (
            f"  {live}  ·  mint cap {config.MAX_MINT_PRICE_NATIVE}  ·  "
            f"{snap.get('wallet_count') or 1} wallet(s)  ·  "
            f"{snap['schedule_count']} schedule(s){next_item}"
        )

    def cmd_settings(self, _args=None):
        wallets = self.service.public_wallets()
        self.emit(bold("Settings"))
        self.emit(f"  Live minting     {self._live_label()}")
        self.emit(f"  Mint cap         {config.MAX_MINT_PRICE_NATIVE} native / tx")
        self.emit(f"  Buy cap          {config.MAX_BUY_PRICE_NATIVE} native / listing")
        self.emit(f"  Direct SeaDrop   {'on' if config.DIRECT_PUBLIC_SEADROP else 'off'}")
        self.emit(f"  Daily mint limit {self.service.max_daily_mints}")
        self.emit("  Wallets")
        for wallet in wallets:
            self.emit(f"    {wallet['label']:<12} {wallet['id']:<12} {wallet['address']}")
        self.emit(dim("Caps change this process only. Put lasting values in .env."))
        return 0

    def cmd_cap(self, args):
        kind = args.kind
        if kind == "mint":
            value = config.set_max_mint_price_native(args.amount)
            self.emit(f"Mint cap is now {value} native coin for this process.")
        else:
            value = config.set_max_buy_price_native(args.amount)
            self.emit(f"Buy cap is now {value} native coin for this process.")
        return 0

    def cmd_networks(self, args):
        force = bool(getattr(args, "refresh", False))
        (counts, errors, age), ms = timed(self.service.chain_coverage, force_refresh=force)
        busy = [(slug, count) for slug, count in counts.items() if count]
        busy.sort(key=lambda item: (-item[1], item[0]))
        quiet = [slug for slug, count in counts.items() if not count]
        if not counts:
            self.emit("Could not read OpenSea's drop calendar.")
            return 1
        if not busy:
            self.emit("OpenSea has no drops scheduled on any configured network right now.")
            self.emit(dim("That is OpenSea's calendar, not a failed scan. Try: scan --refresh"))
            return 0
        self.emit(bold("Networks with drops") + dim(f"  calendar {int(age)}s old · {format_latency(ms)}"))
        rows = [
            (str(index), config.chain_label(slug), slug, str(count))
            for index, (slug, count) in enumerate(busy, 1)
        ]
        self.emit(format_table(("#", "Network", "Slug", "Drops"), rows, max_width=22))
        self.last_shown_networks = [slug for slug, _ in busy]
        if quiet:
            self.emit(dim(f"Quiet: {', '.join(quiet)}"))
        if errors:
            self.emit(yellow("Notes: " + "; ".join(errors[:6])))
        self.emit(dim("Next: scan base   or   scan all"))
        return 0

    def cmd_scan(self, args):
        chain = (args.chain or "").strip() or None
        if chain:
            resolved = config.resolve_chain_slug(chain)
            if resolved:
                chain = resolved
            else:
                chain = chain.lower()
        if chain in {None, "picker", "networks"}:
            return self.cmd_networks(args)
        (scanned, errors), ms = timed(
            self.service.scan_now,
            None if chain == "all" else chain,
            force_refresh=bool(args.refresh),
        )
        self.context_candidates = []
        self.last_shown = list(scanned)
        self.last_scan_chain = None if chain in {None, "all"} else chain
        label = "every network" if chain == "all" else config.chain_label(chain)
        self.emit(bold(f"Scan · {label}") + f"  {len(scanned)} window(s)  ·  {format_latency(ms)}")
        if scanned:
            self.emit(self._format_scan(scanned, None))
        else:
            self.emit("No mint windows in the current scan horizon.")
            self.emit(dim("Try another network, widen DISCOVERY_WINDOW_HOURS, or paste a link into info."))
        if errors:
            self.emit(yellow("Notes: " + "; ".join(errors[:8])))
        return 0

    def cmd_list(self, _args=None):
        candidates = list(self.service.last_candidates or [])
        if not candidates:
            self.emit("No saved scan. Run: scan base")
            return 0
        self.emit(bold("Last scan") + f"  {len(candidates)} window(s)")
        self.last_shown = list(candidates)
        self.emit(self._format_scan(candidates, None))
        return 0

    def cmd_show(self, args):
        candidate = self._resolve_scan_index(args.target)
        self.emit(self._format_candidate(candidate, heading="Mint window"))
        return 0

    def cmd_info(self, args):
        target = str(args.target or "").strip()
        if target.isdigit():
            candidate = self.service.candidate_at(int(target))
            report, ms = timed(self.service.research_candidate, candidate)
            stages = report.get("mint_candidates") or [candidate]
        else:
            report, ms = timed(self.service.research_reference, target)
            stages = report.get("mint_candidates") or []
            if not stages:
                try:
                    stages, extra_ms = timed(self.service.inspect_drop, target)
                    ms += extra_ms
                except Exception as exc:
                    stages = []
                    report.setdefault("route_note", redact_secrets(exc))
        self.context_candidates = [dict(item) for item in stages]
        self.context_label = str(report.get("name") or report.get("slug") or target)
        self.emit(self._format_research(report, self.context_candidates))
        self.emit(dim(f"  Latency    {format_latency(ms)}"))
        return 0

    def cmd_wallet(self, args):
        chain = getattr(args, "chain", None)
        if isinstance(chain, str):
            chain = chain.strip() or None
        if chain:
            resolved = config.resolve_chain_slug(chain)
            if resolved == "all":
                chain = None
            elif resolved:
                chain = resolved
            else:
                return self.fail(ValueError(
                    f"unknown network '{chain}'. Try eth, base, or all."
                ))
        pages = getattr(args, "max_pages", None)
        if pages is None:
            pages = 1 if chain is None else 2
        if chain is None:
            self.emit(dim("  Loading every network…"))
        snapshot, ms = timed(
            lambda: self.service.wallet_snapshot(
                chain, max_pages=pages, nft_fallback=chain is not None
            )
        )
        wallet = snapshot.get("wallet") or {}
        scope = "every network" if chain is None else config.chain_label(chain)
        self.emit(
            bold("Wallet")
            + f"  {wallet.get('label') or 'Primary'}  {snapshot.get('address')}  ·  {scope}  ·  {format_latency(ms)}"
        )
        if chain is None:
            self.emit(dim("  Tip: wallet eth  or  wallet base is much faster."))
        rows = []
        for entry in snapshot.get("chains") or []:
            symbol = entry.get("native") or "native"
            balance = wei_to_native(entry.get("balance_wei"), symbol) if entry.get("balance_wei") is not None else "—"
            nfts = entry.get("nft_count")
            nft_text = "—" if nfts is None else str(nfts) + ("+" if entry.get("nft_count_capped") else "")
            note = "; ".join(entry.get("errors") or [])
            if not note and entry.get("rpc_source") == "public":
                note = "public RPC"
            rows.append((
                config.chain_label(entry.get("chain")),
                balance,
                nft_text,
                note or "ok",
            ))
        if rows:
            self.emit(format_table(("Network", "Gas", "NFTs", "Note"), rows, max_width=28))
        notes = " ".join(row[3] for row in rows).lower()
        if rows and all(entry.get("balance_wei") is None for entry in snapshot.get("chains") or []):
            if "dns" in notes or "unreachable" in notes:
                self.emit(dim(
                    "  Could not reach an RPC. Check internet/DNS, or set "
                    "MINT_RPC_URL_ETHEREUM / MINT_RPC_URL_BASE in .env."
                ))
        history = snapshot.get("recent_mints") or []
        if history:
            self.emit("")
            self.emit(bold("Recent mints"))
            self.emit(self._format_history(history[:5]))
        return 0

    def cmd_history(self, args):
        records = self.service.mint_history(args.limit or 10)
        if not records:
            self.emit("No local mint or buy attempts recorded yet.")
            return 0
        self.emit(bold("History"))
        self.emit(self._format_history(records))
        return 0

    def cmd_mint(self, args):
        return self._run_live_action("mint", args)

    def cmd_schedule(self, args):
        return self._run_live_action("schedule", args)

    def cmd_schedules(self, _args=None):
        items = self.service.schedules(include_finished=True)
        if not items:
            self.emit("No schedules. Arm one with: schedule <n or URL>")
            return 0
        rows = []
        for item in items:
            candidate = item.get("candidate") or {}
            rows.append((
                item.get("id"),
                item.get("status"),
                candidate.get("name") or candidate.get("slug") or "—",
                config.chain_label(candidate.get("chain")),
                window_label(candidate),
            ))
        self.emit(bold("Schedules"))
        self.emit(format_table(("Id", "Status", "Project", "Chain", "Opens"), rows, max_width=28))
        return 0

    def cmd_cancel(self, args):
        item = self.service.cancel_schedule(args.schedule_id)
        self.emit(f"Cancelled {item.get('id')} ({(item.get('candidate') or {}).get('name') or 'schedule'}).")
        return 0

    def cmd_buy(self, args):
        preview = self.service.purchase_preview(args.url)
        self.emit(bold("Listing preview"))
        self.emit(f"  {(preview.get('name') or preview.get('slug'))}")
        self.emit(f"  Chain     {config.chain_label(preview.get('chain'))}")
        self.emit(f"  Price     {preview.get('price_display') or wei_to_native(preview.get('price_wei'))}")
        self.emit(f"  Token     #{preview.get('token_id') or '—'}")
        if not self.service.live_enabled:
            self.emit(yellow("Live transactions are disabled. Set ENABLE_LIVE_MINTS=true to buy."))
            return 0
        if not args.yes and not self._confirm("Type BUY to purchase this listing: ", extra={"buy"}):
            self.emit("Cancelled.")
            return 0
        result = self.service.buy_listing(preview)
        self.emit(self._format_result(result, fallback_name=preview.get("name")))
        return 0

    def cmd_daily(self, args):
        if args.action == "status":
            return self.cmd_status(args)
        if args.action == "stop":
            self.service.stop()
            self.emit("Automatic mode will stop after the current network call.")
            return 0
        if not self.service.live_enabled:
            self.emit(red("Set ENABLE_LIVE_MINTS=true before starting automatic mode."))
            return 1
        if not args.yes and not self._confirm(
            "Type YES to start automatic free-public minting: "
        ):
            self.emit("Cancelled.")
            return 0
        self.service.start_daily()
        self.emit("Automatic mode is running. It only attempts free public stages.")
        self.emit(dim("Keep this process open, or run: watch"))
        return 0

    def cmd_watch(self, _args=None):
        self.cmd_status()
        self.emit(dim("Watching schedules. Ctrl-C stops the process; armed mints will not fire while it is down."))
        started = time.time()
        last_beat = 0.0
        try:
            while True:
                time.sleep(5)
                now = time.time()
                if now - last_beat < 30:
                    continue
                last_beat = now
                snap = self.service.status_snapshot()
                nxt = snap.get("next_schedule_name") or "none armed"
                self.emit(dim(f"  still here · {int(now - started)}s · next {nxt}"))
        except KeyboardInterrupt:
            self.emit("\nStopped watching.")
            return 0

    def _run_live_action(self, action, args):
        try:
            stages = self._resolve_targets(args.target)
            candidate = self._pick_stage(stages, args.stage)
            if args.qty is not None:
                candidate["quantity"] = validate_quantity(candidate, args.qty)
            else:
                candidate["quantity"] = validate_quantity(
                    candidate, candidate.get("quantity") or config.MINT_QUANTITY
                )
            if args.wallets:
                candidate["wallet_ids"] = self._wallet_ids(args.wallets)
            self.emit(self._format_candidate(candidate, heading="Review"))
            fund_ms = self._emit_funding(candidate)
            if action == "schedule":
                self.emit(f"  Will arm for {window_label(candidate)}")
            if fund_ms is not None:
                self.emit(dim(f"  Preview      {format_latency(fund_ms)}"))
            if not self.service.live_enabled:
                self.emit(yellow("Live minting is disabled. Preview only. Set ENABLE_LIVE_MINTS=true to send."))
                return 0
            prompt = (
                "Type MINT to broadcast now: " if action == "mint"
                else "Type ARM to schedule this mint: "
            )
            extra = {"mint"} if action == "mint" else {"arm", "schedule"}
            if not args.yes and not self._confirm(prompt, extra=extra):
                self.emit("Cancelled. Nothing was signed.")
                return 0
            if action == "mint":
                result, ms = timed(self.service.mint_candidate, candidate)
                self.emit(self._format_result(result, fallback_name=candidate.get("name")))
                self.emit(dim(f"  Send latency {format_latency(ms)}"))
            else:
                schedule, ms = timed(self.service.add_schedule, candidate)
                self.emit(green(
                    f"Armed {schedule['id']} · {(schedule.get('candidate') or {}).get('name')} · "
                    f"{window_label(schedule.get('candidate') or {})}"
                ))
                self.emit(dim(f"  Armed in {format_latency(ms)}. Stay in this window or choose Stay online."))
            return 0
        except Exception as exc:
            return self.fail(exc)

    def _resolve_scan_index(self, target):
        if not str(target).isdigit():
            raise ValueError("show expects a scan number from the last scan")
        return self.service.candidate_at(int(target))

    def _resolve_targets(self, target):
        value = str(target or "").strip()
        if not value:
            if self.context_candidates:
                return [dict(item) for item in self.context_candidates]
            raise ValueError("pass a scan number, an OpenSea URL, or run info first")
        if value.isdigit():
            return [self.service.candidate_at(int(value))]
        stages = self.service.inspect_drop(value)
        self.context_candidates = [dict(item) for item in stages]
        self.context_label = value
        return self.context_candidates

    def _pick_stage(self, stages, stage):
        if not stages:
            raise ValueError("no mint window to use")
        if stage is None:
            if len(stages) == 1:
                return dict(stages[0])
            self.emit(self._format_scan(stages, None, start_index=1))
            raise ValueError(f"this drop has {len(stages)} windows; add --stage 1")
        if stage < 1 or stage > len(stages):
            raise ValueError(f"--stage must be between 1 and {len(stages)}")
        return dict(stages[stage - 1])

    def _wallet_ids(self, raw):
        wanted = [part.strip().lower() for part in str(raw).split(",") if part.strip()]
        if not wanted:
            return []
        known = self.service.public_wallets()
        resolved = []
        for token in wanted:
            match = next(
                (
                    wallet for wallet in known
                    if wallet["id"].lower() == token or wallet["label"].lower() == token
                ),
                None,
            )
            if match is None:
                labels = ", ".join(wallet["label"] for wallet in known)
                raise ValueError(f"unknown wallet {token!r}; configured: {labels}")
            resolved.append(match["id"])
        return resolved

    def _confirm(self, prompt, extra=None):
        if self.confirm is None:
            if not sys.stdin.isatty():
                raise ValueError("non-interactive session needs --yes to confirm a live action")
            try:
                answer = input(prompt)
            except EOFError as exc:
                raise ValueError("confirmation was not given") from exc
        else:
            answer = self.confirm(prompt)
        words = {item.lower() for item in LIVE_WORDS}
        if extra:
            words.update(item.lower() for item in extra)
        return str(answer or "").strip().lower() in words

    def _live_label(self):
        return red("ON") if self.service.live_enabled else green("off")

    def _format_scan(self, candidates, chain_filter, start_index=1):
        rows = []
        for offset, candidate in enumerate(candidates, start_index):
            if chain_filter and chain_filter not in {"all", None}:
                if str(candidate.get("chain") or "").lower() != chain_filter:
                    continue
            rows.append((
                str(offset),
                candidate.get("name") or candidate.get("slug") or "—",
                config.chain_label(candidate.get("chain")),
                candidate.get("stage_label") or candidate.get("access_label") or "—",
                candidate.get("price_display") or "—",
                window_label(candidate),
            ))
        if not rows:
            return "Nothing to show."
        return format_table(("#", "Project", "Chain", "Stage", "Price", "Opens"), rows)

    def _format_candidate(self, candidate, heading="Mint"):
        qty = candidate.get("quantity") or config.MINT_QUANTITY
        wallets = self.service.selected_wallets(candidate)
        lines = [
            bold(heading),
            f"  {candidate.get('name') or candidate.get('slug')}",
            f"  Chain      {config.chain_label(candidate.get('chain'))}",
            f"  Stage      {candidate.get('stage_label') or '—'} · {access_style(candidate)}",
            f"  Price      {price_style(candidate)}",
            f"  Quantity   {qty}  (max {quantity_limit(candidate)})",
            f"  Opens      {window_label(candidate)}",
            f"  Route      {candidate.get('route_label') or candidate.get('route') or 'OpenSea'}",
            f"  Contract   {candidate.get('contract_address') or '—'}",
            f"  Wallets    {', '.join(getattr(item, 'label', None) or item.get('label') for item in wallets)}",
            f"  Link       {candidate.get('opensea_url') or candidate.get('url') or '—'}",
        ]
        if candidate.get("is_sold_out") is True:
            lines.append(red("  This stage is sold out."))
        return "\n".join(lines)

    def _format_research(self, report, stages):
        lines = [
            bold(report.get("name") or report.get("slug") or "OpenSea"),
            f"  Slug       {report.get('slug') or '—'}",
            f"  Chain      {config.chain_label(report.get('chain') or (stages[0].get('chain') if stages else ''))}",
            f"  Contract   {report.get('contract_address') or (stages[0].get('contract_address') if stages else '—')}",
            f"  Link       {report.get('opensea_url') or '—'}",
        ]
        if report.get("minting_status"):
            lines.append(f"  Status     {report.get('minting_status')}")
        if report.get("route_note"):
            lines.append(dim(f"  Note       {report.get('route_note')}"))
        if stages:
            lines.append("")
            lines.append(bold("Mint windows"))
            lines.append(self._format_scan(stages, None, start_index=1))
            lines.append(dim("Mint or schedule with: mint --stage 1   or   schedule --stage 1"))
        else:
            lines.append(yellow("  No safe mint route from this link."))
        return "\n".join(lines)

    def _emit_funding(self, candidate):
        try:
            snap, ms = timed(self.service.funding_snapshot, candidate)
        except Exception as exc:
            self.emit(dim(f"  Funding    could not be read ({redact_secrets(exc)})"))
            return None
        symbol = snap.get("native") or "native"
        self.emit(
            f"  Value      {wei_to_native(snap.get('mint_value_wei'), symbol)} + "
            f"~{wei_to_native(snap.get('estimated_gas_wei'), symbol)} gas"
        )
        self.emit(
            f"  Balance    {wei_to_native(snap.get('balance_wei'), symbol)}  "
            f"need ≤ {wei_to_native(snap.get('maximum_total_wei'), symbol)}"
        )
        if int(snap.get("estimated_shortfall_wei") or 0) > 0:
            self.emit(red(
                f"  Shortfall  {wei_to_native(snap.get('estimated_shortfall_wei'), symbol)}"
            ))
        return ms

    def _format_history(self, records):
        rows = []
        for item in records:
            tx = item.get("tx_hash") or ""
            rows.append((
                item.get("status") or "—",
                item.get("name") or item.get("slug") or "—",
                config.chain_label(item.get("chain")),
                tx[:10] + "…" if len(tx) > 12 else (tx or "—"),
                item.get("at") or "",
            ))
        return format_table(("Status", "Project", "Chain", "Tx", "When"), rows, max_width=24)

    def _format_result(self, result, fallback_name="mint"):
        candidate = result.get("candidate") or {}
        name = candidate.get("name") or fallback_name
        tx = result.get("tx_hash") or ""
        url = explorer_tx_url(candidate.get("chain") or result.get("chain"), tx)
        if result.get("confirmed") is True:
            line = green(f"Confirmed {name}. {tx}")
        elif tx:
            line = yellow(f"Sent {name} but confirmation is not success yet. {tx}")
        else:
            line = red(f"{name} finished without a transaction hash.")
        if url:
            line += f"\n  {url}"
        return line

    def cmd_setup(self, _args=None):
        try:
            run_setup_wizard(emit=self.emit)
        except Exception as exc:
            return self.fail(exc)
        self.emit(dim("  Restart the CLI if you changed the signing wallet."))
        return 0


HELP_TEXT = """
OPENSEA MINT BOT  ·  by Takumi

Run with no arguments for the interactive menu.
One-shot commands still work:

  scan [chain|all] [--refresh]   Network picker, or scan one/all networks
  info <n|url>                   Research a scan row or any OpenSea URL
  mint <n|url> [--stage N] [--qty N] [--yes]
  schedule <n|url> [--yes]       Arm a one-time mint at the opening second
  wallet [eth|base|all]          Balance and NFT count for one network, or all

Live sends need ENABLE_LIVE_MINTS=true plus confirmation.
Telegram: python telegram_bot.py
""".strip()


HANDLERS = {
    "help": "cmd_help",
    "status": "cmd_status",
    "settings": "cmd_settings",
    "setup": "cmd_setup",
    "cap": "cmd_cap",
    "networks": "cmd_networks",
    "scan": "cmd_scan",
    "list": "cmd_list",
    "show": "cmd_show",
    "info": "cmd_info",
    "research": "cmd_info",
    "wallet": "cmd_wallet",
    "history": "cmd_history",
    "mints": "cmd_history",
    "mint": "cmd_mint",
    "schedule": "cmd_schedule",
    "schedules": "cmd_schedules",
    "cancel": "cmd_cancel",
    "buy": "cmd_buy",
    "daily": "cmd_daily",
    "watch": "cmd_watch",
}


def run_command(operator, args):
    name = HANDLERS.get(args.cmd)
    if not name:
        operator.emit("Unknown command. Type help.")
        return 2
    return getattr(operator, name)(args)


def make_service():
    alchemy, key, address, opensea = required_env()
    service = DailyMintService(
        alchemy, key, address, opensea,
        notify=lambda message: print(f"[{time.strftime('%H:%M:%S')}] {redact_secrets(message)}", flush=True),
    )
    return service


def _ask(prompt, reader=None):
    source = reader or input
    return str(source(prompt) or "").strip()


def _ask_secret(prompt, reader=None, secret_reader=None):
    if secret_reader is not None:
        return str(secret_reader(prompt) or "").strip()
    if reader is not None:
        return str(reader(prompt) or "").strip()
    try:
        return getpass.getpass(prompt).strip()
    except Exception:
        print(dim("  This terminal cannot hide typing. The key still stays on this computer."))
        return input(prompt).strip()


def run_setup_wizard(emit=None, reader=None, secret_reader=None, path=None):
    """Collect keys in the terminal and write them to a gitignored .env."""
    emit = emit or (lambda text: print(text, flush=True))
    path = Path(path or ENV_PATH)
    load_dotenv(path)
    emit(banner())
    emit("  First-run setup. Keys stay in local .env and are never printed back.")
    emit(dim("  Use a throwaway wallet. Never paste a seed phrase."))
    emit("")

    def current(name):
        return os.getenv(name, "").strip()

    alchemy = _ask("  Alchemy API key: ", reader) or (current("ALCHEMY_API_KEY") if env_filled("ALCHEMY_API_KEY") else "")
    if not alchemy:
        raise ValueError("Alchemy API key is required")

    existing_key = current("PRIVATE_KEY") if env_filled("PRIVATE_KEY") else ""
    secret_prompt = "  Wallet private key (hidden; Enter keeps current): " if existing_key else "  Wallet private key (hidden): "
    private_key = _ask_secret(secret_prompt, reader, secret_reader) or existing_key
    if not private_key:
        raise ValueError("a wallet private key is required")
    address, private_key = derive_wallet_address(private_key)
    emit(f"  Public address  {address}")

    opensea = _ask("  OpenSea API key: ", reader) or (current("OPENSEA_API_KEY") if env_filled("OPENSEA_API_KEY") else "")
    if not opensea:
        raise ValueError("OpenSea API key is required")

    telegram = _ask("  Telegram bot token (optional, Enter to skip): ", reader)
    chat_id = ""
    if telegram:
        chat_id = _ask("  Telegram chat ID: ", reader)

    updates = {
        "ALCHEMY_API_KEY": alchemy,
        "PRIVATE_KEY": private_key,
        "WALLET_ADDRESS": address,
        "OPENSEA_API_KEY": opensea,
        "ENABLE_LIVE_MINTS": current("ENABLE_LIVE_MINTS") or "false",
        "MAX_MINT_PRICE_NATIVE": current("MAX_MINT_PRICE_NATIVE") or "0",
        "MAX_BUY_PRICE_NATIVE": current("MAX_BUY_PRICE_NATIVE") or "0",
    }
    if telegram:
        updates["TELEGRAM_BOT_TOKEN"] = telegram
        if chat_id:
            updates["TELEGRAM_ALLOWED_CHAT_ID"] = chat_id
    upsert_env(updates, path=path)
    load_dotenv(path, override=True)
    emit(green("  Saved. Live minting is still off until you enable it in Settings."))
    return address


class InteractiveApp:
    """Numbered-menu control surface for the full mint flow."""

    def __init__(self, operator, reader=None, pause=True):
        self.op = operator
        self.reader = reader or input
        self.pause = pause
        self._homes = 0

    def ask(self, prompt="  Choose: "):
        return _ask(prompt, self.reader)

    def wait(self):
        if not self.pause:
            return
        self.ask("  Enter to continue: ")

    def run(self):
        while True:
            try:
                self.op.emit(banner(compact=self._homes > 0))
                if self._homes == 0:
                    self.op.emit(dim("  Warming OpenSea's drop calendar in the background…"))
                self._homes += 1
                self.op.emit(self.op.status_strip())
                self.op.emit("")
                self.op.emit(f"  {bold('[1]')}  Scan for mints")
                self.op.emit(f"  {bold('[2]')}  Paste an OpenSea link")
                self.op.emit(f"  {bold('[3]')}  My wallet")
                self.op.emit(f"  {bold('[4]')}  Schedules")
                self.op.emit(f"  {bold('[5]')}  History")
                self.op.emit(f"  {bold('[6]')}  Settings / setup")
                self.op.emit(f"  {bold('[7]')}  Stay online for schedules")
                self.op.emit(f"  {bold('[q]')}  Quit")
                self.op.emit("")
                choice = self.ask().lower()
                parts = choice.split()
                verb = parts[0] if parts else ""
                if verb in {"q", "quit", "exit"}:
                    self.op.emit("  Bye.")
                    return 0
                if verb in {"1", "scan"}:
                    self._scan_flow()
                elif verb in {"2", "link", "info", "paste"}:
                    self._link_flow()
                elif verb in {"3", "wallet"}:
                    self._wallet_flow(parts[1] if len(parts) > 1 else None)
                elif verb in {"4", "schedules"}:
                    self._schedule_menu()
                elif verb in {"5", "history"}:
                    self.op.cmd_history(SimpleNamespace(limit=10))
                    self.wait()
                elif verb in {"6", "settings", "setup"}:
                    self._settings_menu()
                elif verb in {"7", "watch"}:
                    self._watch()
                else:
                    self.op.emit(dim("  Use 1-7, wallet eth, wallet base, or q."))
            except KeyboardInterrupt:
                self.op.emit("\n  Back to home. q quits.")
            except Exception as exc:
                self.op.fail(exc)
                self.wait()

    def _scan_flow(self):
        self.op.cmd_networks(SimpleNamespace(refresh=False))
        self.op.emit("")
        self.op.emit(dim("  Number = that network   a = all   r = refresh   p = paste link   b = back"))
        choice = self.ask()
        if choice.lower() in {"", "b", "back"}:
            return
        if choice.lower() in {"r", "refresh"}:
            self.op.cmd_networks(SimpleNamespace(refresh=True))
            choice = self.ask("  Network number, a, p, or b: ")
            if choice.lower() in {"", "b", "back"}:
                return
        if choice.lower() in {"p", "paste"}:
            self._link_flow()
            return
        chain = "all" if choice.lower() in {"a", "all"} else self._network_from_choice(choice)
        if chain is None:
            self.op.emit(yellow("  That is not a network on the list."))
            self.wait()
            return
        self.op.cmd_scan(SimpleNamespace(chain=chain, refresh=False))
        self._pick_window(self.op.last_shown)

    def _network_from_choice(self, choice):
        if not choice.isdigit():
            slug = config.resolve_chain_slug(choice.strip())
            if slug and slug != "all" and config.chain_config(slug):
                return slug
            return None
        shown = getattr(self.op, "last_shown_networks", None) or []
        index = int(choice)
        if 1 <= index <= len(shown):
            return shown[index - 1]
        return None

    def _default_wallet_chain(self):
        shown = self.op.last_shown or []
        if shown:
            slug = str(shown[0].get("chain") or "").strip().lower()
            if slug and config.chain_config(slug):
                return slug
        if self.op.last_scan_chain and config.chain_config(self.op.last_scan_chain):
            return self.op.last_scan_chain
        if config.chain_config("base"):
            return "base"
        chains = self.op.service.supported_chains()
        return chains[0] if chains else "all"

    def _wallet_flow(self, preset=None):
        token = str(preset or "").strip()
        if not token:
            hint = self._default_wallet_chain()
            raw = self.ask(f"  Network [{hint}]  ·  eth / base / all: ")
            token = (raw or hint).strip()
        resolved = config.resolve_chain_slug(token) if token else None
        if token.lower() in {"all", "a", "*"} or resolved == "all":
            self.op.cmd_wallet(SimpleNamespace(chain="all", max_pages=1))
            self.wait()
            return
        if not resolved or not config.chain_config(resolved):
            self.op.emit(yellow("  Unknown network. Try eth, base, or all."))
            self.wait()
            return
        self.op.cmd_wallet(SimpleNamespace(chain=resolved, max_pages=1))
        self.wait()

    def _link_flow(self):
        url = self.ask("  Paste an OpenSea collection, drop, item, or asset URL: ")
        if not url:
            return
        self.op.cmd_info(SimpleNamespace(target=url))
        if self.op.context_candidates:
            self._pick_window(self.op.context_candidates, from_context=True)
        else:
            self.wait()

    def _pick_window(self, candidates=None, from_context=False):
        rows = list(candidates if candidates is not None else (self.op.service.last_candidates or []))
        if from_context:
            rows = list(self.op.context_candidates or rows)
        if not rows:
            self.wait()
            return
        self.op.emit("")
        self.op.emit(dim("  Number = inspect that mint   b = back"))
        choice = self.ask()
        if choice.lower() in {"", "b", "back"}:
            return
        if not choice.isdigit() or not 1 <= int(choice) <= len(rows):
            self.op.emit(yellow("  That number is not on the list."))
            self.wait()
            return
        candidate = dict(rows[int(choice) - 1])
        self.op.context_candidates = [dict(candidate)]
        self.op.emit(self.op._format_candidate(candidate, heading="Mint window"))
        self._window_actions(candidate)

    def _window_actions(self, candidate):
        while True:
            self.op.emit("")
            self.op.emit(f"  {bold('[1]')}  Mint now")
            self.op.emit(f"  {bold('[2]')}  Schedule this window")
            self.op.emit(f"  {bold('[3]')}  Change quantity")
            self.op.emit(f"  {bold('[b]')}  Back")
            choice = self.ask().lower()
            if choice in {"b", "back", ""}:
                return
            if choice == "3":
                raw = self.ask("  Quantity: ")
                try:
                    candidate["quantity"] = validate_quantity(candidate, raw)
                    self.op.emit(f"  Quantity set to {candidate['quantity']}.")
                except Exception as exc:
                    self.op.fail(exc)
                continue
            if choice not in {"1", "2"}:
                continue
            action = "mint" if choice == "1" else "schedule"
            self.op.context_candidates = [dict(candidate)]
            self.op._run_live_action(action, SimpleNamespace(
                target=None,
                stage=None,
                qty=candidate.get("quantity"),
                wallets=None,
                yes=False,
            ))
            self.wait()
            return

    def _schedule_menu(self):
        self.op.cmd_schedules()
        items = self.op.service.schedules(include_finished=True)
        if not items:
            self.wait()
            return
        self.op.emit(dim("  c <id> cancels   7 from home stays online   b = back"))
        choice = self.ask()
        if choice.lower() in {"", "b", "back"}:
            return
        parts = choice.split()
        if parts and parts[0].lower() in {"c", "cancel"} and len(parts) >= 2:
            self.op.cmd_cancel(SimpleNamespace(schedule_id=parts[1]))
        elif choice.lower() in {"7", "watch"}:
            self._watch()
        self.wait()

    def _settings_menu(self):
        self.op.cmd_settings()
        self.op.emit("")
        self.op.emit(f"  {bold('[1]')}  Enter / replace API keys and private key")
        self.op.emit(f"  {bold('[2]')}  Set mint price cap")
        self.op.emit(f"  {bold('[3]')}  Set buy price cap")
        self.op.emit(f"  {bold('[b]')}  Back")
        choice = self.ask().lower()
        if choice == "1":
            self.op.cmd_setup()
        elif choice == "2":
            amount = self.ask("  Mint cap in native coin (0 = free only): ")
            if amount:
                self.op.cmd_cap(SimpleNamespace(kind="mint", amount=amount))
        elif choice == "3":
            amount = self.ask("  Buy cap in native coin (0 = locked): ")
            if amount:
                self.op.cmd_cap(SimpleNamespace(kind="buy", amount=amount))
        self.wait()

    def _watch(self):
        self.op.emit(bold("  Staying online so armed schedules can fire."))
        self.op.emit(dim("  Ctrl-C returns to the menu. Closing the window cancels the watch."))
        self.op.cmd_status()
        started = time.time()
        last_beat = 0.0
        try:
            while True:
                time.sleep(5)
                now = time.time()
                if now - last_beat < 30:
                    continue
                last_beat = now
                snap = self.op.service.status_snapshot()
                nxt = snap.get("next_schedule_name") or "none armed"
                self.op.emit(dim(f"  still here · {int(now - started)}s · next {nxt}"))
        except KeyboardInterrupt:
            self.op.emit("\n  Watch paused. Schedules stay armed on disk.")


def ensure_ready(emit=None, reader=None, secret_reader=None):
    load_dotenv(ENV_PATH)
    if missing_env_names():
        run_setup_wizard(emit=emit, reader=reader, secret_reader=secret_reader)
        load_dotenv(ENV_PATH, override=True)
        if missing_env_names():
            raise RuntimeError("setup is incomplete")


def repl(operator, parser=None, reader=None):
    return InteractiveApp(operator, reader=reader).run()


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.cmd == "help":
        print(banner())
        print(HELP_TEXT)
        return 0
    if args.cmd == "setup":
        try:
            run_setup_wizard()
            return 0
        except Exception as exc:
            print(redact_secrets(exc), file=sys.stderr)
            return 1
    service = None
    try:
        if not args.cmd:
            ensure_ready()
        service = make_service()
        operator = Operator(service)
        if not args.cmd:
            service.prewarm_calendar()
            return InteractiveApp(operator).run()
        return run_command(operator, args)
    except KeyboardInterrupt:
        print("\nStopped.")
        return 130
    except Exception as exc:
        print(redact_secrets(exc), file=sys.stderr)
        return 1
    finally:
        if service is not None:
            service.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
