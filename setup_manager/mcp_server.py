"""MCP server: lets AI agents (Claude Code, Claude Desktop, Cursor, ...) control the setups.

Run with stdio (default):      setup-manager mcp
Or over HTTP (streamable):     setup-manager mcp --http --port 8801

It uses the same config file as the desktop app, so the setups you add in the
app are visible here. USB devices are attached to the PC where this MCP server runs.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

from .config import ConfigStore
from .fleet import Fleet, FleetError

try:  # mcp >= 2
    from mcp.server.mcpserver import MCPServer as _Server
    from mcp.server.mcpserver.exceptions import ToolError
    _MCP_V2 = True
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server  # type: ignore[no-redef]
    from mcp.server.fastmcp.exceptions import ToolError  # type: ignore[no-redef]
    _MCP_V2 = False

INSTRUCTIONS = """\
Controls a lab of Linux "setup PCs" used for embedded development. Each setup PC
runs an agent that shares its USB devices (debug probes, serial adapters, boards)
over USB/IP. Attaching a device makes it appear on the PC where this MCP server
runs, as if it was plugged in locally (e.g. /dev/ttyACM0, an ST-Link, a J-Link).

Typical flow: list_setups -> list_usb(setup) or find_usb("st-link") ->
attach_usb(setup, busid) -> work with the device -> detach_usb(setup, busid).
Use run_command to run shell commands on a setup PC.
To flash and check a board: serial_open (before the flash), flash_firmware(file=...),
then serial_wait_for("your boot message"). serial_read gives the new output since your last read.
Devices can have nicknames (set_device_name); "used_by_text" says who has a device.
identify_mcu resets a board, reads its boot log and guesses the MCU. Devices that were
identified before show it in the "mcu" field of list_usb.
A setup can be given by id, name or host. A busid looks like "1-1.2".
"""


class _State:
    fleet: Fleet | None = None
    lock: asyncio.Lock | None = None
    cursors: dict[str, int] = {}


async def _fleet() -> Fleet:
    if _State.lock is None:
        _State.lock = asyncio.Lock()
    async with _State.lock:
        if _State.fleet is None:
            _State.fleet = Fleet(_State.store)  # type: ignore[attr-defined]
        await _State.fleet.open()  # (re)opens the HTTP session when needed
    return _State.fleet


@contextlib.asynccontextmanager
async def _lifespan(server: Any) -> Any:
    try:
        yield {}
    finally:
        if _State.fleet is not None:
            await _State.fleet.close()


async def _call(fn: Any) -> Any:
    fleet = await _fleet()
    try:
        return await fn(fleet)
    except FleetError as e:
        # ToolError: the SDK sends this text to the model (other errors are hidden).
        raise ToolError(e.message) from None


def _short_device(d: dict[str, Any]) -> dict[str, Any]:
    keys = ("busid", "name", "vidpid", "serial", "state", "state_detail", "attached_here", "local_port", "ttys",
            "sticky", "auto_bind", "protected", "setup", "setup_name", "mcu", "nickname", "used_by_text")
    return {k: d[k] for k in keys if k in d}


def build_server(store: ConfigStore | None = None) -> Any:
    _State.store = store or ConfigStore()  # type: ignore[attr-defined]
    mcp = _Server("setup-manager", instructions=INSTRUCTIONS, lifespan=_lifespan)

    @mcp.tool()
    async def list_setups() -> list[dict[str, Any]]:
        """List all setup PCs with online state, OS, load and USB counts."""
        async def go(f: Fleet) -> Any:
            await f.refresh_all()
            keep = ("id", "name", "host", "online", "error", "hostname", "os", "cpu_percent", "load", "usb_count",
                    "usb_bound", "usb_in_use", "notes", "tags", "encrypted")
            return [{k: s.get(k) for k in keep} for s in f.summaries()]
        return await _call(go)

    @mcp.tool()
    async def setup_status(setup: str) -> dict[str, Any]:
        """Full status of one setup PC: OS, kernel, uptime, CPU, memory, disk, IPs, temperatures, usbipd state."""
        return await _call(lambda f: f.status(setup))

    @mcp.tool()
    async def list_usb(setup: str, include_hubs: bool = False) -> list[dict[str, Any]]:
        """List the USB devices of a setup PC with their USB/IP state.

        state: available (not shared), bound (shared, no client), in_use (a client has it).
        attached_here: true when the device is attached to this PC (local_port = usbip port).
        """
        return await _call(lambda f: f.usb(setup, include_hubs))

    @mcp.tool()
    async def find_usb(query: str = "") -> list[dict[str, Any]]:
        """Search USB devices on all setups by words in name, vid:pid, serial, busid or tty (e.g. "stlink", "0483")."""
        async def go(f: Fleet) -> Any:
            return [_short_device(d) for d in await f.find_usb(query)]
        return await _call(go)

    @mcp.tool()
    async def attach_usb(setup: str, busid: str, force: bool = False) -> dict[str, Any]:
        """Share a USB device of a setup (usbip bind) and attach it to this PC (usbip attach).

        force=true takes the device from another client that uses it.
        """
        return await _call(lambda f: f.attach(setup, busid, force))

    @mcp.tool()
    async def detach_usb(setup: str, busid: str, unbind: bool = True) -> dict[str, Any]:
        """Detach a USB device from this PC. unbind=true (default) also gives it back to the setup PC."""
        return await _call(lambda f: f.detach(setup, busid, unbind))

    @mcp.tool()
    async def bind_usb(setup: str, busid: str, force: bool = False) -> dict[str, Any]:
        """Only share (usbip bind) a device on the setup PC, without attaching it here."""
        return await _call(lambda f: f.bind(setup, busid, force))

    @mcp.tool()
    async def unbind_usb(setup: str, busid: str) -> dict[str, Any]:
        """Stop sharing a device (usbip unbind). The setup PC gets the device back. Clients lose it."""
        return await _call(lambda f: f.unbind(setup, busid))

    @mcp.tool()
    async def reset_usb(setup: str, busid: str) -> dict[str, Any]:
        """Send a USB port reset to a device on the setup PC (like unplug/replug)."""
        return await _call(lambda f: f.reset(setup, busid))

    @mcp.tool()
    async def keep_attached(setup: str, busid: str, enabled: bool = True) -> dict[str, Any]:
        """Turn keep-attached on or off. When on, the manager re-attaches the device when it drops
        (for example after a board resets or a debugger re-enumerates). Needs the desktop app or
        `setup-manager daemon` to run on this PC."""
        return await _call(lambda f: f.set_sticky(setup, busid, enabled))

    @mcp.tool()
    async def identify_mcu(setup: str, busid: str, profile: str = "", reset: str = "", baud: str = "",
                           duration: float = 0, script: str = "", after_script: str = "") -> dict[str, Any]:
        """Reset the MCU behind a USB device, capture its serial boot log and guess which MCU it is.

        reset: auto, rts (ESP32/ESP8266), dtr (Arduino), rts+dtr, repl (MicroPython), probe (st-flash/pyocd),
        usb, none. baud: "auto" (tries rates until the text is readable) or a number.
        script runs before the reset and after_script runs after it (bash, on the PC with the serial port,
        with $SM_TTY = the port). Use them to make the device print more (for example send a command).
        Their output is also used for the guess. profile: a saved profile name (see identify_profiles).
        The result has guess.label (for example "ESP32-S3" or "probably STM32 (family)"), guess.sure,
        guess.confidence, guess.evidence, and the captured log.
        """
        opts: dict[str, Any] = {"reset": reset or None, "baud": baud or None, "duration": duration or None,
                                "script": script or None, "after_script": after_script or None}

        async def go(f: Fleet) -> Any:
            r = await f.identify(setup, busid, opts, profile or None)
            log_text = r["capture"].get("log", "")
            r["capture"]["log"] = log_text[-6000:]
            r["capture"].pop("captures", None)
            return r
        return await _call(go)

    @mcp.tool()
    async def identify_profiles() -> dict[str, Any]:
        """The identify profiles (built-in and saved) with their reset method, baud rate and scripts."""
        async def go(f: Fleet) -> Any:
            return f.identify_profiles()
        return await _call(go)

    @mcp.tool()
    async def save_identify_profile(name: str, reset: str = "auto", baud: str = "auto", duration: float = 4,
                                    script: str = "", after_script: str = "") -> dict[str, Any]:
        """Save an identify profile (reset method, baud rate, duration and the two scripts) for later use."""
        async def go(f: Fleet) -> Any:
            f.save_identify_profile(name, {"reset": reset, "baud": baud, "duration": duration, "script": script,
                                           "after_script": after_script})
            return {"saved": name}
        return await _call(go)

    # -- serial ----------------------------------------------------------------
    def _key(setup: str, busid: str, tty: str) -> str:
        return f"{setup}/{busid}/{tty}"

    @mcp.tool()
    async def serial_ports(setup: str) -> list[dict[str, Any]]:
        """The serial ports of a setup that you can open (devices that are not shared, or attached here)."""
        return await _call(lambda f: f.serial_ports(setup))

    @mcp.tool()
    async def serial_open(setup: str, busid: str = "", tty: str = "", baud: int = 115200) -> dict[str, Any]:
        """Open (or reuse) the serial port of a device and start to record its output.

        Give busid (the port of that device is used, or tty to select one of its ports), or only tty.
        The port stays open and records until serial_close or 15 minutes without reads. Open it BEFORE
        you flash or reset a board, so that you get the boot output.
        """
        async def go(f: Fleet) -> Any:
            h = await f.serial_open(setup, busid or None, tty or None, baud)
            _State.cursors[_key(setup, busid, tty)] = h.get("end", 0)
            return h
        return await _call(go)

    @mcp.tool()
    async def serial_read(setup: str, busid: str = "", tty: str = "", since: int = -2, wait: float = 0,
                          max_bytes: int = 20000) -> dict[str, Any]:
        """Read the recorded serial output as text.

        since=-2 (default): only what is new since your last serial_read or serial_open.
        since=0: everything still in the buffer (up to 2 MB). since=N: from byte offset N.
        wait: seconds to wait for new data when there is none yet.
        """
        async def go(f: Fleet) -> Any:
            k = _key(setup, busid, tty)
            start = _State.cursors.get(k, 0) if since == -2 else since
            r = await f.serial_read(setup, busid or None, tty or None, start, wait, max_bytes)
            _State.cursors[k] = r["offset"]
            return {"text": r["data"].decode("utf-8", "replace"), "offset": r["offset"], "lost": r["lost"],
                    "connected": r["connected"], "events": [e["text"] for e in r.get("events", [])][-10:]}
        return await _call(go)

    @mcp.tool()
    async def serial_write(setup: str, text: str, busid: str = "", tty: str = "", end: str = "\r\n") -> dict:
        """Send text to the serial port. end is added after the text (default CR LF; use "" for none)."""
        return await _call(lambda f: f.serial_write(setup, busid or None, tty or None, (text + end).encode()))

    @mcp.tool()
    async def serial_wait_for(setup: str, pattern: str, busid: str = "", tty: str = "", timeout: float = 30,
                              since: int = -1) -> dict[str, Any]:
        """Wait until the serial output matches a regular expression (for example "boot OK|panic").

        since=-1: only output that comes after this call. since=N: also output from offset N
        (use the offset from serial_open to include output since you opened the port).
        Returns matched, match, groups and the text that was read.
        """
        return await _call(lambda f: f.serial_wait_for(setup, busid or None, tty or None, pattern, timeout, since))

    @mcp.tool()
    async def serial_reset(setup: str, busid: str, method: str = "auto", tty: str = "") -> dict[str, Any]:
        """Reset the board through the open serial port (methods as in identify_mcu: auto, rts, dtr, ...)."""
        return await _call(lambda f: f.serial_reset(setup, busid, tty or None, method))

    @mcp.tool()
    async def serial_close(setup: str, busid: str = "", tty: str = "") -> dict[str, Any]:
        """Close a serial port that you opened."""
        return await _call(lambda f: f.serial_close(setup, busid or None, tty or None))

    # -- files and flash --------------------------------------------------------
    @mcp.tool()
    async def upload_file(setup: str, local_path: str, remote_path: str = "") -> dict[str, Any]:
        """Copy a file from this PC to a setup PC. A relative remote_path goes in the agent's files folder."""
        return await _call(lambda f: f.upload(setup, local_path, remote_path or None))

    @mcp.tool()
    async def download_file(setup: str, remote_path: str, local_path: str = "") -> dict[str, Any]:
        """Copy a file from a setup PC to this PC (for example a log or a core dump)."""
        return await _call(lambda f: f.download(setup, remote_path, local_path or None))

    @mcp.tool()
    async def list_files(setup: str, path: str = ".") -> dict[str, Any]:
        """List a folder on a setup PC (relative paths are in the agent's files folder)."""
        return await _call(lambda f: f.files(setup, path))

    @mcp.tool()
    async def flash_profiles_list() -> dict[str, Any]:
        """The flash profiles (esptool, st-flash, OpenOCD, pyOCD, nrfjprog, J-Link, picotool, avrdude, ...)."""
        async def go(f: Fleet) -> Any:
            return f.flash_profiles()
        return await _call(go)

    @mcp.tool()
    async def flash_firmware(setup: str, busid: str, file: str, profile: str = "", script: str = "",
                             env: dict[str, str] | None = None, monitor_seconds: float = -1,
                             timeout: float = 300) -> dict[str, Any]:
        """Flash a firmware file (a path on this PC) to the board behind a USB device.

        The file is copied to the setup PC and the profile script runs there ($SM_FILE = the file,
        $SM_TTY = the serial port). profile: a name from flash_profiles_list; empty = the last one used
        for this device, else a suggested one. script/env replace the profile values (for example
        env={"SM_TARGET": "target/stm32h7x.cfg"}). monitor_seconds: read the serial output for this
        time after the flash (-1 = the profile value). Check "ok", "stdout", "stderr" and "monitor".
        """
        async def go(f: Fleet) -> Any:
            prof = profile or None
            opts: dict[str, Any] = {"timeout": timeout}
            if not prof and not script:
                devs = await f.usb(setup, include_hubs=True)
                d = next((x for x in devs if x["busid"] == busid), {"busid": busid})
                last = f.flash_options(setup, d)
                prof = last.get("profile") or None
                opts.update({k: v for k, v in last.items() if k in ("script", "env", "monitor_seconds", "baud")})
            if script:
                opts["script"] = script
            if env:
                opts["env"] = env
            if monitor_seconds >= 0:
                opts["monitor_seconds"] = monitor_seconds
            r = await f.flash(setup, busid, file, prof, opts)
            for k in ("stdout", "stderr"):
                r[k] = (r.get(k) or "")[-6000:]
            r.pop("env", None)
            return r
        return await _call(go)

    @mcp.tool()
    async def set_device_name(setup: str, busid: str, name: str) -> dict[str, Any]:
        """Give a USB device a nickname (for example "Board A, STM32H7 rev2"). Empty name removes it.
        The name is stored on the setup PC, so every manager sees it."""
        return await _call(lambda f: f.set_nickname(setup, busid, name))

    @mcp.tool()
    async def list_attached() -> list[dict[str, Any]]:
        """List the USB/IP devices attached to this PC, with the setup each one comes from."""
        return await _call(lambda f: f.local_ports())

    @mcp.tool()
    async def run_command(setup: str, command: str, timeout: float = 60, cwd: str = "",
                          user: str = "") -> dict[str, Any]:
        """Run a shell command on a setup PC (bash -lc). Returns exit_code, stdout, stderr, duration.

        timeout is in seconds. user="" runs as the configured default user of that setup.
        """
        return await _call(lambda f: f.exec(setup, command, timeout, cwd or None, user or None))

    @mcp.tool()
    async def run_command_many(command: str, setups: list[str] | None = None,
                               timeout: float = 60) -> list[dict[str, Any]]:
        """Run the same shell command on many setups at once (all setups when setups is empty)."""
        return await _call(lambda f: f.exec_many(setups or None, command, timeout))

    @mcp.tool()
    async def agent_log(setup: str, lines: int = 100) -> list[str]:
        """Recent log lines of the agent on a setup PC (binds, errors, commands)."""
        async def go(f: Fleet) -> Any:
            return [x["msg"] for x in (await f.agent_log(setup, lines)).get("lines", [])]
        return await _call(go)

    return mcp


def run(transport: str = "stdio", host: str = "127.0.0.1", port: int = 8801,
        store: ConfigStore | None = None) -> None:
    mcp = build_server(store)
    if transport == "stdio":
        mcp.run("stdio")
        return
    if _MCP_V2:
        mcp.run("streamable-http", host=host, port=port)
    else:
        mcp.settings.host = host
        mcp.settings.port = port
        mcp.run("streamable-http")
