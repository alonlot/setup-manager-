"""Start `setup-manager mcp` over stdio and call tools with the MCP client."""
import asyncio
import json
import os
import sys

import pytest

mcp = pytest.importorskip("mcp")
from mcp import ClientSession  # noqa: E402
from mcp.client.stdio import StdioServerParameters, stdio_client  # noqa: E402

from conftest import ROOT  # noqa: E402


def _payload(result):
    # mcp 2.x uses snake_case attribute names, mcp 1.x camelCase.
    sc = getattr(result, "structured_content", None) or getattr(result, "structuredContent", None)
    if sc is not None:
        return sc["result"] if isinstance(sc, dict) and set(sc) == {"result"} else sc
    return json.loads(result.content[0].text)


def _is_error(result):
    return bool(getattr(result, "is_error", None) or getattr(result, "isError", None))


def test_mcp_tools(manager_config):
    params = StdioServerParameters(command=sys.executable, args=["-m", "setup_manager", "--config",
                                                                 str(manager_config), "mcp"],
                                   env=dict(os.environ, PYTHONPATH=str(ROOT)), cwd=str(ROOT))

    async def go():
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = {t.name for t in (await session.list_tools()).tools}
                assert {"list_setups", "list_usb", "attach_usb", "detach_usb", "run_command",
                        "find_usb", "list_attached"} <= tools
                setups = _payload(await session.call_tool("list_setups", {}))
                assert setups[0]["id"] == "bench" and setups[0]["online"] is True
                devs = _payload(await session.call_tool("list_usb", {"setup": "bench"}))
                assert any(d["busid"] == "1-1" for d in devs)
                r = _payload(await session.call_tool("attach_usb", {"setup": "bench", "busid": "1-2"}))
                assert r["port"]["busid"] == "1-2"
                attached = _payload(await session.call_tool("list_attached", {}))
                assert attached[0]["setup"] == "bench"
                _payload(await session.call_tool("detach_usb", {"setup": "bench", "busid": "1-2"}))
                r = _payload(await session.call_tool("run_command", {"setup": "bench", "command": "echo mcp"}))
                assert r["stdout"] == "mcp\n"
                assert "identify_mcu" in tools
                r = _payload(await session.call_tool("identify_mcu", {"setup": "bench", "busid": "1-3",
                                                                      "reset": "none", "duration": 0.5}))
                assert r["capture"]["ttys"] == [] and "label" in r["guess"]
                bad = await session.call_tool("list_usb", {"setup": "nope"})
                assert _is_error(bad) and "unknown setup" in bad.content[0].text
    asyncio.run(go())
