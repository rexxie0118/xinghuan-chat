"""MCP registry tests using a local stdio fake server."""
from __future__ import annotations

import json
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from webchat.tools import ToolError, ToolRegistry


class ToolRegistryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fake = Path(__file__).with_name("fake_mcp_server.py")
        self.config = {
            "servers": {
                "fake": {
                    "transport": "stdio",
                    "command": sys.executable,
                    "args": [str(self.fake)],
                    "enabled": True,
                    "allow_tools": ["echo"],
                    "timeout": 5,
                }
            }
        }
        self.registry = ToolRegistry(self.config).discover()

    def tearDown(self) -> None:
        self.registry.close()

    def test_discovers_only_allowlisted_tools(self):
        names = [tool.name for tool in self.registry.list_tools()]
        self.assertEqual(names, ["fake.echo"])
        summary = self.registry.summary()
        self.assertTrue(summary["enabled"])
        self.assertEqual(summary["servers"][0]["tools"], 2)
        self.assertEqual(summary["tools"][0]["input_schema"]["required"], ["text"])

    def test_calls_stdio_tool(self):
        result = self.registry.call("fake.echo", {"text": "hello mcp"})
        self.assertTrue(result.ok)
        self.assertEqual(result.content, "hello mcp")
        self.assertEqual(result.server, "fake")

    def test_rejects_unknown_tool_and_marks_tool_error(self):
        with self.assertRaises(ToolError):
            self.registry.call("fake.missing", {})
        # The server has a fail tool but it is not allowlisted.
        with self.assertRaises(ToolError):
            self.registry.call("fake.fail", {})

    def test_http_mcp_server(self):
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("content-length") or 0)
                req = json.loads(self.rfile.read(length) or b"{}")
                method = req.get("method")
                req_id = req.get("id")
                if method == "initialize":
                    result = {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "http-fake", "version": "1.0"},
                    }
                elif method == "tools/list":
                    result = {"tools": [{
                        "name": "ping",
                        "description": "Ping over HTTP",
                        "inputSchema": {"type": "object", "properties": {}},
                    }]}
                elif method == "tools/call":
                    result = {"content": [{"type": "text", "text": "pong"}], "isError": False}
                elif req_id is None:
                    self.send_response(202)
                    self.end_headers()
                    return
                else:
                    result = {}
                body = json.dumps({"jsonrpc": "2.0", "id": req_id, "result": result}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        registry = ToolRegistry({
            "servers": {
                "remote": {
                    "transport": "http",
                    "url": f"http://127.0.0.1:{server.server_port}/mcp",
                    "allow_tools": ["ping"],
                    "timeout": 5,
                }
            }
        }).discover()
        try:
            self.assertEqual([tool.name for tool in registry.list_tools()], ["remote.ping"])
            result = registry.call("remote.ping", {})
            self.assertTrue(result.ok)
            self.assertEqual(result.content, "pong")
        finally:
            registry.close()
            server.shutdown()
            server.server_close()

    def test_disabled_server_is_reported(self):
        registry = ToolRegistry(
            {"servers": {"off": {"enabled": False, "transport": "stdio", "command": "no"}}}
        ).discover()
        try:
            summary = registry.summary()
            self.assertFalse(summary["enabled"])
            self.assertEqual(summary["servers"][0]["name"], "off")
            self.assertEqual(registry.list_tools(), [])
        finally:
            registry.close()


if __name__ == "__main__":
    unittest.main()
