"""`setup-manager serial SETUP BUSID`: a serial terminal in this terminal (like picocom). Ctrl-] exits."""

from __future__ import annotations

import asyncio
import os
import re
import sys
import termios
import time
import tty
from typing import Any

from .fleet import Fleet, FleetError

EXIT_KEY = b"\x1d"  # Ctrl-]
ENDINGS = {"cr": b"\r", "lf": b"\n", "crlf": b"\r\n"}


async def _session(fleet: Fleet, args: Any) -> int:
    h = await fleet.serial_open(args.setup, args.busid, args.tty, args.baud)
    label = f"{h.get('name') or args.busid or ''} {h['tty']} @ {h['baud']} ({'this PC' if h['where'] == 'here' else h['setup']})"
    offset = 0 if args.history else h["end"]
    out = sys.stdout.buffer
    in_fd = sys.stdin.fileno()
    interactive = os.isatty(in_fd) and not args.read_only and not args.wait_for
    sys.stderr.write(f"\x1b[90m--- {label}{'  ·  Ctrl-] to exit' if interactive else ''} ---\x1b[0m\r\n")
    rx = re.compile(args.wait_for) if args.wait_for else None
    deadline = time.time() + args.timeout if args.timeout else None
    seen = ""
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    old = termios.tcgetattr(in_fd) if interactive else None
    eol = ENDINGS[args.eol]

    def on_key() -> None:
        data = os.read(in_fd, 1024)
        if not data or EXIT_KEY in data:
            stop.set()
            return
        data = data.replace(b"\r", eol) if eol != b"\r" else data
        asyncio.ensure_future(fleet.serial_write(args.setup, args.busid, args.tty, data))

    if interactive:
        tty.setraw(in_fd, termios.TCSANOW)
        loop.add_reader(in_fd, on_key)
    try:
        while not stop.is_set():
            wait = 1.0
            if deadline:
                wait = max(0.0, min(1.0, deadline - time.time()))
            r = await fleet.serial_read(args.setup, args.busid, args.tty, since=offset, wait=wait)
            offset = r["offset"]
            if r["data"]:
                out.write(r["data"])
                out.flush()
                if rx:
                    seen = (seen + r["data"].decode("utf-8", "replace"))[-100000:]
                    if rx.search(seen):
                        return 0
            if deadline and time.time() >= deadline:
                return 1 if rx else 0
    finally:
        if interactive:
            loop.remove_reader(in_fd)
            termios.tcsetattr(in_fd, termios.TCSADRAIN, old)
            sys.stderr.write("\r\n\x1b[90m--- closed ---\x1b[0m\r\n")
    return 0


def run_serial(store: Any, args: Any) -> int:
    async def go() -> int:
        async with Fleet(store) as fleet:
            return await _session(fleet, args)
    try:
        return asyncio.run(go())
    except FleetError as e:
        sys.stderr.write(f"error: {e.message}\n")
        return 2
    except KeyboardInterrupt:
        return 130
