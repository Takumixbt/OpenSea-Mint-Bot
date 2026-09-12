import unittest
from pathlib import Path

import config
from chain_assets import ASSET_DIR, chain_logo, logo_path
from chain_picker_card import picker_rows


class AccessTagTests(unittest.TestCase):
    def test_free_public_sold_and_gated(self):
        self.assertEqual(config.access_tag({"is_free": True, "is_public": True}), "FREE PUB")
        self.assertEqual(config.access_tag({"is_free": True, "is_public": False}), "FREE GATE")
        self.assertEqual(config.access_tag({"is_free": False, "is_public": True, "price_wei": 1}), "PAID PUB")
        self.assertEqual(config.access_tag({"is_sold_out": True}), "SOLD")
        self.assertEqual(config.access_tag({}), "GATED")


class LogoTests(unittest.TestCase):
    def test_every_configured_chain_has_a_logo_file(self):
        missing = [
            slug for slug in config.CHAIN_CONFIGS
            if logo_path(slug) is None
        ]
        self.assertEqual(missing, [], f"missing logos: {missing}")

    def test_base_logo_is_not_a_solid_rgb_square(self):
        logo = chain_logo("base", 52)
        self.assertIsNotNone(logo)
        self.assertEqual(logo.mode, "RGBA")
        # Official Base mark is a blue circle on a transparent field, not a
        # flattened blue tile that fills the whole square.
        corners = [
            logo.getpixel((0, 0))[3],
            logo.getpixel((51, 0))[3],
            logo.getpixel((0, 51))[3],
            logo.getpixel((51, 51))[3],
        ]
        self.assertTrue(min(corners) < 40)

    def test_picker_rows_are_busiest_first(self):
        rows = picker_rows({"base": 2, "ethereum": 7, "zora": 0})
        self.assertEqual(rows, [("ethereum", 7), ("base", 2)])


class AssetDirTests(unittest.TestCase):
    def test_sources_file_exists(self):
        self.assertTrue((ASSET_DIR / "SOURCES.md").is_file())
        self.assertTrue((Path(__file__).resolve().parents[1] / "scripts" / "fetch_chain_logos.py").is_file())


if __name__ == "__main__":
    unittest.main()
