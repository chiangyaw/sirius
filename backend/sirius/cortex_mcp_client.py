"""
Sirius as an MCP *client* for the external cortex-mcp server.

Wires the community cortex-mcp server (https://github.com/chiangyaw/cortex-mcp)
into Sirius so an agent (Defender) can drive a Cortex tenant over the real Model
Context Protocol. On init() we launch the server over stdio, discover its tools via
the protocol, and register each as a native Sirius skill named `cortexmcp_<tool>` —
so the tools flow through the normal agent loop (isolation guard + event stream)
while every call is executed over MCP against the live server.

Each call spawns the server over stdio, runs one tool call, and tears it down —
simple and robust for a demo. The server reads the same CORTEX_FQDN / CORTEX_API_KEY
/ CORTEX_API_KEY_ID environment variables Sirius already uses (passed through).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from typing import Any

from sirius.config import load_config
from sirius.events import emit
from sirius.skills import registry

_log = logging.getLogger("sirius.cortex_mcp")

# Populated by init(): the native skill names registered for cortex-mcp tools.
REGISTERED: list[str] = []


def _server_params():
    """StdioServerParameters for launching cortex-mcp, with our env passed through."""
    from mcp import StdioServerParameters

    cfg = load_config().cortex_mcp
    command = cfg.command or sys.executable
    args = list(cfg.args) if cfg.args else ["-m", "cortex_mcp"]
    return StdioServerParameters(command=command, args=args, env={**os.environ})


async def _with_session(fn):
    """Open a stdio MCP session to cortex-mcp, run `fn(session)`, tear down."""
    from mcp import ClientSession
    from mcp.client.stdio import stdio_client

    params = _server_params()
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            return await fn(session)


async def _list_tools() -> list[dict]:
    async def _fn(session):
        res = await session.list_tools()
        return [
            {
                "name": t.name,
                "description": t.description or f"cortex-mcp tool {t.name}",
                "input_schema": t.inputSchema or {"type": "object", "properties": {}},
            }
            for t in res.tools
        ]

    return await _with_session(_fn)


async def _call_tool(tool: str, arguments: dict) -> Any:
    async def _fn(session):
        res = await session.call_tool(tool, arguments or {})
        text = "".join(
            getattr(c, "text", "") for c in res.content
            if getattr(c, "type", None) == "text"
        )
        if getattr(res, "isError", False):
            return {"ok": False, "error": text or "cortex-mcp reported an error"}
        try:
            return json.loads(text)
        except (ValueError, TypeError):
            return {"ok": True, "output": text}

    return await _with_session(_fn)


def _make_handler(tool: str):
    """Build a sync skill handler that runs one cortex-mcp tool call over stdio."""
    def handler(**kwargs: Any) -> Any:
        emit("skill_invoked", f"cortex-mcp → {tool}", severity="info",
             payload={"tool": tool, "input": kwargs, "via": "cortex-mcp"})
        try:
            result = asyncio.run(_call_tool(tool, kwargs))
        except Exception as e:  # noqa: BLE001
            emit("status", f"cortex-mcp {tool} failed: {e}", severity="danger",
                 payload={"tool": tool, "error": str(e)})
            return {"ok": False, "error": f"cortex-mcp call failed: {e}"}
        ok = not (isinstance(result, dict) and result.get("ok") is False)
        emit("status", f"cortex-mcp ← {tool}", severity="success" if ok else "danger",
             payload={"tool": tool, "via": "cortex-mcp"})
        return result

    return handler


def init() -> list[str]:
    """Discover cortex-mcp tools over the protocol and register them as native
    `cortexmcp_*` skills. Returns the registered names ([] if disabled/unavailable
    — Sirius still boots and Defender falls back to native cortex skills)."""
    global REGISTERED
    cfg = load_config().cortex_mcp
    if not cfg.enabled:
        _log.info("cortex-mcp client disabled (cortex_mcp.enabled=false)")
        return []
    try:
        tools = asyncio.run(_list_tools())
    except Exception as e:  # noqa: BLE001
        _log.warning("cortex-mcp discovery failed (%s) — Defender uses native cortex skills", e)
        return []

    names: list[str] = []
    for t in tools:
        native_name = f"cortexmcp_{t['name']}"
        registry.skill(native_name,
                       f"[via cortex-mcp] {t['description']}",
                       t["input_schema"])(_make_handler(t["name"]))
        names.append(native_name)
    REGISTERED = names
    _log.info("cortex-mcp client ready: %d tools registered", len(names))
    return names
