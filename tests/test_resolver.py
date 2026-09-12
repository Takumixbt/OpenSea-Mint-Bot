import socket
import unittest
from unittest.mock import patch

import resolver


class SecureDnsTests(unittest.TestCase):
    def setUp(self):
        resolver._cache.clear()

    def test_resolve_uses_doh_and_caches(self):
        with patch.object(resolver, "_doh_a_record", return_value=("1.2.3.4", 60)) as probe:
            self.assertEqual(resolver.resolve("eth-mainnet.g.alchemy.com"), "1.2.3.4")
            self.assertEqual(resolver.resolve("eth-mainnet.g.alchemy.com"), "1.2.3.4")
        self.assertEqual(probe.call_count, 1)

    def test_ip_literals_are_not_looked_up(self):
        with patch.object(resolver, "_doh_a_record") as probe:
            self.assertEqual(resolver.resolve("1.1.1.1"), "1.1.1.1")
        probe.assert_not_called()

    def test_install_routes_getaddrinfo_through_doh(self):
        original = socket.getaddrinfo
        previous_flag = resolver._installed
        previous_orig = resolver._orig_getaddrinfo
        try:
            if previous_orig is not None:
                socket.getaddrinfo = previous_orig
            resolver._installed = False
            resolver._orig_getaddrinfo = None
            with patch.object(resolver, "_doh_a_record", return_value=("8.8.8.8", 60)):
                resolver.install()
                result = socket.getaddrinfo("eth-mainnet.g.alchemy.com", 443, socket.AF_INET, socket.SOCK_STREAM)
            self.assertTrue(result)
            self.assertEqual(result[0][4][0], "8.8.8.8")
        finally:
            socket.getaddrinfo = original
            resolver._installed = previous_flag
            resolver._orig_getaddrinfo = previous_orig
