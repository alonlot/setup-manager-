"""Interactive shell on a setup PC, in the current terminal (like ssh).

`setup-manager shell bench-1` connects to the agent's PTY WebSocket. The app's
terminal tabs run this command inside a VTE terminal.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
import termios
import tty
from typing import Any

import aiohttp

from .agent_client import AgentError
from .fleet import Fleet, FleetError


def _size() -> tuple[int, int]:
    try:
        s = os.get_terminal_size(sys.stdout.fileno())
        return s.columns, s.lines
    except OSError:
        return 80, 24


async def _session(fleet: Fleet, ref: str, user: str | None) -> int:
    setup = fleet.setup(ref)
    client = fleet.client(setup)
    cols, rows = _size()
    assert fleet.session is not None
    loop = asyncio.get_running_loop()
    in_fd, out_fd = sys.stdin.fileno(), sys.stdout.fileno()
    is_tty = os.isatty(in_fd)
    old = termios.tcgetattr(in_fd) if is_tty else None
    sys.stderr.write(f"\x1b[90mconnecting to {setup['name']} ({setup['host']})…\x1b[0m\r\n")
    try:
        await client.ensure()
    except AgentError as e:
        sys.stderr.write(f"\x1b[31m{e.message}\x1b[0m\r\n")
        return 1
    if not client.encrypted:
        sys.stderr.write("\x1b[33mwarning: this connection is NOT encrypted (old agent)\x1b[0m\r\n")
    try:
        ws = await fleet.session.ws_connect(client.shell_url(cols, rows, user), headers=client.headers,
                                            ssl=client.ssl, heartbeat=30, max_msg_size=16 * 1024 * 1024)
    except aiohttp.ServerFingerprintMismatch:
        sys.stderr.write(f"\x1b[31m{client.mismatch_message()}\x1b[0m\r\n")
        return 1
    except aiohttp.WSServerHandshakeError as e:
        msg = "the agent rejected the token" if e.status == 401 else f"HTTP {e.status}"
        sys.stderr.write(f"\x1b[31mcannot open a shell on {setup['name']}: {msg}\x1b[0m\r\n")
        return 1
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
        sys.stderr.write(f"\x1b[31mcannot reach {setup['name']} ({client.base}): {e}\x1b[0m\r\n")
        return 1
    code = 0
    stdin_closed = asyncio.Event()
    try:
        if is_tty:
            tty.setraw(in_fd, termios.TCSANOW)  # keep what the user typed while connecting

        def on_stdin() -> None:
            try:
                data = os.read(in_fd, 65536)
            except OSError:
                data = b""
            if not data:
                loop.remove_reader(in_fd)
                stdin_closed.set()
                return
            asyncio.ensure_future(ws.send_bytes(data))

        def on_resize() -> None:
            c, r = _size()
            asyncio.ensure_future(ws.send_str(json.dumps({"t": "r", "c": c, "r": r})))

        loop.add_reader(in_fd, on_stdin)
        if is_tty:
            loop.add_signal_handler(signal.SIGWINCH, on_resize)
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.BINARY:
                os.write(out_fd, msg.data)
            elif msg.type == aiohttp.WSMsgType.TEXT:
                info: dict[str, Any] = json.loads(msg.data)
                if info.get("t") == "exit":
                    code = info.get("code") or 0
                    break
            else:
                break
    finally:
        try:
            loop.remove_reader(in_fd)
            if is_tty:
                loop.remove_signal_handler(signal.SIGWINCH)
        except (ValueError, RuntimeError):
            pass
        if old is not None:
            termios.tcsetattr(in_fd, termios.TCSADRAIN, old)
        await ws.close()
    sys.stderr.write(f"\r\n\x1b[90m[connection to {setup['name']} closed]\x1b[0m\r\n")
    return int(code)


def run_shell(store: Any, ref: str, user: str | None = None) -> int:
    async def go() -> int:
        async with Fleet(store) as fleet:
            return await _session(fleet, ref, user)
    try:
        return asyncio.run(go())
    except FleetError as e:
        sys.stderr.write(f"error: {e.message}\n")
        return 1
    except KeyboardInterrupt:
        return 130
