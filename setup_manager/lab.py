"""Setup Manager as a Python library for automation (scripts, CI, pytest).

    from setup_manager import Lab

    with Lab() as lab:                               # the same config as the app
        board = lab.device("Board A")                # nickname, serial, "bench-1:1-2", "0483:374b"
        with board.reserve(owner="ci-42"):           # a lock: other users cannot change the board
            board.flash("build/zephyr.hex")
            with board.serial() as con:
                board.reset()
                con.expect(r"boot OK", timeout=10)

The API is synchronous. It runs the asyncio core (the same Fleet class as the
app, the CLI and the MCP server) in a background thread.
"""

from __future__ import annotations

import asyncio
import copy
import getpass
import os
import re
import socket
import threading
import time
from concurrent.futures import Future
from dataclasses import dataclass, field
from typing import Any, Awaitable, Iterator

from .agent_client import AgentClient
from .config import DEFAULTS, ConfigStore
from .fleet import Fleet, FleetError

__all__ = ["Lab", "Setup", "Device", "SerialConsole", "Reservation", "CommandResult", "FlashResult",
           "IdentifyResult", "LabError", "DeviceNotFound", "AmbiguousDevice", "BoardBusy", "FlashFailed",
           "CommandFailed", "ExpectTimeout"]


# --------------------------------------------------------------------------- errors

class LabError(Exception):
    """Base class of the errors of this library."""


class DeviceNotFound(LabError):
    pass


class AmbiguousDevice(LabError):
    def __init__(self, message: str, candidates: list["Device"]):
        super().__init__(message)
        self.candidates = candidates


class BoardBusy(LabError):
    """The device is reserved by another owner, or in use by another client."""


class FlashFailed(LabError):
    def __init__(self, result: "FlashResult"):
        super().__init__(f"flash failed (exit {result.exit_code}): {(result.stderr or result.stdout)[-500:]}")
        self.result = result


class CommandFailed(LabError):
    def __init__(self, result: "CommandResult"):
        super().__init__(f"command failed (exit {result.exit_code}): {result.command}\n{result.stderr[-500:]}")
        self.result = result


class ExpectTimeout(LabError, TimeoutError):
    def __init__(self, pattern: str, timeout: float, text: str):
        tail = text[-800:]
        super().__init__(f"no match for {pattern!r} in {timeout} s. Last output:\n{tail}")
        self.pattern, self.timeout, self.text = pattern, timeout, text


def _convert(e: FleetError) -> LabError:
    if e.status in (409, 423):
        return BoardBusy(e.message)
    if e.status == 404 and "no USB device" in e.message:
        return DeviceNotFound(e.message)
    return LabError(e.message)


# --------------------------------------------------------------------------- results

@dataclass
class CommandResult:
    command: str
    exit_code: int | None
    stdout: str
    stderr: str
    duration: float = 0.0
    timed_out: bool = False
    setup: str = ""

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out

    def check(self) -> "CommandResult":
        if not self.ok:
            raise CommandFailed(self)
        return self


@dataclass
class FlashResult:
    ok: bool
    exit_code: int | None
    stdout: str
    stderr: str
    duration: float
    where: str
    file: str | None
    monitor: str = ""
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


@dataclass
class IdentifyResult:
    mcu: str | None
    label: str
    sure: bool
    confidence: float
    firmware: list[str]
    evidence: list[str]
    log: str
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


# --------------------------------------------------------------------------- the async core, with leases

class _LeaseClient(AgentClient):
    """An AgentClient that also sends the leases of the reservations that this Lab holds."""

    def __init__(self, *args: Any, leases: list[str] | None = None, **kw: Any):
        super().__init__(*args, **kw)
        self._leases = leases or []

    @property
    def headers(self) -> dict[str, str]:
        h = dict(super().headers)
        if self._leases:
            h["X-SM-Lease"] = ",".join(self._leases)
        return h


class _LabFleet(Fleet):
    def __init__(self, store: ConfigStore):
        super().__init__(store)
        self.leases: dict[str, dict[str, str]] = {}  # setup id -> {reservation key: lease}

    client_class = _LeaseClient

    def _client_kwargs(self, setup: dict[str, Any]) -> dict[str, Any]:
        return {"leases": list(self.leases.get(setup["id"], {}).values())}


class _MemoryStore(ConfigStore):
    """A config that lives only in memory (Lab(setups=[...]))."""

    def __init__(self, data: dict[str, Any]):
        super().__init__(path=os.devnull)
        merged = copy.deepcopy(DEFAULTS)
        for k, v in data.items():
            if isinstance(v, dict) and isinstance(merged.get(k), dict):
                merged[k].update(v)
            else:
                merged[k] = v
        merged["setups"] = [self._normalize(s) for s in merged.get("setups", [])]
        self._mem = merged

    def data(self) -> dict[str, Any]:
        return self._mem

    def save(self) -> None:
        pass


# --------------------------------------------------------------------------- Lab

class Lab:
    """Entry point. Lab() uses the config of the app (~/.config/setup-manager/config.json).

    Lab(config="path.json") uses another config file. Lab(setups=[{"host": ..., "token": ...}]) uses
    no file at all. owner is the default owner of reservations (default: user@hostname).
    """

    def __init__(self, config: str | os.PathLike | None = None, setups: list[dict[str, Any]] | None = None,
                 owner: str | None = None, client_name: str | None = None, timeout: float = 600):
        if setups is not None:
            store: ConfigStore = _MemoryStore({"setups": setups})
        else:
            store = ConfigStore(config or os.environ.get("SETUP_MANAGER_CONFIG"))
        if client_name:
            store.data()["client_name"] = client_name  # only in memory unless something saves the config
        self.store = store
        self.owner = owner or f"{_user()}@{socket.gethostname()}"
        self.timeout = timeout
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, name="setup-manager-lab", daemon=True)
        self._thread.start()
        self._fleet = _LabFleet(store)
        self._run(self._fleet.open())
        self._reservations: list[Reservation] = []
        self._consoles: list[SerialConsole] = []
        self._closed = False

    # -- plumbing -----------------------------------------------------------------
    def _run(self, coro: Awaitable[Any], timeout: float | None = None) -> Any:
        fut: Future = asyncio.run_coroutine_threadsafe(coro, self._loop)  # type: ignore[arg-type]
        try:
            return fut.result(timeout or self.timeout)
        except FleetError as e:
            raise _convert(e) from None

    def close(self) -> None:
        """Close the consoles, release the reservations of this Lab, and stop the background thread."""
        if self._closed:
            return
        self._closed = True
        for c in list(self._consoles):
            c.close()
        for r in list(self._reservations):
            try:
                r.release()
            except LabError:
                pass
        try:
            self._run(self._fleet.close(), timeout=10)
        finally:
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(5)

    def __enter__(self) -> "Lab":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def __del__(self) -> None:  # best effort
        try:
            if not self._closed and self._loop.is_running():
                self.close()
        except Exception:  # noqa: BLE001
            pass

    # -- setups ---------------------------------------------------------------------
    def setups(self) -> list["Setup"]:
        return [Setup(self, s["id"]) for s in self.store.setups()]

    def setup(self, ref: str) -> "Setup":
        try:
            return Setup(self, self._fleet.setup(ref)["id"])
        except FleetError as e:
            raise LabError(e.message) from None

    def add_setup(self, host: str, token: str = "", name: str | None = None, port: int | None = None,
                  **extra: Any) -> "Setup":
        r = self._run(self._fleet.add_setup(dict(extra, host=host, token=token, name=name or host, port=port)))
        return Setup(self, r["setup"]["id"])

    # -- devices --------------------------------------------------------------------
    def devices(self, query: str = "", setup: str | None = None) -> list["Device"]:
        """All USB devices of the online setups (or of one setup) that match the words in query."""
        if setup:
            infos = [dict(d, setup=self.setup(setup).id) for d in self._run(self._fleet.usb(setup))]
            q = query.lower().split()
            infos = [d for d in infos if all(w in _haystack(d) for w in q)]
        else:
            infos = self._run(self._fleet.find_usb(query))
        return [Device(self, d["setup"], d) for d in infos]

    def device(self, ref: str, setup: str | None = None) -> "Device":
        """Find one device by: "setup:busid", nickname, USB serial number, "vid:pid", or name words."""
        if ":" in ref and not re.fullmatch(r"[0-9a-fA-F]{4}:[0-9a-fA-F]{4}", ref):
            sid, _, busid = ref.partition(":")
            if re.fullmatch(r"\d+-\d+(\.\d+)*", busid):
                devs = self.devices(setup=sid)
                for d in devs:
                    if d.busid == busid:
                        return d
                raise DeviceNotFound(f"no device {busid} on {sid}")
        devs = self.devices(setup=setup)
        r = ref.strip().lower()
        for test in (lambda d: (d.nickname or "").lower() == r,
                     lambda d: (d.serial_number or "").lower() == r,
                     lambda d: d.vidpid == r,
                     lambda d: d.busid == r,
                     lambda d: all(w in _haystack(d.info) for w in r.split())):
            found = [d for d in devs if test(d)]
            if len(found) == 1:
                return found[0]
            if len(found) > 1:
                names = ", ".join(f"{d.setup_id}:{d.busid} ({d.display_name})" for d in found)
                raise AmbiguousDevice(f"'{ref}' matches {len(found)} devices: {names}", found)
        raise DeviceNotFound(f"no device matches '{ref}'" + (f" on {setup}" if setup else ""))

    def reservations(self) -> list[dict[str, Any]]:
        """All reservations on all online setups."""
        out = []
        for s in self.setups():
            try:
                out += [dict(r, setup=s.id) for r in s._agent("GET", "/api/reservations")["reservations"]]
            except LabError:
                pass
        return out


def _user() -> str:
    try:
        return getpass.getuser()
    except Exception:  # noqa: BLE001
        return "user"


def _haystack(d: dict[str, Any]) -> str:
    return " ".join(str(x or "") for x in (d.get("name"), d.get("nickname"), d.get("vidpid"), d.get("serial"),
                                            d.get("busid"), (d.get("mcu") or {}).get("mcu"),
                                            " ".join(d.get("ttys") or []))).lower()


# --------------------------------------------------------------------------- Setup

class Setup:
    def __init__(self, lab: Lab, sid: str):
        self.lab, self.id = lab, sid

    def __repr__(self) -> str:
        return f"<Setup {self.id} {self.host}>"

    @property
    def _cfg(self) -> dict[str, Any]:
        return self.lab._fleet.setup(self.id)

    @property
    def name(self) -> str:
        return self._cfg["name"]

    @property
    def host(self) -> str:
        return self._cfg["host"]

    def _agent(self, method: str, path: str, body: dict | None = None, timeout: float = 30) -> Any:
        async def go() -> Any:
            return await self.lab._fleet._agent_call(self.lab._fleet.client(self.id).request(method, path, body,
                                                                                            timeout=timeout))
        return self.lab._run(go())

    def status(self) -> dict[str, Any]:
        return self.lab._run(self.lab._fleet.status(self.id))

    def online(self) -> bool:
        try:
            self.status()
            return True
        except LabError:
            return False

    def devices(self, query: str = "") -> list["Device"]:
        return self.lab.devices(query, setup=self.id)

    def device(self, ref: str) -> "Device":
        return self.lab.device(ref, setup=self.id)

    def run(self, command: str, timeout: float = 60, cwd: str | None = None, user: str | None = None,
            check: bool = False) -> CommandResult:
        r = self.lab._run(self.lab._fleet.exec(self.id, command, timeout, cwd, user), timeout=timeout + 30)
        res = CommandResult(command, r.get("exit_code"), r.get("stdout", ""), r.get("stderr", ""),
                            r.get("duration", 0.0), bool(r.get("timed_out")), self.id)
        return res.check() if check else res

    def upload(self, local_path: str, remote_path: str | None = None) -> dict[str, Any]:
        return self.lab._run(self.lab._fleet.upload(self.id, local_path, remote_path))

    def download(self, remote_path: str, local_path: str | None = None) -> dict[str, Any]:
        return self.lab._run(self.lab._fleet.download(self.id, remote_path, local_path))

    def files(self, path: str = ".") -> list[dict[str, Any]]:
        return self.lab._run(self.lab._fleet.files(self.id, path))["files"]


# --------------------------------------------------------------------------- Device

class Device:
    """One USB device on a setup PC. It follows the device by serial number when its port changes."""

    def __init__(self, lab: Lab, setup_id: str, info: dict[str, Any]):
        self.lab = lab
        self.setup_id = setup_id
        self.info = info
        self.vid, self.pid = info["vid"], info["pid"]
        self.serial_number = info.get("serial")
        self._console: SerialConsole | None = None

    def __repr__(self) -> str:
        return f"<Device {self.setup_id}:{self.busid} {self.display_name} ({self.vidpid})>"

    # -- properties (from the last refresh) ---------------------------------------
    busid = property(lambda self: self.info["busid"])
    name = property(lambda self: self.info["name"])
    nickname = property(lambda self: self.info.get("nickname"))
    display_name = property(lambda self: self.info.get("nickname") or self.info["name"])
    vidpid = property(lambda self: self.info["vidpid"])
    ttys = property(lambda self: list(self.info.get("ttys") or []))
    state = property(lambda self: self.info["state"])
    attached_here = property(lambda self: bool(self.info.get("attached_here")))
    used_by = property(lambda self: self.info.get("used_by_text"))
    reserved_by = property(lambda self: self.info.get("reserved_by"))
    mcu = property(lambda self: self.info.get("mcu"))

    @property
    def setup(self) -> Setup:
        return Setup(self.lab, self.setup_id)

    @property
    def local_ttys(self) -> list[str]:
        """The serial ports on this PC while the device is attached here, for example ["/dev/ttyACM3"]."""
        return [f"/dev/{t}" for t in self.refresh().info.get("local_ttys") or []]

    def refresh(self) -> "Device":
        """Read the device again. When it moved to another port, find it by serial number."""
        devs = self.lab._run(self.lab._fleet.usb(self.setup_id, include_hubs=True))
        match = None
        if self.serial_number:
            match = next((d for d in devs if d.get("serial") == self.serial_number and d["vid"] == self.vid
                          and d["pid"] == self.pid), None)
        if match is None:
            match = next((d for d in devs if d["busid"] == self.info["busid"] and d["vid"] == self.vid
                          and d["pid"] == self.pid), None)
        if match is None:
            raise DeviceNotFound(f"{self.display_name} ({self.vidpid}, SN {self.serial_number}) is not on "
                                 f"{self.setup_id} now")
        self.info = dict(match, setup=self.setup_id)
        return self

    def _call(self, fn: Any, timeout: float | None = None) -> Any:
        """Run fn(fleet, busid). When the busid is no longer valid, refresh and try one more time."""
        try:
            return self.lab._run(fn(self.lab._fleet, self.busid), timeout)
        except DeviceNotFound:
            old = self.busid
            self.refresh()
            if self.busid == old:
                raise
            return self.lab._run(fn(self.lab._fleet, self.busid), timeout)

    # -- reservation -------------------------------------------------------------------
    def reserve(self, owner: str | None = None, ttl: float = 300, wait: float = 0, note: str = "") -> "Reservation":
        """Lock the device for this Lab. Other users cannot bind, reset, flash or write to its serial port.

        wait: seconds to wait while somebody else has it (0 = fail at once with BoardBusy).
        The reservation renews itself while the Lab runs, and ends with release() or at the end
        of a with block. If this process dies, it ends after ttl seconds.
        """
        res = Reservation(self, owner or self.lab.owner, ttl, note)
        res.acquire(wait)
        return res

    # -- USB/IP ----------------------------------------------------------------------
    def attach(self, force: bool = False) -> "Device":
        self._call(lambda f, b: f.attach(self.setup_id, b, force))
        return self.refresh()

    def detach(self, unbind: bool = True) -> "Device":
        self._call(lambda f, b: f.detach(self.setup_id, b, unbind))
        return self.refresh()

    def attached(self, force: bool = False) -> "_Attached":
        """with device.attached(): ...  attaches here and detaches at the end (also on an error)."""
        return _Attached(self, force)

    def bind(self, force: bool = False) -> None:
        self._call(lambda f, b: f.bind(self.setup_id, b, force))

    def unbind(self) -> None:
        self._call(lambda f, b: f.unbind(self.setup_id, b))

    def usb_reset(self) -> None:
        """USB port reset of the device (like unplug and plug in)."""
        self._call(lambda f, b: f.reset(self.setup_id, b))

    def set_name(self, name: str) -> "Device":
        self._call(lambda f, b: f.set_nickname(self.setup_id, b, name))
        return self.refresh()

    # -- board actions -----------------------------------------------------------------
    def reset(self, method: str = "auto") -> list[str]:
        """Reset the board through its serial port (auto, rts, dtr, rts+dtr, repl, probe, usb)."""
        con = self.console
        return self._call(lambda f, b: f.serial_reset(self.setup_id, b, con.tty, method))["notes"]

    def flash(self, file: str | None, profile: str | None = None, env: dict[str, str] | None = None,
              script: str | None = None, monitor: float | None = None, timeout: float = 300,
              check: bool = True) -> FlashResult:
        """Flash a file (a path on this PC). profile=None: the last profile of this device, else a suggested one.

        check=True raises FlashFailed when the tool fails. monitor: seconds of serial output to return.
        """
        opts: dict[str, Any] = {"timeout": timeout}
        prof = profile
        if not prof and not script:
            last = self.lab._fleet.flash_options(self.setup_id, self.refresh().info)
            prof = last.get("profile") or None
            opts.update({k: v for k, v in last.items() if k in ("script", "env", "monitor_seconds", "baud")})
        if script:
            opts["script"] = script
        if env:
            opts["env"] = dict(opts.get("env") or {}, **env)
        if monitor is not None:
            opts["monitor_seconds"] = monitor
        r = self._call(lambda f, b: f.flash(self.setup_id, b, file, prof, opts), timeout=timeout + 300)
        res = FlashResult(r["ok"], r.get("exit_code"), r.get("stdout", ""), r.get("stderr", ""),
                          r.get("duration", 0.0), r.get("where", ""), r.get("file"),
                          (r.get("monitor") or {}).get("text", ""), r)
        if check and not res.ok:
            raise FlashFailed(res)
        return res

    def identify(self, profile: str | None = None, **options: Any) -> IdentifyResult:
        """Reset the MCU, read its boot log and guess which MCU it is (options as in the app)."""
        r = self._call(lambda f, b: f.identify(self.setup_id, b, options, profile))
        g = r["guess"]
        self.refresh()
        return IdentifyResult(g.get("mcu"), g["label"], g["sure"], g["confidence"], g.get("firmware") or [],
                              g.get("evidence") or [], r["capture"].get("log", ""), r)

    # -- serial ------------------------------------------------------------------------
    def serial(self, baud: int | None = 115200, tty: str | None = None, history: bool = False) -> "SerialConsole":
        """A serial console of the device. Use it in a with block, or call close()."""
        return SerialConsole(self, baud, tty, history).open()

    @property
    def console(self) -> "SerialConsole":
        """The default console (opened at the first use, 115200 baud, closed with the Lab)."""
        if self._console is None or self._console.closed:
            self._console = self.serial()
        return self._console


class _Attached:
    def __init__(self, dev: Device, force: bool):
        self.dev, self.force = dev, force

    def __enter__(self) -> Device:
        return self.dev.attach(self.force)

    def __exit__(self, *exc: Any) -> None:
        try:
            self.dev.detach()
        except LabError:
            pass


# --------------------------------------------------------------------------- Reservation

class Reservation:
    def __init__(self, dev: Device, owner: str, ttl: float, note: str = ""):
        self.dev, self.owner, self.ttl, self.note = dev, owner, max(5.0, float(ttl)), note
        self.lease: str | None = None
        self.key: str | None = None
        self.expires = 0.0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __repr__(self) -> str:
        return f"<Reservation {self.dev.display_name} by {self.owner} {'active' if self.active else 'released'}>"

    @property
    def active(self) -> bool:
        return self.lease is not None and time.time() < self.expires

    def _request(self) -> dict[str, Any]:
        body = {"busid": self.dev.busid, "owner": self.owner, "ttl": self.ttl, "note": self.note}
        if self.lease:
            body["lease"] = self.lease
        return self.dev.setup._agent("POST", "/api/reserve", body)

    def acquire(self, wait: float = 0) -> "Reservation":
        deadline = time.time() + max(0.0, wait)
        while True:
            try:
                r = self._request()
                break
            except DeviceNotFound:
                self.dev.refresh()
            except BoardBusy:
                if time.time() >= deadline:
                    raise
                time.sleep(min(2.0, max(0.1, deadline - time.time())))
        self.lease, self.key, self.expires = r["lease"], r["key"], r["expires"]
        self.dev.lab._fleet.leases.setdefault(self.dev.setup_id, {})[self.key] = self.lease
        self.dev.lab._reservations.append(self)
        self._stop.clear()
        self._thread = threading.Thread(target=self._renew_loop, name="reservation", daemon=True)
        self._thread.start()
        return self

    def _renew_loop(self) -> None:
        while not self._stop.wait(max(2.0, self.ttl / 3)):
            try:
                self.expires = self._request()["expires"]
            except LabError:
                pass  # try again; the reservation lasts ttl seconds

    def release(self) -> None:
        if self.lease is None:
            return
        self._stop.set()
        leases = self.dev.lab._fleet.leases.get(self.dev.setup_id, {})
        leases.pop(self.key or "", None)
        try:
            self.dev.setup._agent("POST", "/api/release", {"busid": self.dev.busid, "lease": self.lease})
        finally:
            self.lease = None
            if self in self.dev.lab._reservations:
                self.dev.lab._reservations.remove(self)

    def __enter__(self) -> "Reservation":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.release()


# --------------------------------------------------------------------------- SerialConsole

class SerialConsole:
    """pexpect-like access to a serial port: send(), expect(), read(). Records a log of everything."""

    def __init__(self, dev: Device, baud: int | None = 115200, tty: str | None = None, history: bool = False):
        self.dev, self.baud, self.tty, self.history = dev, baud, tty, history
        self.offset = 0
        self.closed = True
        self.log = ""          # everything this console received
        self._pending = ""     # received but not consumed by expect() or read()

    def __repr__(self) -> str:
        return f"<SerialConsole {self.dev.display_name} {self.tty} {'closed' if self.closed else 'open'}>"

    def open(self) -> "SerialConsole":
        h = self.dev._call(lambda f, b: f.serial_open(self.dev.setup_id, b, self.tty, self.baud))
        self.tty = h["tty"]
        self.offset = 0 if self.history else h.get("end", 0)
        self.closed = False
        self.dev.lab._consoles.append(self)
        return self

    def close(self) -> None:
        """Stop reading. The port stays open on the agent for other readers (it closes when idle)."""
        self.closed = True
        if self in self.dev.lab._consoles:
            self.dev.lab._consoles.remove(self)

    def __enter__(self) -> "SerialConsole":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def _fetch(self, wait: float) -> str:
        r = self.dev._call(lambda f, b: f.serial_read(self.dev.setup_id, b, self.tty, self.offset, wait))
        self.offset = r["offset"]
        text = r["data"].decode("utf-8", "replace")
        self.log += text
        self._pending += text
        return text

    def read(self, timeout: float = 0) -> str:
        """The text that came since the last read() or expect(). Waits up to timeout for something."""
        if not self._pending:
            self._fetch(timeout)
        else:
            self._fetch(0)
        out, self._pending = self._pending, ""
        return out

    def read_for(self, seconds: float) -> str:
        """Everything that comes in the next `seconds`."""
        end = time.time() + seconds
        while time.time() < end:
            self._fetch(min(1.0, max(0.0, end - time.time())))
        out, self._pending = self._pending, ""
        return out

    def expect(self, pattern: str | re.Pattern, timeout: float = 10) -> re.Match:
        """Wait until the output matches. Returns the re.Match. Text before the match is consumed.
        Raises ExpectTimeout (with the output) when it does not come in time."""
        rx = re.compile(pattern) if isinstance(pattern, str) else pattern
        deadline = time.time() + timeout
        while True:
            m = rx.search(self._pending)
            if m:
                self._pending = self._pending[m.end():]
                return m
            left = deadline - time.time()
            if left <= 0:
                raise ExpectTimeout(rx.pattern, timeout, self._pending)
            self._fetch(min(2.0, left))

    def lines(self, timeout: float = 10) -> Iterator[str]:
        """Yield complete lines as they come, until no line comes for `timeout` seconds."""
        while True:
            try:
                m = self.expect(r"[^\r\n]*\r?\n", timeout)
            except ExpectTimeout:
                return
            yield m.group(0).rstrip("\r\n")

    def send(self, text: str = "", end: str = "\r\n") -> None:
        self.write((text + end).encode())

    def write(self, data: bytes) -> None:
        self.dev._call(lambda f, b: f.serial_write(self.dev.setup_id, b, self.tty, data))

    def clear(self) -> None:
        """Forget the text that was not consumed yet (new output only from now on)."""
        self._fetch(0)
        self._pending = ""
