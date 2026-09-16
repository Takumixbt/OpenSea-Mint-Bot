import unittest

import config
from cli import wei_to_native


class ArcChainTests(unittest.TestCase):
    def test_resolve_chain_slug(self):
        self.assertEqual(config.resolve_chain_slug("arc"), "arc")
        self.assertEqual(config.resolve_chain_slug("Arc"), "arc")
        self.assertEqual(config.resolve_chain_slug("arc_chain"), "arc")

    def test_chain_slug_for_id(self):
        self.assertEqual(config.chain_slug_for_id(5042), "arc")

    def test_arc_config_uses_usdc_native_gas(self):
        settings = config.chain_config("arc")
        self.assertIsNotNone(settings)
        self.assertEqual(settings["chain_id"], 5042)
        self.assertEqual(settings["native"], "USDC")
        self.assertEqual(settings["explorer"], "https://explorer.arc.io")
        self.assertIn("rpc.mainnet.arc.io", settings["rpc_url"])

    def test_chain_native_symbol(self):
        self.assertEqual(config.chain_native_symbol("arc"), "USDC")
        self.assertEqual(config.chain_native_symbol("base"), "ETH")
        self.assertEqual(config.chain_native_symbol("unknown"), "native coin")

    def test_is_usdc_native_chain(self):
        self.assertTrue(config.is_usdc_native_chain("arc"))
        self.assertFalse(config.is_usdc_native_chain("ethereum"))
        self.assertFalse(config.is_usdc_native_chain("base"))

    def test_wei_to_native_labels_usdc_on_arc(self):
        one_usdc = 10 ** 18
        self.assertEqual(
            wei_to_native(one_usdc, config.chain_native_symbol("arc")),
            "1 USDC",
        )


if __name__ == "__main__":
    unittest.main()
