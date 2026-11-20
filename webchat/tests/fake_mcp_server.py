"""Tiny stdio MCP server used by the registry tests."""
from __future__ import annotations

import json
import sys


def emit(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False), flush=True)


TOOLS = [
    {
        "name": "echo",
        "description": "Echo text back",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    },
    {
        "name": "fail",
        "description": "Return an MCP tool error",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        req = json.loads(line)
    except json.JSONDecodeError:
        continue
    method = req.get("method")
    req_id = req.get("id")
    if method == "initialize":
        emit({"jsonrpc": "2.0", "id": req_id, "result": {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "fake", "version": "1.0"},
        }})
    elif method == "tools/list":
        emit({"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOLS}})
    elif method == "tools/call":
        params = req.get("params") or {}
        name = params.get("name")
        args = params.get("arguments") or {}
        if name == "echo":
            emit({"jsonrpc": "2.0", "id": req_id, "result": {
                "content": [{"type": "text", "text": str(args.get("text", ""))}],
                "isError": False,
            }})
        elif name == "fail":
            emit({"jsonrpc": "2.0", "id": req_id, "result": {
                "content": [{"type": "text", "text": "intentional tool error"}],
                "isError": True,
            }})
        else:
            emit({"jsonrpc": "2.0", "id": req_id, "error": {
                "code": -32601, "message": f"unknown tool {name}",
            }})
    elif method == "notifications/initialized":
        continue
    elif req_id is not None:
        emit({"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": method}})
