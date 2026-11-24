"""Tests for the trusted plugin manifest/loader."""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from webchat.runtime import AgentKernel
from webchat.runtime.plugins import PluginManifest, load_plugin_host
from webchat.tools import ToolRegistry


class FakeRunner:
    def attach_hooks(self, hooks, context):
        self.hooks = hooks
        self.context = context

    def run(self, **_kwargs):
        raise AssertionError("not used")


class PluginHostTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="plugin-test-"))
        self.config = self.tmp / "plugins.json"
        self.settings = SimpleNamespace(
            root=self.tmp,
            plugins_enabled=True,
            plugins_config=self.config,
            plugins_allow_external=False,
        )
        self.kernel = AgentKernel(self.settings, FakeRunner())
        self.tools = ToolRegistry({})

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, plugins):
        self.config.write_text(json.dumps({"plugins": plugins}), encoding="utf-8")

    def test_loads_trusted_plugin_and_registers_tool(self):
        self.write([
            {
                "name": "audit",
                "version": "1.0.0",
                "entrypoint": "webchat.plugins.audit:plugin",
                "trusted": True,
                "permissions": ["hooks", "tools"],
                "hooks": ["agent_end"],
            }
        ])
        host = load_plugin_host(self.settings, self.kernel, self.tools)
        self.assertEqual(host.statuses[0].status, "loaded")
        result = self.tools.call("plugin.audit.ping", {})
        self.assertTrue(result.ok)
        self.assertEqual(result.content, "pong")
        self.assertIn("plugin.audit.ping", [t.name for t in self.tools.list_tools()])

    def test_untrusted_plugin_is_rejected(self):
        self.write([
            {
                "name": "bad",
                "version": "1.0.0",
                "entrypoint": "webchat.plugins.audit:plugin",
                "trusted": False,
                "permissions": ["hooks"],
            }
        ])
        host = load_plugin_host(self.settings, self.kernel, self.tools)
        self.assertEqual(host.statuses[0].status, "error")
        self.assertIn("trusted", host.statuses[0].error)
        self.assertEqual(self.tools.list_tools(), [])

    def test_tool_allowlist_is_enforced(self):
        self.write([
            {
                "name": "audit",
                "version": "1.0.0",
                "entrypoint": "webchat.plugins.audit:plugin",
                "trust": "trusted",
                "permissions": ["hooks", "tools"],
                "hooks": ["agent_end"],
                "tool_allowlist": ["other"],
            }
        ])
        host = load_plugin_host(self.settings, self.kernel, self.tools)
        self.assertEqual(host.statuses[0].status, "error")
        self.assertIn("未允许工具", host.statuses[0].error)

    def test_secret_permission_is_required(self):
        with self.assertRaises(ValueError):
            PluginManifest.from_dict(
                {
                    "name": "x",
                    "version": "1.0.0",
                    "entrypoint": "webchat.plugins.audit:plugin",
                    "trust": "trusted",
                    "permissions": ["tools"],
                    "secrets": ["TOKEN"],
                }
            )

    def test_disabled_plugin_is_not_loaded(self):
        self.write([
            {
                "name": "audit",
                "version": "1.0.0",
                "entrypoint": "webchat.plugins.audit:plugin",
                "trusted": True,
                "enabled": False,
                "permissions": ["hooks", "tools"],
            }
        ])
        host = load_plugin_host(self.settings, self.kernel, self.tools)
        self.assertEqual(host.statuses[0].status, "disabled")
        self.assertEqual(self.tools.list_tools(), [])


if __name__ == "__main__":
    unittest.main()
