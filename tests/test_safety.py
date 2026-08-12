import os
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import config
import daily_runner
import discovery
import opensea_client
import nft_card
from daily_runner import DailyMintService, quantity_limit, validate_quantity
from minter import Minter
from telegram_bot import TelegramBot


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


class FakeAPI:
    def __init__(self):
        self.sent = []
        self.edited = []
        self.photos = []

    def send_message(self, *args, **kwargs):
        self.sent.append((args, kwargs))
        return {"message_id": 99}

    def edit_message_text(self, *args, **kwargs):
        self.edited.append((args, kwargs))
        return {"message_id": 1}

    def answer_callback(self, *args, **kwargs):
        return True

    def send_photo(self, *args, **kwargs):
        self.photos.append((args, kwargs))
        return {"message_id": 100}

    def download_file(self, file_id, destination):
        from PIL import Image
        Image.new("RGB", (1200, 675), "navy").save(destination, format="JPEG")
        return Path(destination)


class FakeTelegramService:
    live_enabled = True
    max_daily_mints = 5
    max_daily_gas_wei = 10**18
    last_candidates = [CANDIDATE]
    last_errors = []

    def configured_chains(self):
        return ["base"]

    def supported_chains(self):
        return ["base"]

    def candidate_at(self, index):
        if index != 1:
            raise ValueError("candidate number is out of range; run /scan first")
        return dict(self.last_candidates[0])

    def status_snapshot(self):
        return {
            "mode": "stopped",
            "live_enabled": False,
            "chains": ["base"],
            "invalid_chains": [],
            "candidate_count": 1,
            "project_count": 1,
            "last_scan_at": None,
            "last_error_count": 0,
            "attempt_count": 0,
            "max_daily_mints": 5,
            "daily_gas_cap": "0.05",
            "worker_alive": False,
            "stop_requested": False,
        }

    def start_daily(self):
        return None

    def stop(self):
        return None

    def mint_candidate(self, candidate):
        return {"candidate": candidate, "status": "sent", "tx_hash": "0xabc", "confirmed": True}

    def enrich_candidate(self, candidate):
        return dict(candidate, metadata_loaded=True)

    def research_candidate(self, candidate):
        return {
            "slug": candidate["slug"],
            "name": candidate["name"],
            "chain": candidate["chain"],
            "opensea_url": candidate["url"],
            "candidate": dict(candidate),
        }

    def inspect_drop(self, value):
        return [dict(self.last_candidates[0])]


class FakeEngine:
    def __init__(self):
        self.calls = 0

    def execute(self, candidate, **kwargs):
        self.calls += 1
        time.sleep(0.02)
        return {
            "candidate": candidate,
            "status": "sent",
            "tx_hash": "0xabc",
            "confirmed": True,
            "worst_case_gas_wei": 1,
        }


class FakeMintResponse:
    status_code = 200
    headers = {}

    @staticmethod
    def json():
        return {
            "to": "0x0000000000000000000000000000000000000002",
            "data": "0x1234",
            "value": "0",
        }


class FakeMintClient:
    def __init__(self):
        self.payload = None

    def post(self, endpoint, **kwargs):
        self.payload = {"endpoint": endpoint, **kwargs}
        return FakeMintResponse()


class TelegramSafetyTests(unittest.TestCase):
    def test_scan_flow_requires_one_network_and_has_no_all_chains_button(self):
        api = FakeAPI()
        bot = TelegramBot(api, FakeTelegramService(), 123)

        keyboard = bot.chain_keyboard()
        callbacks = [
            button.get("callback_data")
            for row in keyboard["inline_keyboard"]
            for button in row
        ]
        self.assertIn("scan:base", callbacks)
        self.assertNotIn("scan:all", callbacks)

        bot.start_scan(123)
        self.assertIn("Which network", api.sent[-1][0][1])
        bot.start_scan(123, "all")
        self.assertIn("Choose one network at a time", api.sent[-1][0][1])

    def test_scan_results_rescan_only_the_selected_network(self):
        bot = TelegramBot(FakeAPI(), FakeTelegramService(), 123)
        bot.last_scan_chain = "base"
        keyboard = bot.candidates_keyboard()
        callbacks = [
            button.get("callback_data")
            for row in keyboard["inline_keyboard"]
            for button in row
        ]
        self.assertIn("scan:base", callbacks)
        self.assertNotIn("scan:all", callbacks)

    def test_candidate_buttons_are_bound_to_a_specific_scan(self):
        api = FakeAPI()
        bot = TelegramBot(api, FakeTelegramService(), 123)
        keyboard = bot.candidates_keyboard()
        callback = keyboard["inline_keyboard"][0][0]["callback_data"]

        self.assertTrue(callback.startswith("project:"))
        bot.handle_callback({
            "id": "callback-1",
            "message": {"chat": {"id": 123}, "message_id": 1},
            "data": callback,
        })
        self.assertIn("Demo &amp; Drop", api.edited[-1][0][2])
        candidate_callback = api.edited[-1][0][3]["inline_keyboard"][0][0]["callback_data"]
        self.assertTrue(candidate_callback.startswith("candidate:1:"))

        with self.assertRaisesRegex(ValueError, "stale"):
            bot._candidate_from_ref("candidate:1:0000000000")

    def test_candidate_list_is_paginated_without_losing_absolute_indexes(self):
        service = FakeTelegramService()
        service.last_candidates = [
            dict(CANDIDATE, slug=f"demo-drop-{index}", name=f"Demo Drop {index}")
            for index in range(10)
        ]
        bot = TelegramBot(FakeAPI(), service, 123)

        keyboard = bot.candidates_keyboard()
        page_button = next(
            button
            for row in keyboard["inline_keyboard"]
            for button in row
            if button.get("callback_data") == "candidates:page:1"
        )
        self.assertEqual(page_button["callback_data"], "candidates:page:1")
        self.assertTrue(keyboard["inline_keyboard"][7][0]["callback_data"].startswith("project:"))

        text = bot.render_candidates(service.last_candidates, [], page=1)
        self.assertIn("Page 2 of 2", text)
        self.assertIn("10. Demo Drop 9", text)

    def test_scan_groups_multiple_mint_windows_under_one_project(self):
        service = FakeTelegramService()
        service.last_candidates = [
            dict(CANDIDATE, stage_index=0, stage_label="Allowlist", start_time=100),
            dict(CANDIDATE, stage_index=1, stage_label="Public", start_time=200),
        ]
        bot = TelegramBot(FakeAPI(), service, 123)
        text = bot.render_candidates(service.last_candidates, [])
        keyboard = bot.candidates_keyboard()

        self.assertIn("1 projects · 2 mint options", text)
        project_buttons = [
            row for row in keyboard["inline_keyboard"]
            if row and row[0].get("callback_data", "").startswith("project:")
        ]
        self.assertEqual(len(project_buttons), 1)
        self.assertTrue(project_buttons[0][0]["callback_data"].startswith("project:"))
        group = bot._project_from_ref(project_buttons[0][0]["callback_data"].split(":", 1)[1])
        self.assertEqual(len(group["options"]), 2)

    def test_schedule_preview_shows_mint_details_and_uses_short_callback_tokens(self):
        bot = TelegramBot(FakeAPI(), FakeTelegramService(), 123)
        candidate = dict(CANDIDATE, name="Scheduled Drop", price_display="Paid · 0.01 ETH")
        text = bot.render_schedule_candidate(candidate)
        keyboard = bot.schedule_candidate_keyboard(candidate)

        self.assertIn("Scheduled Drop", text)
        self.assertIn("Paid · 0.01 ETH", text)
        self.assertIn("Quantity", text)
        self.assertTrue(
            any(
                row[0]["callback_data"].startswith("schedule:live:")
                for row in keyboard["inline_keyboard"]
            )
        )
        callbacks = [
            button["callback_data"]
            for row in keyboard["inline_keyboard"]
            for button in row
            if "callback_data" in button
        ]
        self.assertFalse(any("dry" in callback for callback in callbacks))
        self.assertLessEqual(
            len(keyboard["inline_keyboard"][0][0]["callback_data"].encode("utf-8")),
            64,
        )

    def test_telegram_actions_are_live_only_and_show_total_mint_value(self):
        bot = TelegramBot(FakeAPI(), FakeTelegramService(), 123)
        paid = dict(
            CANDIDATE,
            price_wei=10**16,
            price_display="Paid · 0.01 ETH",
            is_free=False,
            quantity=2,
        )
        text = bot.render_schedule_candidate(paid)
        self.assertIn("Total mint value:</b> 0.02 ETH", text)
        keyboards = [
            bot.candidate_detail_keyboard(1, paid),
            bot.schedule_candidate_keyboard(paid),
            bot.daily_keyboard(),
        ]
        callbacks = [
            button.get("callback_data", "")
            for keyboard in keyboards
            for row in keyboard["inline_keyboard"]
            for button in row
        ]
        self.assertFalse(any("dry" in callback.lower() for callback in callbacks))
        self.assertNotIn("dry", bot.help_text().lower())

    def test_result_card_reports_measured_broadcast_delay(self):
        bot = TelegramBot(FakeAPI(), FakeTelegramService(), 123)
        text = bot.render_result({
            "candidate": CANDIDATE,
            "tx_hash": "0xabc",
            "confirmed": True,
            "launch_delay_ms": 187,
        })
        self.assertIn("Broadcast delay:</b> 187 ms", text)

    def test_final_refresh_refuses_a_price_change_after_confirmation(self):
        service = FakeTelegramService()
        service.last_candidates = [dict(CANDIDATE, price_wei=0)]
        bot = TelegramBot(FakeAPI(), service, 123)
        approved = dict(CANDIDATE, price_wei=1, price_display="Paid")
        with self.assertRaisesRegex(RuntimeError, "price changed"):
            bot._fresh_candidate(approved)

    def test_quantity_picker_has_presets_and_respects_stage_wallet_limit(self):
        bot = TelegramBot(FakeAPI(), FakeTelegramService(), 123)
        candidate = dict(CANDIDATE, max_per_wallet=3)
        self.assertEqual(quantity_limit(candidate), 3)
        self.assertEqual(validate_quantity(candidate, 3), 3)
        with self.assertRaisesRegex(ValueError, "between 1 and 3"):
            validate_quantity(candidate, 4)
        keyboard = bot.quantity_keyboard(candidate, "candidate", index=1)
        callbacks = [button["callback_data"] for row in keyboard["inline_keyboard"] for button in row]
        self.assertTrue(any(callback.startswith("quantity:set:candidate:1:") for callback in callbacks))
        self.assertTrue(any(callback.endswith(":3") for callback in callbacks))
        self.assertFalse(any(callback.endswith(":5") for callback in callbacks))

    def test_detail_cards_escape_description_and_embed_project_links(self):
        bot = TelegramBot(FakeAPI(), FakeTelegramService(), 123)
        candidate = dict(
            CANDIDATE,
            description="A & <carefully described> collection.",
            project_url="https://example.com/project",
            twitter_url="https://x.com/example",
            contract_address="0x0000000000000000000000000000000000000001",
            opensea_url="https://opensea.io/collection/demo-drop",
            total_supply=10,
            max_supply=100,
        )
        text = bot.render_schedule_candidate(candidate)
        self.assertIn("A &amp; &lt;carefully described&gt; collection.", text)
        self.assertIn('<a href="https://example.com/project">🌐 Website</a>', text)
        self.assertIn('<a href="https://x.com/example">𝕏 X</a>', text)
        self.assertIn('<a href="https://basescan.org/address/0x0000000000000000000000000000000000000001">🔗 Contract</a>', text)
        self.assertIn('<a href="https://opensea.io/collection/demo-drop">Demo &amp; Drop</a>', text)

    def test_research_report_embeds_social_and_wallet_links(self):
        bot = TelegramBot(FakeAPI(), FakeTelegramService(), 123)
        owner = "0x0000000000000000000000000000000000000001"
        research = {
            "name": "Demo Research",
            "slug": "demo-drop",
            "chain": "base",
            "opensea_url": "https://opensea.io/collection/demo-drop",
            "twitter_url": "https://x.com/demo",
            "owner": owner,
            "editors": [],
            "developer_note": "OpenSea attribution only; not verified developer identity.",
            "stats_total": {"volume": 2.5, "sales": 10, "num_owners": 7},
        }
        text = bot.render_research(research)

        self.assertIn('<a href="https://x.com/demo">𝕏</a>', text)
        self.assertIn("Attributed owner", text)
        self.assertIn(
            f'<a href="https://basescan.org/address/{owner}">',
            text,
        )
        self.assertIn(f'<a href="https://opensea.io/{owner}">OpenSea profile</a>', text)
        self.assertIn(
            f'<a href="https://intel.arkm.com/explorer/address/{owner}">Arkham</a>',
            text,
        )
        self.assertIn(f"<code>{owner}</code>", text)
        self.assertIn("not verified developer", text.lower())

    def test_wallet_attribution_shows_profile_name_and_full_address(self):
        bot = TelegramBot(FakeAPI(), FakeTelegramService(), 123)
        owner = "0x32d4e1e8b75754e1ff391577836c98f38d3f577b"
        text = bot.render_research({
            "name": "Thinking Catss",
            "chain": "robinhood",
            "owner": owner,
            "owner_profiles": {
                owner.upper(): {
                    "username": "Thinkingcats_dev",
                    "display_name": "Thinkingcats_dev",
                },
            },
        })

        self.assertIn("<b>Attributed owner:</b>", text)
        self.assertIn("Thinkingcats_dev", text)
        self.assertIn(f"<code>{owner}</code>", text)
        self.assertIn(f'href="https://opensea.io/{owner}"', text)
        self.assertIn(f'href="https://intel.arkm.com/explorer/address/{owner}"', text)
        self.assertNotIn("0x32d4e1e8b75754e…", text)

    def test_research_report_hides_empty_metrics_and_duplicate_editor(self):
        bot = TelegramBot(FakeAPI(), FakeTelegramService(), 123)
        owner = "0x0000000000000000000000000000000000000001"
        text = bot.render_research({
            "name": "Clean Report",
            "chain": "base",
            "opensea_url": "https://opensea.io/collection/clean-report",
            "category": "",
            "safelist_status": "not_requested",
            "stats_total": {
                "floor_price": 0.0,
                "volume": 0.0,
                "sales": 0,
                "num_owners": 1,
            },
            "stats_one_day": {"volume": 0.0, "sales": 0},
            "owner": owner,
            "editors": [owner, owner.upper()],
        })

        self.assertNotIn("Category:", text)
        self.assertNotIn("Safelist", text)
        self.assertNotIn("Floor:", text)
        self.assertNotIn("24h", text)
        self.assertNotIn("OpenSea editor", text)
        self.assertEqual(text.count("Attributed owner"), 1)

    def test_research_report_formats_currency_counts_and_recent_nfts(self):
        bot = TelegramBot(FakeAPI(), FakeTelegramService(), 123)
        text = bot.render_research({
            "name": "Thinking Catss",
            "chain": "robinhood",
            "total_supply": 10000,
            "stats_currency": "ETH",
            "stats_total": {
                "floor_price": 0.001899999997,
                "floor_price_symbol": "ETH",
                "volume": 7.02734730124206,
                "sales": 16480,
                "num_owners": 1390,
            },
            "stats_one_day": {"volume": 7.027347301242118, "sales": 16480},
            "sample_nfts": [{
                "name": "hmmmm",
                "identifier": "9998",
                "opensea_url": "https://opensea.io/assets/robinhood/contract/9998",
            }],
        })

        self.assertIn("<b>Supply:</b> 10,000", text)
        self.assertIn("<b>Floor:</b> 0.0019 ETH", text)
        self.assertIn("<b>All-time volume:</b> 7.03 ETH", text)
        self.assertIn("<b>All-time sales:</b> 16,480", text)
        self.assertIn("<b>Owners:</b> 1,390", text)
        self.assertIn("<b>24h volume:</b> 7.03 ETH", text)
        self.assertIn("hmmmm #9998", text)
        self.assertNotIn("𝕏 X", text)

    def test_candidate_menu_has_info_and_image_actions_with_safe_callbacks(self):
        bot = TelegramBot(FakeAPI(), FakeTelegramService(), 123)
        keyboard = bot.candidate_detail_keyboard(1, CANDIDATE)
        callbacks = [
            button["callback_data"]
            for row in keyboard["inline_keyboard"]
            for button in row
            if "callback_data" in button
        ]
        self.assertTrue(any(value.startswith("info:candidate:1:") for value in callbacks))
        self.assertTrue(any(value.startswith("card:candidate:1:") for value in callbacks))
        self.assertTrue(all(len(value.encode("utf-8")) <= 64 for value in callbacks))

    def test_mint_card_generates_a_telegram_ready_jpeg(self):
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            os.environ,
            {"NFT_CARD_BACKGROUND": "", "NFT_CARD_BRAND_NAME": "Test Bot"},
        ):
            path = nft_card.build_mint_card(
                dict(CANDIDATE, quantity=2),
                {"stats_total": {"num_owners": 11}},
                output_dir=temp_dir,
            )
            self.assertTrue(path.is_file())
            self.assertEqual(path.suffix, ".jpg")
            from PIL import Image
            with Image.open(path) as image:
                self.assertEqual(image.size, (1200, 675))

    def test_mint_card_background_can_be_installed_and_reset(self):
        from PIL import Image

        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "source.png"
            target = Path(temp_dir) / "state" / "nft-card-background.jpg"
            Image.new("RGB", (500, 500), "purple").save(source)
            with patch.object(nft_card, "PERSISTENT_BACKGROUND", target):
                installed = nft_card.install_card_background(source)
                self.assertEqual(installed, target)
                with Image.open(installed) as image:
                    self.assertEqual(image.size, (1200, 675))
                self.assertTrue(nft_card.clear_card_background())
                self.assertFalse(target.exists())


class DailyRunnerSafetyTests(unittest.TestCase):
    def test_concurrent_attempts_cannot_execute_the_same_candidate_twice(self):
        original_root = daily_runner.ROOT
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                daily_runner.ROOT = Path(temp_dir)
                service = DailyMintService(
                    "alchemy",
                    "private",
                    "0x0000000000000000000000000000000000000001",
                    "opensea",
                )
                service.engine = FakeEngine()
                outcomes = []

                def attempt():
                    try:
                        with patch.dict(os.environ, {"ENABLE_LIVE_MINTS": "true"}):
                            outcomes.append(service.mint_candidate(CANDIDATE)["status"])
                    except Exception as exc:  # one caller should see the duplicate guard
                        outcomes.append(type(exc).__name__)

                first = threading.Thread(target=attempt)
                second = threading.Thread(target=attempt)
                first.start()
                second.start()
                first.join()
                second.join()

                self.assertEqual(sorted(outcomes), ["RuntimeError", "sent"])
                self.assertEqual(service.engine.calls, 1)
                service.shutdown()
        finally:
            daily_runner.ROOT = original_root

    def test_one_time_schedule_persists_and_can_be_cancelled(self):
        original_root = daily_runner.ROOT
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                daily_runner.ROOT = Path(temp_dir)
                service = DailyMintService(
                    "alchemy",
                    "private",
                    "0x0000000000000000000000000000000000000001",
                    "opensea",
                )
                candidate = dict(
                    CANDIDATE,
                    slug="scheduled-drop",
                    start_time=int(time.time()) + 3600,
                    end_time=int(time.time()) + 7200,
                )
                with patch.dict(os.environ, {"ENABLE_LIVE_MINTS": "true"}):
                    schedule = service.add_schedule(candidate)
                self.assertEqual(schedule["status"], "armed")
                self.assertEqual(service.schedules()[0]["id"], schedule["id"])
                service.shutdown()

                restored = DailyMintService(
                    "alchemy",
                    "private",
                    "0x0000000000000000000000000000000000000001",
                    "opensea",
                )
                self.assertEqual(restored.schedules()[0]["status"], "armed")
                cancelled = restored.cancel_schedule(schedule["id"])
                self.assertEqual(cancelled["status"], "cancelled")
                self.assertEqual(restored.schedules()[0]["status"], "cancelled")
                restored.shutdown()
        finally:
            daily_runner.ROOT = original_root

    def test_live_schedule_rejects_a_known_price_above_the_configured_cap(self):
        original_root = daily_runner.ROOT
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                daily_runner.ROOT = Path(temp_dir)
                service = DailyMintService(
                    "alchemy",
                    "private",
                    "0x0000000000000000000000000000000000000001",
                    "opensea",
                )
                paid = dict(
                    CANDIDATE,
                    price_wei=1,
                    price_display="Paid · 0.000000000000000001 ETH",
                    is_free=False,
                )
                with patch.dict(os.environ, {"ENABLE_LIVE_MINTS": "true"}):
                    with self.assertRaisesRegex(RuntimeError, "exceeds"):
                        service.add_schedule(paid)
                self.assertEqual(service.schedules(), [])
                service.shutdown()
        finally:
            daily_runner.ROOT = original_root


class DiscoverySafetyTests(unittest.TestCase):
    def test_high_resolution_wait_does_not_fire_early(self):
        from mint_engine import MintEngine

        target = time.time() + 0.05
        MintEngine._wait_until(target)
        self.assertGreaterEqual(time.time(), target)

    def test_fast_retry_profile_is_bounded_and_front_loaded(self):
        self.assertEqual(config.FIRE_MAX_ATTEMPTS, 10)
        self.assertLessEqual(config.FIRE_RETRY_DELAYS_SECONDS[0], 0.20)
        self.assertLess(sum(config.FIRE_RETRY_DELAYS_SECONDS[:4]), 2.0)
        self.assertLessEqual(
            sum(config.FIRE_RETRY_DELAYS_SECONDS[: config.FIRE_MAX_ATTEMPTS - 1]),
            config.FIRE_TIMEOUT_SECONDS,
        )

    def test_live_engine_approves_exact_total_mint_value(self):
        engine = daily_runner.MintEngine("alchemy", "private", "wallet", "opensea")
        paid = dict(CANDIDATE, price_wei=10, quantity=3)
        self.assertEqual(engine._approved_value(paid, 3), 30)
        with self.assertRaisesRegex(RuntimeError, "unknown"):
            engine._approved_value(dict(CANDIDATE, price_wei=None), 1)

    def test_minter_rejects_opensea_value_that_differs_from_confirmation(self):
        minter = object.__new__(Minter)
        with patch.object(config, "MAX_MINT_VALUE_WEI", 10):
            with self.assertRaisesRegex(RuntimeError, "different from the amount approved"):
                minter.build_transaction(
                    "0x0000000000000000000000000000000000000002",
                    "0x1234",
                    value=2,
                    approved_value_wei=1,
                )

    def test_selected_quantity_is_sent_to_opensea_mint_endpoint(self):
        client = FakeMintClient()
        calldata, error = opensea_client.get_mint_calldata(
            client,
            "demo-drop",
            stage_index=0,
            quantity=7,
            address="0x0000000000000000000000000000000000000001",
            api_key="test-opensea-key",
        )
        self.assertIsNone(error)
        self.assertEqual(calldata["data"], "0x1234")
        self.assertEqual(client.payload["json"], {
            "minter": "0x0000000000000000000000000000000000000001",
            "quantity": 7,
        })

    def test_drop_url_parser_rejects_asset_urls_and_accepts_collection_urls(self):
        self.assertEqual(
            discovery.opensea_client.parse_drop_slug(
                "https://opensea.io/collection/example?ref=bot"
            ),
            "example",
        )
        with self.assertRaisesRegex(ValueError, "individual NFT asset"):
            discovery.opensea_client.parse_drop_slug(
                "https://opensea.io/assets/ethereum/0xabc/1"
            )

    def test_research_parser_accepts_collection_and_valid_asset_urls(self):
        collection = opensea_client.parse_opensea_reference(
            "https://opensea.io/collection/example?ref=bot"
        )
        asset = opensea_client.parse_opensea_reference(
            "https://opensea.io/assets/base/0x0000000000000000000000000000000000000001/42"
        )
        self.assertEqual(collection, {"kind": "collection", "slug": "example"})
        self.assertEqual(asset["kind"], "asset")
        self.assertEqual(asset["chain"], "base")
        self.assertEqual(asset["identifier"], "42")

    def test_stage_type_can_mark_a_neutral_label_as_restricted(self):
        stages = discovery.build_drop_candidates(
            "holder-drop",
            "Holder Drop",
            "base",
            [{
                "stageIndex": 0,
                "startTime": int(time.time()) + 60,
                "endTime": int(time.time()) + 3600,
                "label": "Public",
                "stageType": "holder",
                "price": "0",
            }],
        )
        self.assertEqual(len(stages), 1)
        self.assertFalse(stages[0].is_public)

    def test_gated_stage_labels_are_not_marked_public(self):
        for label in ("Allowlist", "OH NFT + PFP Holders", "Team Mint", "Private"):
            self.assertFalse(discovery._is_public_label(label), label)
        for label in ("Public", "Public stage", "Newly announced"):
            self.assertTrue(discovery._is_public_label(label), label)

    def test_day_bounds_follow_the_configured_offset(self):
        timestamp = datetime(2026, 1, 1, 23, 30, tzinfo=timezone.utc).timestamp()
        with patch.dict(os.environ, {"DISCOVERY_UTC_OFFSET_HOURS": "1"}):
            start, end, label = config.discovery_day_bounds(timestamp)

        self.assertEqual(label, "2026-01-02")
        self.assertEqual(end - start, 86399)

    def test_discovery_keeps_only_zero_price_public_stages_with_valid_times(self):
        now = int(time.time())
        cards = [
            {"collectionSlug": "public-drop", "collectionName": "Public Drop"},
            {"collectionSlug": "allowlist-drop", "collectionName": "Allowlist Drop"},
            {"collectionSlug": "paid-drop", "collectionName": "Paid Drop"},
            {"collectionSlug": "unknown-time", "collectionName": "Unknown Time"},
        ]

        schedules = {
            "public-drop": ("Public Drop", [{
                "stageIndex": 0,
                "startTime": now + 60,
                "endTime": now + 3600,
                "label": "Public",
                "price": {"amount": "0"},
            }]),
            "allowlist-drop": ("Allowlist Drop", [{
                "stageIndex": 0,
                "startTime": now + 60,
                "endTime": now + 3600,
                "label": "Allowlist",
                "price": "0",
            }]),
            "paid-drop": ("Paid Drop", [{
                "stageIndex": 0,
                "startTime": now + 60,
                "endTime": now + 3600,
                "label": "Public",
                "price": "1000000000000000",
            }]),
            "unknown-time": ("Unknown Time", [{
                "stageIndex": 0,
                "startTime": 0,
                "endTime": None,
                "label": "Public",
                "price": "0",
            }]),
        }

        with patch.object(
            discovery.opensea_client,
            "list_drops",
            return_value=(cards, None),
        ), patch.object(
            discovery.opensea_client,
            "get_drop_schedule",
            side_effect=lambda client, slug, api_key: schedules[slug],
        ), patch.object(config, "DISCOVERY_REQUEST_DELAY_SECONDS", 0), patch.object(
            config, "DISCOVERY_MAX_PAGES_PER_CHAIN", 1
        ):
            candidates, errors = discovery.discover_free_mints(object(), "key", ["base"], 24)

        self.assertEqual(errors, [])
        self.assertEqual([item.slug for item in candidates], ["public-drop"])

    def test_all_stage_discovery_keeps_paid_and_restricted_entries_for_display(self):
        now = int(time.time())
        cards = [
            {"collectionSlug": "public-drop", "collectionName": "Public Drop"},
            {"collectionSlug": "allowlist-drop", "collectionName": "Allowlist Drop"},
            {"collectionSlug": "paid-drop", "collectionName": "Paid Drop"},
        ]
        schedules = {
            "public-drop": ("Public Drop", [{
                "stageIndex": 0, "startTime": now + 60, "endTime": now + 3600,
                "label": "Public", "price": "0",
            }]),
            "allowlist-drop": ("Allowlist Drop", [{
                "stageIndex": 0, "startTime": now + 60, "endTime": now + 3600,
                "label": "Allowlist", "price": "0",
            }]),
            "paid-drop": ("Paid Drop", [{
                "stageIndex": 0, "startTime": now + 60, "endTime": now + 3600,
                "label": "Public", "price": "1000000000000000",
            }]),
        }

        with patch.object(
            discovery.opensea_client,
            "list_drops",
            return_value=(cards, None),
        ), patch.object(
            discovery.opensea_client,
            "get_drop_schedule",
            side_effect=lambda client, slug, api_key: schedules[slug],
        ), patch.object(config, "DISCOVERY_REQUEST_DELAY_SECONDS", 0), patch.object(
            config, "DISCOVERY_MAX_PAGES_PER_CHAIN", 1
        ):
            candidates, errors = discovery.discover_mints(
                object(), "key", ["base"], 24, today_only=True
            )

        self.assertEqual(errors, [])
        self.assertEqual([item.slug for item in candidates], [
            "allowlist-drop", "paid-drop", "public-drop"
        ])
        by_slug = {item.slug: item for item in candidates}
        self.assertTrue(by_slug["public-drop"].is_free)
        self.assertFalse(by_slug["allowlist-drop"].is_public)
        self.assertFalse(by_slug["paid-drop"].is_free)

    def test_discovery_merges_all_calendar_feeds_without_duplicate_stages(self):
        now = int(time.time())
        cards_by_type = {
            "upcoming": [
                {"collectionSlug": "shared", "collectionName": "Shared"},
                {"collectionSlug": "upcoming-only", "collectionName": "Upcoming"},
            ],
            "recently_minted": [
                {"collectionSlug": "shared", "collectionName": "Shared"},
                {"collectionSlug": "recent-only", "collectionName": "Recent"},
            ],
            "featured": [
                {"collectionSlug": "featured-only", "collectionName": "Featured"},
            ],
        }
        schedules = {
            slug: (slug, [{
                "stageIndex": 0,
                "startTime": now + 60,
                "endTime": now + 3600,
                "label": "Public",
                "price": "0",
            }])
            for slug in {"shared", "upcoming-only", "recent-only", "featured-only"}
        }

        def list_feed(client, api_key, chain, drop_type, limit, cursor):
            return cards_by_type[drop_type], None

        with patch.object(
            discovery.opensea_client, "list_drops", side_effect=list_feed
        ), patch.object(
            discovery.opensea_client,
            "get_drop_schedule",
            side_effect=lambda client, slug, api_key: schedules[slug],
        ), patch.object(config, "DISCOVERY_REQUEST_DELAY_SECONDS", 0), patch.object(
            config, "DISCOVERY_MAX_PAGES_PER_CHAIN", 1
        ):
            candidates, errors = discovery.discover_mints(
                object(), "key", ["base"], 24, today_only=False
            )

        self.assertEqual(errors, [])
        self.assertEqual(
            sorted(item.slug for item in candidates),
            ["featured-only", "recent-only", "shared", "upcoming-only"],
        )

    def test_today_scan_excludes_an_active_stage_that_started_yesterday(self):
        day_start, _, _ = config.discovery_day_bounds()
        cards = [{"collectionSlug": "yesterday", "collectionName": "Yesterday"}]
        stages = [{
            "stageIndex": 0,
            "startTime": day_start - 60,
            "endTime": int(time.time()) + 3600,
            "label": "Public",
            "price": "0",
        }]
        with patch.object(
            discovery.opensea_client, "list_drops", return_value=(cards, None)
        ), patch.object(
            discovery.opensea_client,
            "get_drop_schedule",
            return_value=("Yesterday", stages),
        ), patch.object(config, "DISCOVERY_REQUEST_DELAY_SECONDS", 0), patch.object(
            config, "DISCOVERY_MAX_PAGES_PER_CHAIN", 1
        ):
            candidates, errors = discovery.discover_mints(
                object(), "key", ["base"], 24, today_only=True
            )

        self.assertEqual(errors, [])
        self.assertEqual(candidates, [])

    def test_chain_scan_finds_today_drop_omitted_from_calendar(self):
        day_start, _, _ = config.discovery_day_bounds()
        ranked = [{
            "collection": "thinking-catss",
            "name": "Thinking Catss",
            "description": "10,000 thinking cats",
            "opensea_url": "https://opensea.io/collection/thinking-catss",
        }]
        drop = {
            "slug": "thinking-catss",
            "name": "Thinking Catss",
            "chain": "robinhood",
            "contract_address": "0x65d8b5d6a86a24ce21ac09af95bed55fd8b76995",
            "opensea_url": "https://opensea.io/collection/thinking-catss",
            "stages": [{
                "stageIndex": 0,
                "startTime": day_start + 60,
                "endTime": day_start + 80000,
                "label": "Public stage",
                "stageType": "public_sale",
                "price": "100000000000000",
            }],
        }
        with patch.object(
            discovery.opensea_client, "list_drops", return_value=([], None)
        ), patch.object(
            discovery.opensea_client, "list_top_collections", return_value=ranked
        ), patch.object(
            discovery.opensea_client, "get_drop_info", return_value=drop
        ), patch.object(config, "DISCOVERY_REQUEST_DELAY_SECONDS", 0), patch.object(
            config, "DISCOVERY_MAX_PAGES_PER_CHAIN", 1
        ), patch.object(config, "DISCOVERY_RANKED_FALLBACK_WORKERS", 1):
            candidates, errors = discovery.discover_mints(
                object(),
                "key",
                ["robinhood"],
                24,
                today_only=True,
                include_ranked_fallback=True,
            )

        self.assertEqual(errors, [])
        self.assertEqual([item.slug for item in candidates], ["thinking-catss"])
        self.assertEqual(candidates[0].price_display, "Paid · 0.0001 ETH")

    def test_ranked_secondary_collection_without_drop_is_silently_ignored(self):
        ranked = [{"collection": "secondary-only", "name": "Secondary Only"}]
        with patch.object(
            discovery.opensea_client, "list_drops", return_value=([], None)
        ), patch.object(
            discovery.opensea_client, "list_top_collections", return_value=ranked
        ), patch.object(
            discovery.opensea_client,
            "get_drop_info",
            side_effect=RuntimeError("OpenSea API request failed (HTTP 404): Drop not found"),
        ), patch.object(config, "DISCOVERY_MAX_PAGES_PER_CHAIN", 1), patch.object(
            config, "DISCOVERY_RANKED_FALLBACK_WORKERS", 1
        ):
            candidates, errors = discovery.discover_mints(
                object(),
                "key",
                ["robinhood"],
                24,
                today_only=True,
                include_ranked_fallback=True,
            )

        self.assertEqual(candidates, [])
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
