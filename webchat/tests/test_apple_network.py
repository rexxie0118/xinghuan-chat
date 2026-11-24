"""Tests for Apple Container network policy mapping."""
from __future__ import annotations

import unittest

from webchat.sandbox.apple_network import INTERNAL_NETWORK, network_policy


class AppleNetworkPolicyTest(unittest.TestCase):
    def test_none_and_isolated_map_to_internal_network(self):
        self.assertEqual(network_policy("none"), (INTERNAL_NETWORK, True))
        self.assertEqual(network_policy("isolated"), (INTERNAL_NETWORK, True))
        self.assertEqual(network_policy("NONE"), (INTERNAL_NETWORK, True))

    def test_default_and_custom_network(self):
        self.assertEqual(network_policy("default"), (None, False))
        self.assertEqual(network_policy(""), (None, False))
        self.assertEqual(network_policy("team-net"), ("team-net", False))


if __name__ == "__main__":
    unittest.main()
