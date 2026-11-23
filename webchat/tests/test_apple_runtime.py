"""Unit tests for persistent Apple Container runtime naming/lifecycle contracts."""
from __future__ import annotations

import unittest
from types import SimpleNamespace

from webchat.sandbox.apple_runtime import AppleContainerRuntime, RuntimeInfo


class AppleRuntimeTest(unittest.TestCase):
    def setUp(self):
        self.runtime = AppleContainerRuntime(
            SimpleNamespace(
                apple_container_bin="container",
                apple_container_image="python:3.12-slim",
                apple_container_cpus=2,
                apple_container_memory="2G",
                apple_container_network="default",
            )
        )

    def test_names_are_deterministic_and_key_scoped(self):
        a = self.runtime.names_for("slack:T1:C1:thread-1")
        b = self.runtime.names_for("slack:T1:C1:thread-1")
        c = self.runtime.names_for("slack:T1:C1:thread-2")
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)
        self.assertTrue(a[0].startswith("xh-rt-"))
        self.assertTrue(a[1].startswith("xh-vol-"))
        self.assertLessEqual(len(a[0]), 63)
        self.assertLessEqual(len(a[1]), 63)

    def test_runtime_info_public_shape(self):
        info = RuntimeInfo("key", "name", "volume", "image", "running", "id")
        self.assertEqual(info.public()["state"], "running")
        self.assertEqual(info.public()["container_id"], "id")


if __name__ == "__main__":
    unittest.main()
