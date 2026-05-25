"""
fnol_tools_mcp.py — Model Context Protocol (MCP) server exposing FNOL tools.

Aligned to V2 Blueprint (L100 · May 2026 · Industrialization-Aware Edition).

Provides:
- Stdio MCP server (default; for Claude Desktop / Code integrations)
- Optional HTTP shim (for browser / non-MCP clients)

Optional dependency: pip install mcp
If unavailable, this module logs a warning and exposes a minimal stdio
JSON-line protocol that mimics the MCP request/response shape so the
platform stays usable without the full SDK.

Run:
    python fnol_tools_mcp.py            # stdio (MCP standard)
    python fnol_tools_mcp.py --http     # HTTP shim on :8765
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from typing import Any, Dict, List

from fnol_tools_registry import ToolRegistry

logger = logging.getLogger("fnol.mcp")

try:
    from mcp.server import Server
    from mcp.server.stdio import stdio_server
    from mcp.types import Tool, TextContent
    MCP_AVAILABLE = True
except Exception:  # pragma: no cover
    MCP_AVAILABLE = False


# ---------------------------------------------------------------------------
# Build tool descriptors from the registry (shared by both transports)
# ---------------------------------------------------------------------------

def _registry_specs(registry: ToolRegistry) -> List[Dict[str, Any]]:
    """Convert ToolRegistry.tool_specs() to MCP-style descriptors."""
    out: List[Dict[str, Any]] = []
    for spec in registry.tool_specs():
        out.append({
            "name": spec["name"],
            "description": spec.get("description", ""),
            "inputSchema": spec.get("input_schema") or spec.get("parameters") or {
                "type": "object", "properties": {}, "additionalProperties": True,
            },
        })
    return out


# ---------------------------------------------------------------------------
# Real MCP server (preferred path)
# ---------------------------------------------------------------------------

async def _run_mcp_stdio(registry: ToolRegistry) -> None:
    if not MCP_AVAILABLE:
        raise RuntimeError("mcp package not installed; use --http or pip install mcp")

    server = Server("fnol-intelligence-platform")

    @server.list_tools()
    async def list_tools() -> List[Tool]:
        tools: List[Tool] = []
        for s in _registry_specs(registry):
            tools.append(Tool(
                name=s["name"],
                description=s["description"],
                inputSchema=s["inputSchema"],
            ))
        return tools

    @server.call_tool()
    async def call_tool(name: str, arguments: Dict[str, Any] | None) -> List[TextContent]:
        try:
            result = registry.call(name, **(arguments or {}))
            return [TextContent(type="text", text=json.dumps(result, default=str))]
        except Exception as e:
            return [TextContent(type="text", text=json.dumps({"error": str(e)}))]

    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


# ---------------------------------------------------------------------------
# Fallback stdio shim (line-delimited JSON; minimal MCP-shape)
# ---------------------------------------------------------------------------

async def _run_shim_stdio(registry: ToolRegistry) -> None:
    """Line-delimited JSON shim. One request per line; one response per line."""
    logger.warning("MCP SDK not available; running JSON-line shim on stdio.")
    loop = asyncio.get_event_loop()

    def _read_line() -> str:
        return sys.stdin.readline()

    while True:
        line = await loop.run_in_executor(None, _read_line)
        if not line:
            break
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception as e:
            sys.stdout.write(json.dumps({"error": f"bad json: {e}"}) + "\n")
            sys.stdout.flush()
            continue

        method = req.get("method")
        rid = req.get("id")
        params = req.get("params", {}) or {}

        if method == "list_tools":
            resp = {"id": rid, "result": _registry_specs(registry)}
        elif method == "call_tool":
            name = params.get("name", "")
            args = params.get("arguments", {}) or {}
            try:
                result = registry.call(name, **args)
                resp = {"id": rid, "result": result}
            except Exception as e:
                resp = {"id": rid, "error": str(e)}
        else:
            resp = {"id": rid, "error": f"unknown method: {method}"}

        sys.stdout.write(json.dumps(resp, default=str) + "\n")
        sys.stdout.flush()


# ---------------------------------------------------------------------------
# HTTP shim (for browser-based callers)
# ---------------------------------------------------------------------------

async def _run_http(registry: ToolRegistry, host: str = "127.0.0.1", port: int = 8765) -> None:
    try:
        from aiohttp import web
    except Exception as e:
        raise RuntimeError(f"aiohttp not installed: {e}")

    app = web.Application()

    async def list_tools(_req):
        return web.json_response({"tools": _registry_specs(registry)})

    async def call_tool(req):
        body = await req.json()
        name = body.get("name", "")
        args = body.get("arguments", {}) or {}
        try:
            result = registry.call(name, **args)
            return web.json_response({"result": result})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=400)

    app.router.add_get("/mcp/tools", list_tools)
    app.router.add_post("/mcp/call", call_tool)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    logger.info(f"FNOL MCP HTTP shim listening on http://{host}:{port}")
    while True:
        await asyncio.sleep(3600)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s: %(message)s")
    p = argparse.ArgumentParser(description="FNOL MCP server (V2 Blueprint aligned).")
    p.add_argument("--http", action="store_true", help="Run HTTP shim instead of stdio.")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--maturity", default="L2", choices=["L1", "L2", "L3"])
    args = p.parse_args()

    registry = ToolRegistry(maturity=args.maturity)

    if args.http:
        asyncio.run(_run_http(registry, args.host, args.port))
    elif MCP_AVAILABLE:
        asyncio.run(_run_mcp_stdio(registry))
    else:
        asyncio.run(_run_shim_stdio(registry))


if __name__ == "__main__":
    main()
