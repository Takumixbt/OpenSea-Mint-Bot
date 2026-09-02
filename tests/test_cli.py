import io
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import cli
import config

CANDIDATE = {
    "slug": "demo-drop",
    "name": "Demo & Drop",
    "chain": "base",
    "chain_id": 8453,
    "stage_index": 0,
    "stage_label": "Public",
    "start_time": 0,
    "end_time": None,
    "price_wei": 0,
    "price_display": "Free",
    "access_label": "Public",
    "is_free": True,
    "is_public": True,
    "url": "https://opensea.io/collection/demo-drop",
}


class FakeCliService:
    def __init__(self, live=False):
        self.live_enabled = live
        self.max_daily_mints = 5
        self.last_candidates = [dict(CANDIDATE)]
        self.mint_calls = []
        self.schedule_calls = []
        self.buy_calls = []
        self.started_daily = False
        self.stopped = False
        self._schedules = []
        self.wallet_calls = []

    def status_snapshot(self):
        return {
            "mode": "stopped",
            "live_enabled": self.live_enabled,
            "wallet_count": 1,
            "chains": ["base"],
            "invalid_chains": [],
            "candidate_count": len(self.last_candidates),
            "project_count": 1,
            "free_candidate_count": 1,
            "last_scan_at": None,
            "last_error_count": 0,
            "attempt_count": 0,
            "max_daily_mints": 5,
            "daily_gas_cap": "0.05",
            "worker_alive": False,
            "stop_requested": False,
            "schedule_count": len(self._schedules),
            "schedule_worker_alive": False,
            "next_schedule_at": None,
            "next_schedule_name": None,
        }

    def chain_coverage(self, force_refresh=False):
        return {"base": 2, "ethereum": 0}, [], 1.5

    def scan_now(self, chain_slug=None, force_refresh=False):
        self.last_scan = {"chain": chain_slug, "refresh": force_refresh}
        return [dict(CANDIDATE)], []

    def candidate_at(self, index):
        if index < 1 or index > len(self.last_candidates):
            raise ValueError("candidate number is out of range; run /scan first")
        return dict(self.last_candidates[index - 1])

    def research_candidate(self, candidate):
        return {
            "name": candidate.get("name"),
            "slug": candidate.get("slug"),
            "chain": candidate.get("chain"),
            "opensea_url": candidate.get("url"),
            "contract_address": "0x0000000000000000000000000000000000000002",
            "mint_candidates": [dict(candidate)],
        }

    def research_reference(self, value):
        extra = dict(CANDIDATE, stage_label="Allowlist", is_public=False, access_label="Allowlist")
        return {
            "name": "Demo & Drop",
            "slug": "demo-drop",
            "chain": "base",
            "opensea_url": "https://opensea.io/collection/demo-drop",
            "mint_candidates": [dict(CANDIDATE), extra],
        }

    def inspect_drop(self, value):
        return self.research_reference(value)["mint_candidates"]

    def supported_chains(self):
        return ["base"]

    def wallet_snapshot(self, chain_slug=None, max_pages=5, wallet_id="primary", **kwargs):
        self.wallet_calls.append({
            "chain": chain_slug,
            "max_pages": max_pages,
            "wallet_id": wallet_id,
        })
        return {
            "address": "0x0000000000000000000000000000000000000001",
            "wallet": {"id": "primary", "label": "Primary", "address": "0x0000000000000000000000000000000000000001"},
            "chains": [{
                "chain": chain_slug or "base",
                "native": "ETH",
                "balance_wei": 12300000000000000,
                "nft_count": 2,
                "nft_count_capped": False,
                "errors": [],
            }],
            "recent_mints": self.mint_history(),
        }

    def mint_history(self, limit=10):
        return [{
            "name": "Demo Drop",
            "slug": "demo-drop",
            "chain": "base",
            "status": "confirmed",
            "tx_hash": "0xabc",
            "at": "2026-09-02T12:00:00+00:00",
        }][:limit]

    def public_wallets(self):
        return [{"id": "primary", "label": "Primary", "address": "0x0000000000000000000000000000000000000001"}]

    def selected_wallets(self, candidate):
        return [SimpleNamespace(id="primary", label="Primary")]

    def funding_snapshot(self, candidate):
        return {
            "native": "ETH",
            "balance_wei": 2 * 10**16,
            "mint_value_wei": 0,
            "estimated_gas_wei": 10**15,
            "maximum_gas_wei": 25 * 10**15,
            "estimated_total_wei": 10**15,
            "maximum_total_wei": 25 * 10**15,
            "estimated_shortfall_wei": 0,
            "wallet_count": 1,
        }

    def mint_candidate(self, candidate):
        self.mint_calls.append(dict(candidate))
        return {"candidate": candidate, "status": "sent", "tx_hash": "0xabc", "confirmed": True}

    def add_schedule(self, candidate):
        item = {
            "id": "sch_test",
            "status": "armed",
            "candidate": dict(candidate),
            "run_at": candidate.get("start_time") or int(time.time()) + 60,
        }
        self.schedule_calls.append(item)
        self._schedules.append(item)
        return item

    def schedules(self, include_finished=True):
        return list(self._schedules)

    def cancel_schedule(self, schedule_id):
        for item in self._schedules:
            if item["id"] == schedule_id:
                item["status"] = "cancelled"
                return dict(item)
        raise ValueError("schedule not found or expired")

    def purchase_preview(self, value):
        return {
            "name": "Demo listing",
            "slug": "demo-drop",
            "chain": "base",
            "price_wei": 10**16,
            "price_display": "0.01 ETH",
            "token_id": "7",
        }

    def buy_listing(self, preview, wallet_id="primary"):
        self.buy_calls.append(dict(preview))
        return {"candidate": {"name": preview.get("name"), "chain": "base"}, "tx_hash": "0xbuy", "confirmed": True}

    def start_daily(self):
        if not self.live_enabled:
            raise RuntimeError("live minting is disabled; set ENABLE_LIVE_MINTS=true first")
        self.started_daily = True

    def stop(self):
        self.stopped = True


def run(service, argv, confirm=None):
    captured = io.StringIO()
    operator = cli.Operator(service, confirm=confirm, output=captured.write)
    parser = cli.build_parser("python cli.py")
    args = parser.parse_args(argv)
    code = cli.run_command(operator, args)
    return code, captured.getvalue(), operator


class CliParserTests(unittest.TestCase):
    def test_parser_accepts_the_operator_commands(self):
        parser = cli.build_parser("python cli.py")
        scan = parser.parse_args(["scan", "base", "--refresh"])
        self.assertEqual(scan.cmd, "scan")
        self.assertEqual(scan.chain, "base")
        self.assertTrue(scan.refresh)
        mint = parser.parse_args(["mint", "1", "--qty", "2", "--stage", "1", "--yes"])
        self.assertEqual(mint.qty, 2)
        self.assertTrue(mint.yes)
        info = parser.parse_args(["info", "https://opensea.io/collection/example"])
        self.assertEqual(info.cmd, "info")

    def test_help_command_does_not_need_env(self):
        code = cli.main(["help"])
        self.assertEqual(code, 0)


class CliFormatTests(unittest.TestCase):
    def test_table_and_clock_and_explorer(self):
        table = cli.format_table(("A", "B"), [("hello", "world")])
        self.assertIn("hello", table)
        self.assertIn("world", table)
        live = cli.clock(int(time.time()) - 10)
        self.assertIn("live", live)
        self.assertTrue(cli.explorer_tx_url("base", "0xabc").startswith("https://basescan.org/tx/"))
        self.assertEqual(cli.explorer_tx_url("base", "nope"), "")
        self.assertEqual(cli.short_address("0x1234567890abcdef1234"), "0x1234…1234")

    def test_group_projects_keeps_scan_indexes(self):
        second = dict(CANDIDATE, slug="other", name="Other")
        grouped = cli.group_projects([CANDIDATE, CANDIDATE, second])
        self.assertEqual(len(grouped), 2)
        self.assertEqual([index for index, _ in grouped[0]["options"]], [1, 2])
        self.assertEqual(grouped[1]["options"][0][0], 3)


class CliOperatorTests(unittest.TestCase):
    def test_scan_and_list_print_numbered_windows(self):
        service = FakeCliService()
        code, text, _ = run(service, ["scan", "base"])
        self.assertEqual(code, 0)
        self.assertIn("Demo & Drop", text)
        self.assertIn("Base", text)
        self.assertEqual(service.last_scan["chain"], "base")
        code, listed, _ = run(service, ["list"])
        self.assertEqual(code, 0)
        self.assertIn("Demo & Drop", listed)

    def test_networks_hides_empty_chains_from_the_busy_table(self):
        code, text, _ = run(FakeCliService(), ["networks"])
        self.assertEqual(code, 0)
        self.assertIn("Base", text)
        self.assertIn("Quiet:", text)
        self.assertIn("ethereum", text)

    def test_info_from_url_remembers_stages_for_later_mint(self):
        service = FakeCliService()
        code, text, operator = run(service, ["info", "https://opensea.io/collection/demo-drop"])
        self.assertEqual(code, 0)
        self.assertIn("Allowlist", text)
        self.assertEqual(len(operator.context_candidates), 2)

    def test_mint_is_preview_only_when_live_mode_is_off(self):
        service = FakeCliService(live=False)
        code, text, _ = run(service, ["mint", "1"])
        self.assertEqual(code, 0)
        self.assertIn("Live minting is disabled", text)
        self.assertEqual(service.mint_calls, [])

    def test_mint_without_confirmation_does_not_send(self):
        service = FakeCliService(live=True)
        code, text, _ = run(service, ["mint", "1"], confirm=lambda prompt: "")
        self.assertEqual(code, 0)
        self.assertIn("Cancelled", text)
        self.assertEqual(service.mint_calls, [])

    def test_mint_yes_sends_the_selected_quantity(self):
        service = FakeCliService(live=True)
        code, text, _ = run(service, ["mint", "1", "--qty", "1", "--yes"])
        self.assertEqual(code, 0)
        self.assertEqual(len(service.mint_calls), 1)
        self.assertEqual(service.mint_calls[0]["quantity"], 1)
        self.assertIn("0xabc", text)

    def test_mint_url_requires_stage_when_several_windows_exist(self):
        service = FakeCliService(live=True)
        code, text, _ = run(service, ["mint", "https://opensea.io/collection/demo-drop", "--yes"])
        self.assertEqual(code, 1)
        self.assertIn("--stage", text)
        self.assertEqual(service.mint_calls, [])

    def test_schedule_yes_arms_a_window(self):
        service = FakeCliService(live=True)
        code, text, _ = run(service, ["schedule", "1", "--yes"])
        self.assertEqual(code, 0)
        self.assertEqual(service.schedule_calls[0]["id"], "sch_test")
        self.assertIn("Armed sch_test", text)

    def test_unknown_wallet_is_rejected_before_signing(self):
        service = FakeCliService(live=True)
        code, text, _ = run(service, ["mint", "1", "--wallets", "Ghost", "--yes"])
        self.assertEqual(code, 1)
        self.assertIn("unknown wallet", text)
        self.assertEqual(service.mint_calls, [])

    def test_wallet_and_history_never_print_private_keys(self):
        service = FakeCliService()
        _, wallet_text, _ = run(service, ["wallet", "base"])
        _, history_text, _ = run(service, ["history"])
        blob = wallet_text + history_text
        self.assertNotIn("PRIVATE_KEY", blob)
        self.assertIn("0.0123 ETH", wallet_text)
        self.assertIn("Demo Drop", history_text)

    def test_fail_redacts_secrets(self):
        captured = io.StringIO()
        operator = cli.Operator(FakeCliService(), output=captured.write)
        with patch.dict(os.environ, {"OPENSEA_API_KEY": "supersecret-opensea-key-value"}):
            operator.fail("boom supersecret-opensea-key-value")
        self.assertNotIn("supersecret-opensea-key-value", captured.getvalue())
        self.assertIn("OPENSEA_API_KEY hidden", captured.getvalue())

    def test_buy_preview_does_not_purchase_when_live_is_off(self):
        service = FakeCliService(live=False)
        code, text, _ = run(service, ["buy", "demo-drop"])
        self.assertEqual(code, 0)
        self.assertIn("Listing preview", text)
        self.assertEqual(service.buy_calls, [])

    def test_daily_stop_does_not_need_confirmation(self):
        service = FakeCliService(live=True)
        code, text, _ = run(service, ["daily", "stop"])
        self.assertEqual(code, 0)
        self.assertTrue(service.stopped)
        self.assertIn("stop", text.lower())

    def test_cap_updates_in_process_ceiling(self):
        original = config.MAX_MINT_PRICE_NATIVE
        try:
            code, text, _ = run(FakeCliService(), ["cap", "mint", "0"])
            self.assertEqual(code, 0)
            self.assertIn("Mint cap is now 0", text)
            self.assertEqual(config.MAX_MINT_VALUE_WEI, 0)
        finally:
            config.set_max_mint_price_native(original)


class CliInteractiveTests(unittest.TestCase):
    def test_banner_and_latency_formatting(self):
        text = cli.banner()
        self.assertIn("Takumi", text)
        self.assertIn("___", text)
        compact = cli.banner(compact=True)
        self.assertIn("OPENSEA MINT BOT", compact)
        self.assertIn("Takumi", compact)
        self.assertIn("ms", cli.format_latency(12))
        self.assertIn("s", cli.format_latency(2500))

    def test_help_prints_the_brand(self):
        with patch("sys.stdout", new_callable=io.StringIO) as stdout:
            code = cli.main(["help"])
        self.assertEqual(code, 0)
        self.assertIn("OPENSEA MINT BOT", stdout.getvalue())
        self.assertIn("Takumi", stdout.getvalue())

    def test_upsert_env_writes_keys_without_touching_unrelated_lines(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / ".env"
            path.write_text("ALCHEMY_API_KEY=old\n# comment\nOTHER=keep\n", encoding="utf-8")
            cli.upsert_env({"ALCHEMY_API_KEY": "new", "PRIVATE_KEY": "0xabc"}, path=path)
            text = path.read_text(encoding="utf-8")
            self.assertIn("ALCHEMY_API_KEY=new", text)
            self.assertIn("PRIVATE_KEY=0xabc", text)
            self.assertIn("# comment", text)
            self.assertIn("OTHER=keep", text)

    def test_upsert_env_does_not_blank_secrets(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / ".env"
            path.write_text("PRIVATE_KEY=keepme\nALCHEMY_API_KEY=old\n", encoding="utf-8")
            cli.upsert_env({"PRIVATE_KEY": "", "OPENSEA_API_KEY": "k"}, path=path)
            text = path.read_text(encoding="utf-8")
            self.assertIn("PRIVATE_KEY=keepme", text)
            self.assertIn("ALCHEMY_API_KEY=old", text)
            self.assertIn("OPENSEA_API_KEY=k", text)

    def test_setup_wizard_saves_env_and_never_prints_the_private_key(self):
        key = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
        captured = io.StringIO()
        answers = iter(["alchemy-test-key", "opensea-test-key", ""])
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / ".env"
            with patch.dict(os.environ, {
                "ALCHEMY_API_KEY": "",
                "PRIVATE_KEY": "",
                "WALLET_ADDRESS": "",
                "OPENSEA_API_KEY": "",
            }):
                address = cli.run_setup_wizard(
                    emit=lambda text: captured.write(str(text) + "\n"),
                    reader=lambda prompt: next(answers),
                    secret_reader=lambda prompt: key,
                    path=path,
                )
            saved = path.read_text(encoding="utf-8")
        self.assertTrue(address.startswith("0x"))
        self.assertIn("PRIVATE_KEY=" + key, saved)
        self.assertNotIn(key, captured.getvalue())
        self.assertIn("Takumi", captured.getvalue())
        self.assertIn(address, captured.getvalue())

    def test_interactive_home_quit_and_scan_to_schedule_preview(self):
        service = FakeCliService(live=False)
        captured = io.StringIO()
        operator = cli.Operator(service, output=lambda text: captured.write(str(text) + "\n"))
        answers = iter(["1", "1", "1", "2", "q"])
        app = cli.InteractiveApp(operator, reader=lambda prompt: next(answers), pause=False)
        code = app.run()
        text = captured.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("Takumi", text)
        self.assertIn("OPENSEA MINT BOT", text)
        self.assertIn("Scan for mints", text)
        self.assertIn("Demo & Drop", text)
        self.assertIn("Schedule this window", text)
        self.assertEqual(service.schedule_calls, [])
        self.assertIn("Live minting is disabled", text)

    def test_wallet_one_shot_base_is_narrower_than_all(self):
        service = FakeCliService()
        code, text, _ = run(service, ["wallet", "base"])
        self.assertEqual(code, 0)
        self.assertEqual(service.wallet_calls[-1]["chain"], "base")
        self.assertEqual(service.wallet_calls[-1]["max_pages"], 2)
        self.assertIn("Base", text)
        code, all_text, _ = run(service, ["wallet", "all"])
        self.assertIsNone(service.wallet_calls[-1]["chain"])
        self.assertEqual(service.wallet_calls[-1]["max_pages"], 1)
        self.assertIn("Loading every network", all_text)

    def test_wallet_eth_alias_maps_to_ethereum(self):
        self.assertEqual(config.resolve_chain_slug("ETH"), "ethereum")
        self.assertEqual(config.resolve_chain_slug("Base"), "base")
        service = FakeCliService()
        code, text, _ = run(service, ["wallet", "ETH"])
        self.assertEqual(code, 0)
        self.assertEqual(service.wallet_calls[-1]["chain"], "ethereum")
        self.assertIn("Ethereum", text)

    def test_interactive_wallet_eth_from_home(self):
        service = FakeCliService()
        captured = io.StringIO()
        operator = cli.Operator(service, output=lambda text: captured.write(str(text) + "\n"))
        answers = iter(["wallet eth", "q"])
        app = cli.InteractiveApp(operator, reader=lambda prompt: next(answers), pause=False)
        code = app.run()
        self.assertEqual(code, 0)
        self.assertEqual(service.wallet_calls[-1]["chain"], "ethereum")
        self.assertEqual(service.wallet_calls[-1]["max_pages"], 1)

    def test_interactive_wallet_defaults_to_one_chain(self):
        service = FakeCliService()
        captured = io.StringIO()
        operator = cli.Operator(service, output=lambda text: captured.write(str(text) + "\n"))
        answers = iter(["3", "", "q"])
        app = cli.InteractiveApp(operator, reader=lambda prompt: next(answers), pause=False)
        code = app.run()
        self.assertEqual(code, 0)
        self.assertEqual(service.wallet_calls[-1]["chain"], "base")
        self.assertEqual(service.wallet_calls[-1]["max_pages"], 1)


if __name__ == "__main__":
    unittest.main()
