"""Fleet: the core of the manager. The GUI, the CLI and the MCP server all use it."""

from __future__ import annotations

import asyncio
import getpass
import json
import logging
import os
import re
import socket
import subprocess
import time
from typing import Any

import aiohttp

from . import flash_profiles, mcu_detect
from . import tunnel
from .agent_client import AgentClient, AgentError, clean_fingerprint
from .config import ConfigError, ConfigStore
from .usbip_local import LocalUsbip, UsbipError

log = logging.getLogger("setup-manager")

DISCOVERY_MAGIC = b"SETUP-MANAGER-DISCOVER"
BUSID_RE = re.compile(r"^\d+-\d+(\.\d+)*$")


def match_rule(rule: str, dev: dict[str, Any]) -> bool:
    """Same rule matcher as the agent ("busid", "vid:pid", "vid:*", "vid:pid:serial")."""
    rule = str(rule).strip().lower()
    if BUSID_RE.match(rule):
        return dev["busid"] == rule
    parts = rule.split(":")
    if len(parts) < 2 or parts[0] not in ("*", dev["vid"]) or parts[1] not in ("*", dev["pid"]):
        return False
    if len(parts) >= 3:
        return ":".join(parts[2:]) in ("*", (dev.get("serial") or "").lower())
    return True


IDENTIFY_KEYS = ("reset", "baud", "duration", "script", "after_script", "script_timeout", "tty", "user")


FLASH_KEYS = ("script", "env", "timeout", "monitor_seconds", "baud", "user", "tty")


def used_by_text(d: dict[str, Any]) -> str:
    """Who uses a device, in words: "david@laptop (192.168.1.33)", "lab-pc (10.0.0.4)", ..."""
    u = d.get("used_by") or {}
    if u.get("client"):
        ip = u.get("ip")
        return u["client"] + (f" ({ip})" if ip and ip not in u["client"] else "")
    if u.get("ip"):
        return f"{u['host']} ({u['ip']})" if u.get("host") else u["ip"]
    if u.get("ips"):
        return "one of " + ", ".join(u["ips"])
    return "another client"


class FleetError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


class Fleet:
    def __init__(self, store: ConfigStore | None = None):
        self.store = store or ConfigStore()
        self.local = LocalUsbip(self.store.data().get("local_usbip"))
        self.session: aiohttp.ClientSession | None = None
        self.cache: dict[str, dict[str, Any]] = {}
        self.sticky_state: dict[str, dict[str, Any]] = {}
        self._tasks: list[asyncio.Task] = []
        self._resolve_cache: dict[str, tuple[float, set[str]]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._local_hub: Any = None
        self._local_usb: Any = None
        self.serial_targets: dict[str, dict[str, Any]] = {}
        self._transport: dict[str, tuple[str, float]] = {}  # setup id -> ("tls" | "plain", checked at)
        self._tunnel_procs: dict[int, Any] = {}

    # -- lifecycle -------------------------------------------------------------
    async def open(self) -> "Fleet":
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession()
        return self

    async def start_background(self) -> None:
        await self.open()
        self._tasks.append(asyncio.create_task(self._poll_loop()))
        self._tasks.append(asyncio.create_task(self._sticky_loop()))

    async def close(self) -> None:
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        self._tasks.clear()
        if self.session:
            await self.session.close()
            self.session = None

    async def __aenter__(self) -> "Fleet":
        return await self.open()

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    # -- lookup ----------------------------------------------------------------
    def setup(self, ref: str) -> dict[str, Any]:
        s = self.store.get_setup(ref)
        if not s:
            names = ", ".join(x["id"] for x in self.store.setups()) or "none configured"
            raise FleetError(f"unknown setup '{ref}' (known: {names})", 404)
        return s

    client_class = AgentClient

    def _client_kwargs(self, setup: dict[str, Any]) -> dict[str, Any]:
        return {}

    def client(self, ref: str | dict[str, Any]) -> AgentClient:
        setup = ref if isinstance(ref, dict) else self.setup(ref)
        assert self.session is not None, "Fleet.open() was not called"
        return self.client_class(self.session, setup, self.store.agent_port(setup), self.store.agent_token(setup),
                                 transport_cache=self._transport,
                                 on_pin=lambda fp, sid=setup["id"]: self._pin(sid, fp),
                                 require_tls=bool(self.store.data().get("require_encryption")),
                                 **self._client_kwargs(setup))

    def _pin(self, sid: str, fp: str) -> None:
        """Trust on first use: save the fingerprint of the agent's certificate (like SSH known_hosts)."""
        try:
            self.store.update_setup(sid, {"fingerprint": fp})
            log.warning("first TLS connection to %s: trusting its certificate sha256 %s", sid, fp)
        except ConfigError:
            pass

    def trust(self, ref: str, fingerprint: str | None) -> dict[str, Any]:
        """Set the pinned fingerprint of a setup ('' = forget it: the next connection trusts the new one)."""
        setup = self.setup(ref)
        fp = clean_fingerprint(fingerprint) if fingerprint else ""
        if fingerprint and not fp:
            raise FleetError("a fingerprint is 64 hex characters (SHA-256), with or without ':'")
        self.store.update_setup(setup["id"], {"fingerprint": fp})
        self._transport.pop(setup["id"], None)
        return {"setup": setup["id"], "fingerprint": fp}

    def encryption(self, ref: str) -> dict[str, Any]:
        setup = self.setup(ref)
        hit = self._transport.get(setup["id"])
        fp = clean_fingerprint(setup.get("fingerprint"))
        enc = True if fp else (hit[0] == "tls" if hit else None)
        return {"encrypted": enc, "fingerprint": fp, "transport": setup.get("transport", "auto")}

    def _lock(self, key: str) -> asyncio.Lock:
        if key not in self._locks:
            self._locks[key] = asyncio.Lock()
        return self._locks[key]

    async def _host_keys(self, host: str) -> set[str]:
        now = time.time()
        hit = self._resolve_cache.get(host)
        if hit and now - hit[0] < 60:
            return hit[1]
        keys = {host.lower()}
        try:
            infos = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
            keys |= {i[4][0].lower() for i in infos}
        except OSError:
            pass
        self._resolve_cache[host] = (now, keys)
        return keys

    # -- polling / cache -------------------------------------------------------
    async def refresh(self, sid: str) -> dict[str, Any]:
        setup = self.setup(sid)
        c = self.client(setup)
        entry = self.cache.setdefault(setup["id"], {})
        try:
            status, usb = await asyncio.gather(c.status(), c.usb())
            entry.update({"online": True, "status": status, "usb": usb.get("devices", []),
                          "usbip": usb.get("usbip"), "error": None, "last_seen": time.time()})
        except AgentError as e:
            entry.update({"online": False, "error": e.message})
        entry["last_poll"] = time.time()
        return entry

    async def refresh_all(self) -> None:
        await asyncio.gather(*(self.refresh(s["id"]) for s in self.store.setups()), return_exceptions=True)
        known = {s["id"] for s in self.store.setups()}
        for sid in list(self.cache):
            if sid not in known:
                del self.cache[sid]

    async def _poll_loop(self) -> None:
        while True:
            try:
                await self.refresh_all()
            except Exception:  # pragma: no cover
                log.exception("poll error")
            await asyncio.sleep(max(1.0, float(self.store.data().get("poll_interval") or 5)))

    def summaries(self) -> list[dict[str, Any]]:
        out = []
        for s in self.store.setups():
            e = self.cache.get(s["id"], {})
            st = e.get("status") or {}
            out.append({
                **ConfigStore.redact(s),
                "agent_port": self.store.agent_port(s),
                "online": e.get("online"),
                "error": e.get("error"),
                "last_seen": e.get("last_seen"),
                "hostname": st.get("hostname"),
                "os": st.get("os"),
                "cpu_percent": st.get("cpu_percent"),
                "load": st.get("load"),
                "memory": st.get("memory"),
                "uptime": st.get("uptime"),
                "version": st.get("version"),
                "usb_count": st.get("usb_count"),
                "usb_bound": st.get("usb_bound"),
                "usb_in_use": st.get("usb_in_use"),
                "usbip": st.get("usbip"),
                **self.encryption(s["id"]),
            })
        return out

    async def snapshot(self) -> dict[str, Any]:
        """Everything the app shows on the overview, in one call."""
        ports = await self.local_ports()
        return {"setups": self.summaries(), "ports": ports, "sticky": self.sticky_report(),
                "usbip": self.local.info()}

    # -- local attachments -----------------------------------------------------
    async def local_ports(self) -> list[dict[str, Any]]:
        """Devices attached to this PC, each tagged with the setup it comes from."""
        self.local.update(self.store.data().get("local_usbip"))
        ports = await self.local.ports()
        setups = self.store.setups()
        keys = await asyncio.gather(*(self._host_keys(s["host"]) for s in setups))
        tunnels = tunnel.load_tunnels()
        live = set()
        for p in ports:
            p["setup"] = None
            t = tunnels.get(str(p.get("remote_port"))) if p.get("host") in ("127.0.0.1", "localhost", "::1") else None
            if t and t.get("busid") == p.get("busid") and any(s["id"] == t["setup"] for s in setups):
                s = next(x for x in setups if x["id"] == t["setup"])
                p.update({"setup": s["id"], "setup_name": s["name"], "encrypted": True})
                live.add(str(p["remote_port"]))
                for d in self.cache.get(s["id"], {}).get("usb") or []:
                    if d["busid"] == p.get("busid"):
                        p["name"] = d["name"]
                        p["serial"] = d.get("serial")
                continue
            host = (p.get("host") or "").lower()
            for s, k in zip(setups, keys):
                if host in k and (p.get("remote_port") or 3240) == s.get("usbip_port", 3240):
                    p["setup"] = s["id"]
                    p["setup_name"] = s["name"]
                    p["encrypted"] = False
                    # `usbip port` often has no names (no usb.ids): use the agent's name.
                    for d in self.cache.get(s["id"], {}).get("usb") or []:
                        if d["busid"] == p.get("busid"):
                            p["name"] = d["name"]
                            p["serial"] = d.get("serial")
                    break
        stale = [k for k, t in tunnels.items() if k not in live and time.time() - t.get("at", 0) > 60]
        if stale:
            for k in stale:
                tunnels.pop(k, None)
            tunnel.save_tunnels(tunnels)
        return ports

    @staticmethod
    def _match_port(ports: list[dict[str, Any]], sid: str, busid: str) -> dict[str, Any] | None:
        for p in ports:
            if p.get("setup") == sid and p.get("busid") == busid:
                return p
        return None

    def _sticky_key(self, sid: str, busid: str) -> str:
        return f"{sid}/{busid}"

    async def usb(self, ref: str, include_hubs: bool = False) -> list[dict[str, Any]]:
        setup = self.setup(ref)
        data, ports = await asyncio.gather(self.client(setup).usb(include_hubs), self.local_ports())
        sticky = {x["busid"] for x in self.store.sticky() if x.get("setup") == setup["id"]}
        devices = data.get("devices", [])
        results = self.store.data().get("mcu_results") or {}
        for d in devices:
            d["mcu"] = results.get(self.device_key(setup["id"], d))
            p = self._match_port(ports, setup["id"], d["busid"])
            d["attached_here"] = p is not None
            d["local_port"] = p["port"] if p else None
            d["sticky"] = d["busid"] in sticky
            d["display_name"] = d.get("nickname") or d["name"]
            if d["state"] == "in_use" and not p:
                d["used_by_text"] = used_by_text(d)
                d["state_detail"] = f"in use by {d['used_by_text']}"
            elif p:
                d["state_detail"] = f"attached to this PC (port {p['port']})"
                d["local_ttys"] = self._local_ttys(p)
                u = d.get("used_by") or {}
                if u.get("client") != self.client_name:
                    asyncio.ensure_future(self._claim(setup, d["busid"]))  # tell the setup who has it
            else:
                d["state_detail"] = {"available": "not shared", "bound": "shared, waiting for a client",
                                     "error": "usbip error"}.get(d["state"], d["state"])
        entry = self.cache.setdefault(setup["id"], {})
        entry.update({"usb": devices, "online": True, "last_seen": time.time()})
        return devices

    async def _device(self, setup: dict[str, Any], busid: str) -> dict[str, Any]:
        data = await self.client(setup).usb(include_hubs=True)
        for d in data.get("devices", []):
            if d["busid"] == busid:
                return d
        raise FleetError(f"no USB device {busid} on {setup['name']}", 404)

    # -- USB actions -----------------------------------------------------------
    async def bind(self, ref: str, busid: str, force: bool = False) -> dict[str, Any]:
        return await self._agent_call(self.client(ref).bind(busid, force))

    async def unbind(self, ref: str, busid: str) -> dict[str, Any]:
        return await self._agent_call(self.client(ref).unbind(busid))

    async def reset(self, ref: str, busid: str) -> dict[str, Any]:
        return await self._agent_call(self.client(ref).reset(busid))

    async def _agent_call(self, coro: Any) -> Any:
        try:
            return await coro
        except AgentError as e:
            raise FleetError(e.message, e.status if e.status and e.status >= 400 else 502) from None

    async def attach(self, ref: str, busid: str, force: bool = False) -> dict[str, Any]:
        """Bind the device on the setup (if needed) and attach it to this PC."""
        setup = self.setup(ref)
        async with self._lock(self._sticky_key(setup["id"], busid)):
            ports = await self.local_ports()
            p = self._match_port(ports, setup["id"], busid)
            if p:
                return {"message": f"already attached on port {p['port']}", "port": p}
            dev = await self._agent_call(self._device(setup, busid))
            if dev["state"] == "in_use":
                if not force:
                    raise FleetError(f"{busid} ({dev['name']}) is in use by {used_by_text(dev)}. "
                                     "Use force to take it.", 409)
                await self.unbind(setup["id"], busid)
                dev["bound"] = False
            if not dev["bound"]:
                await self.bind(setup["id"], busid, force=force)
            out = await self._attach_usbip(setup, busid)
            for _ in range(24):
                ports = await self.local_ports()
                p = self._match_port(ports, setup["id"], busid)
                if p:
                    break
                await asyncio.sleep(0.25)
            log.info("attached %s:%s (%s)", setup["id"], busid, dev["name"])
            await self._claim(setup, busid)
            return {"message": out or "attached", "port": p, "device": dev}

    async def _attach_usbip(self, setup: dict[str, Any], busid: str) -> str:
        """usbip attach: through the encrypted tunnel of the agent, or directly (old agent, not encrypted)."""
        c = self.client(setup)
        info: dict[str, Any] = {}
        try:
            await c.ensure()
            if c.encrypted:
                info = await c.ping()
        except AgentError as e:
            raise FleetError(e.message, e.status or 502) from None
        if c.encrypted and info.get("tunnel"):
            leases = list(getattr(c, "_leases", []) or [])
            try:
                port, proc = await tunnel.open_tunnel(setup["host"], c.port, c.fingerprint, c.token, leases,
                                                      verify=bool(setup.get("verify_tls")))
            except tunnel.TunnelError as e:
                raise FleetError(f"attach failed: {e}", 502) from None
            try:
                out = await self.local.attach("127.0.0.1", busid, port)
            except UsbipError as e:
                why = await tunnel.tunnel_error(proc)
                status = 423 if "reserved" in why else 500
                raise FleetError(f"attach failed: {why or e}", status) from None
            self._tunnel_procs[port] = proc  # keep the reference (the helper keeps running on its own)
            tunnels = tunnel.load_tunnels()
            tunnels[str(port)] = {"setup": setup["id"], "busid": busid, "host": setup["host"], "pid": proc.pid,
                                  "at": time.time()}
            tunnel.save_tunnels(tunnels)
            return out or "attached (encrypted)"
        if self.store.data().get("require_encryption"):
            raise FleetError(f"{setup['name']} cannot tunnel USB/IP (agent before version 0.4): update it, or "
                             "turn off 'require encryption'", 495)
        try:
            out = await self.local.attach(setup["host"], busid, setup.get("usbip_port", 3240))
        except UsbipError as e:
            raise FleetError(f"attach failed: {e}", 500) from None
        log.warning("attached %s:%s WITHOUT encryption (old agent)", setup["id"], busid)
        return out or "attached (not encrypted)"

    async def detach(self, ref: str, busid: str, unbind: bool = True) -> dict[str, Any]:
        """Detach the device from this PC and (by default) unbind it on the setup."""
        setup = self.setup(ref)
        if any(x.get("setup") == setup["id"] and x.get("busid") == busid for x in self.store.sticky()):
            # A manual detach turns keep-attached off, else the loop attaches it again.
            self.store.set_sticky(setup["id"], busid, False)
            self.sticky_state.pop(self._sticky_key(setup["id"], busid), None)
        async with self._lock(self._sticky_key(setup["id"], busid)):
            ports = await self.local_ports()
            p = self._match_port(ports, setup["id"], busid)
            msgs = []
            if p:
                try:
                    msgs.append(await self.local.detach(p["port"]) or f"detached port {p['port']}")
                except UsbipError as e:
                    raise FleetError(f"detach failed: {e}", 500) from None
            else:
                msgs.append("not attached to this PC")
            try:
                await self.client(setup).release(busid)
            except AgentError:
                pass  # an old agent without claims
            if unbind:
                await asyncio.sleep(0.3)
                r = await self.unbind(setup["id"], busid)
                msgs.append(r.get("message") or "unbound")
            return {"message": "; ".join(m for m in msgs if m)}

    async def detach_port(self, port: int) -> dict[str, Any]:
        try:
            return {"message": await self.local.detach(port) or f"detached port {port}"}
        except UsbipError as e:
            raise FleetError(str(e), 500) from None

    async def set_sticky(self, ref: str, busid: str, enabled: bool) -> dict[str, Any]:
        setup = self.setup(ref)
        dev = None
        if enabled:
            dev = await self._agent_call(self._device(setup, busid))
        self.store.set_sticky(setup["id"], busid, enabled, dev)
        if enabled:
            result = await self.attach(setup["id"], busid)
            return {"message": "keep-attached on. " + result["message"], "sticky": True}
        self.sticky_state.pop(self._sticky_key(setup["id"], busid), None)
        return {"message": "keep-attached off", "sticky": False}

    async def set_auto_bind(self, ref: str, busid: str, enabled: bool) -> dict[str, Any]:
        """Ask the agent to bind this device by itself whenever it is plugged in."""
        setup = self.setup(ref)
        c = self.client(setup)
        dev = await self._agent_call(self._device(setup, busid))
        cfg = await self._agent_call(c.get_config())
        rules = [r for r in cfg.get("auto_bind", [])]
        rule = f"{dev['vid']}:{dev['pid']}:{dev['serial']}" if dev.get("serial") else busid
        if enabled:
            if rule not in rules:
                rules.append(rule)
        else:
            rules = [r for r in rules if not match_rule(r, dev)]
        await self._agent_call(c.update_config({"auto_bind": rules}))
        if enabled and not dev["bound"]:
            await self.bind(setup["id"], busid)
        return {"message": f"auto-bind {'on' if enabled else 'off'} ({rule})", "auto_bind": rules}

    async def _sticky_loop(self) -> None:
        while True:
            await asyncio.sleep(4)
            try:
                await self._sticky_pass()
            except Exception:  # pragma: no cover
                log.exception("sticky loop error")

    async def _sticky_pass(self) -> None:
        items = self.store.sticky()
        if not items:
            return
        ports = await self.local_ports()
        for item in items:
            key = self._sticky_key(item["setup"], item["busid"])
            st = self.sticky_state.setdefault(key, {"retry_at": 0.0})
            if self._match_port(ports, item["setup"], item["busid"]):
                st.update({"ok": True, "error": None})
                continue
            if st.get("retry_at", 0) > time.time():
                continue
            try:
                setup = self.setup(item["setup"])
                busid = item["busid"]
                # The device can move to another port: follow it by serial number.
                if item.get("serial"):
                    data = await self.client(setup).usb()
                    for d in data.get("devices", []):
                        if d.get("serial") == item["serial"] and d["vidpid"] == item.get("vidpid"):
                            if d["busid"] != busid:
                                self.store.set_sticky(setup["id"], busid, False)
                                self.store.set_sticky(setup["id"], d["busid"], True, d)
                                busid = d["busid"]
                            break
                await self.attach(setup["id"], busid)
                st.update({"ok": True, "error": None, "last_attach": time.time()})
                log.info("keep-attached: re-attached %s", key)
            except (FleetError, AgentError) as e:
                msg = getattr(e, "message", str(e))
                st.update({"ok": False, "error": msg, "retry_at": time.time() + 10})

    def sticky_report(self) -> list[dict[str, Any]]:
        out = []
        for item in self.store.sticky():
            st = self.sticky_state.get(self._sticky_key(item["setup"], item["busid"]), {})
            out.append({**item, "ok": st.get("ok"), "error": st.get("error")})
        return out

    # -- MCU identification ----------------------------------------------------
    @staticmethod
    def device_key(sid: str, d: dict[str, Any]) -> str:
        """Results follow the device by serial number when it has one, else by its port (busid)."""
        return f"{sid}/{d.get('vid')}:{d.get('pid')}/{d.get('serial') or d.get('busid')}"

    def identify_profiles(self) -> dict[str, dict[str, Any]]:
        profiles = {k: dict(v, builtin=True) for k, v in mcu_detect.BUILTIN_PROFILES.items()}
        for k, v in (self.store.data().get("identify_profiles") or {}).items():
            profiles[k] = dict(v, builtin=False)
        return profiles

    def save_identify_profile(self, name: str, options: dict[str, Any] | None) -> None:
        """Save (or with options=None delete) one of your identify profiles."""
        name = name.strip()
        if not name:
            raise FleetError("the profile needs a name")
        if name in mcu_detect.BUILTIN_PROFILES:
            raise FleetError(f"'{name}' is a built-in profile. Save it with another name.")
        data = self.store.data()
        profiles = data.setdefault("identify_profiles", {})
        if options is None:
            profiles.pop(name, None)
        else:
            profiles[name] = {k: options.get(k) for k in IDENTIFY_KEYS if k in options}
        self.store.save()

    def identify_options(self, ref: str, busid: str, d: dict[str, Any] | None = None) -> dict[str, Any]:
        """The options to show in the identify dialog: the last ones used for this device, else Auto."""
        setup = self.setup(ref)
        last = (self.store.data().get("identify_last") or {}).get(self.device_key(setup["id"], d or {"busid": busid}))
        base = dict(mcu_detect.BUILTIN_PROFILES["Auto"], profile="Auto")
        base.pop("help", None)
        return dict(base, **(last or {}))

    async def identify(self, ref: str, busid: str, options: dict[str, Any] | None = None,
                       profile: str | None = None) -> dict[str, Any]:
        """Reset the MCU, capture its serial log and guess which MCU it is.

        A profile gives the default options. Options given here replace them.
        The run happens on the setup PC, or on this PC when the device is attached here.
        """
        setup = self.setup(ref)
        opts: dict[str, Any] = {}
        if profile:
            profiles = self.identify_profiles()
            if profile not in profiles:
                raise FleetError(f"unknown profile '{profile}' (known: {', '.join(profiles)})", 404)
            opts.update({k: v for k, v in profiles[profile].items() if k in IDENTIFY_KEYS})
        opts.update({k: v for k, v in (options or {}).items() if k in IDENTIFY_KEYS and v is not None})
        devs = await self.usb(setup["id"], include_hubs=True)
        dev = next((d for d in devs if d["busid"] == busid), None)
        if dev is None:
            raise FleetError(f"no USB device {busid} on {setup['name']}", 404)
        if dev.get("attached_here"):
            raw = await self._identify_local(setup, dev, opts)
            where = "this PC"
        elif dev["state"] == "in_use":
            raise FleetError(f"{busid} is attached to another client. Identify it there, or take it over.", 409)
        else:
            was_bound = bool(dev["bound"])
            if was_bound:
                await self.unbind(setup["id"], busid)  # the serial port must be on the setup PC
                await asyncio.sleep(1.0)
            try:
                raw = await self._agent_call(self.client(setup).identify(busid, opts))
            finally:
                if was_bound:
                    try:
                        await self.bind(setup["id"], busid)  # share it again, as it was
                    except FleetError as e:
                        log.warning("could not share %s again: %s", busid, e.message)
            where = setup["name"]
        script_out = "\n".join((s.get("stdout") or "") + (s.get("stderr") or "") for s in raw.get("scripts", []))
        guess = mcu_detect.analyze(dev, raw.get("log", ""), "\n".join(x for x in (raw.get("probe"), script_out) if x))
        stored = {k: guess[k] for k in ("mcu", "label", "confidence", "sure", "firmware")}
        stored.update({"evidence": guess["evidence"][:6], "at": time.time(), "busid": busid,
                       "reset": raw.get("reset"), "baud": raw.get("baud")})
        data = self.store.data()
        key = self.device_key(setup["id"], dev)
        data.setdefault("mcu_results", {})[key] = stored
        data.setdefault("identify_last", {})[key] = dict(opts, profile=profile or opts.get("profile") or "")
        self.store.save()
        log.info("identified %s:%s as %s (%.0f%%)", setup["id"], busid, guess["label"], 100 * guess["confidence"])
        return {"setup": setup["id"], "busid": busid, "where": where, "guess": guess, "capture": raw,
                "device": {k: dev.get(k) for k in ("busid", "name", "vidpid", "serial", "ttys")}}

    async def _identify_local(self, setup: dict[str, Any], dev: dict[str, Any], opts: dict[str, Any]) -> dict:
        """Identify a device that is attached to this PC: use its local serial port(s)."""
        from .agent import setup_agent as sa  # the same capture code as the agent

        ports = await self.local_ports()
        port = self._match_port(ports, setup["id"], dev["busid"])
        local_busid = port.get("local_busid") if port else None
        if not local_busid:
            raise FleetError("cannot find the local USB port of the attached device", 500)
        usb = sa.UsbManager({"sysfs_root": "/sys", "protected": [], "auto_bind": [], "usbip_cmd": "usbip"})
        try:
            local = usb.get(local_busid)
        except sa.ApiError as e:
            raise FleetError(e.message, e.code) from None
        paths = [f"/dev/{t}" for t in local["ttys"]]
        if opts.get("tty"):
            paths = [p for p in paths if os.path.basename(p) == os.path.basename(str(opts["tty"]))]

        def run_script(script: str, env: dict[str, str], timeout: float) -> dict[str, Any]:
            env = dict(os.environ, SM_SETUP=setup["name"], SM_WHERE="manager", **env)
            try:
                p = subprocess.run(["bash", "-c", script], capture_output=True, env=env, timeout=timeout,
                                   stdin=subprocess.DEVNULL)
                return {"exit_code": p.returncode, "stdout": p.stdout.decode(errors="replace"),
                        "stderr": p.stderr.decode(errors="replace"), "timed_out": False}
            except subprocess.TimeoutExpired as e:
                return {"exit_code": None, "stdout": (e.stdout or b"").decode(errors="replace"),
                        "stderr": "", "timed_out": True}

        def hw_reset(method: str) -> str:
            if method == "usb":
                return "USB reset is not available for an attached device (detach it first)"
            return sa.run_probe(dev, "reset")[1]

        def go() -> dict[str, Any]:
            r = sa.identify_capture(dev, paths, opts.get("baud", "auto"), opts.get("duration", 4),
                                    opts.get("reset", "auto"), opts.get("script") or "",
                                    opts.get("after_script") or "", run_script, hw_reset,
                                    float(opts.get("script_timeout") or 30))
            r["probe"] = sa.run_probe(dev, "info")[1]
            return r

        try:
            return await asyncio.get_running_loop().run_in_executor(None, go)
        except sa.ApiError as e:
            raise FleetError(e.message, e.code) from None

    def forget_mcu(self, ref: str, d: dict[str, Any]) -> None:
        setup = self.setup(ref)
        (self.store.data().get("mcu_results") or {}).pop(self.device_key(setup["id"], d), None)
        self.store.save()

    # -- who uses a device, nicknames ---------------------------------------------
    @property
    def client_name(self) -> str:
        name = self.store.data().get("client_name")
        if name:
            return str(name)
        try:
            user = getpass.getuser()
        except Exception:  # noqa: BLE001
            user = "user"
        return f"{user}@{socket.gethostname()}"

    async def _claim(self, setup: dict[str, Any], busid: str) -> None:
        try:
            await self.client(setup).claim(busid, self.client_name)
        except AgentError as e:
            log.debug("claim of %s failed: %s", busid, e.message)

    async def set_nickname(self, ref: str, busid: str, name: str) -> dict[str, Any]:
        """Give a device a name (stored on the setup PC, so all managers see it). Empty name = remove."""
        r = await self._agent_call(self.client(ref).set_nickname(busid, name))
        return {"message": f"renamed to {name}" if name else "name removed", "device": r.get("device")}

    # -- serial monitor ------------------------------------------------------------
    def _local_ttys(self, port: dict[str, Any]) -> list[str]:
        """The serial ports on this PC of an attached device."""
        if not port.get("local_busid"):
            return []
        try:
            if self._local_usb is None:
                from .agent import setup_agent as sa
                self._local_usb = sa.UsbManager({"sysfs_root": "/sys", "protected": [], "auto_bind": [],
                                                 "usbip_cmd": "usbip"})
            return list(self._local_usb.get(port["local_busid"])["ttys"])
        except Exception:  # noqa: BLE001 - not Linux, or the port is gone
            return []

    @property
    def local_hub(self) -> Any:
        if self._local_hub is None:
            from .agent import setup_agent as sa
            self._local_hub = sa.SerialHub()
        return self._local_hub

    async def serial_target(self, ref: str, busid: str | None = None, tty: str | None = None,
                            refresh: bool = False) -> dict[str, Any]:
        """Where a serial port is: on the setup PC ("setup"), or on this PC for an attached device ("here")."""
        setup = self.setup(ref)
        key = f"{setup['id']}/{busid or ''}/{tty or ''}"
        if key in self.serial_targets and not refresh:
            return self.serial_targets[key]
        target: dict[str, Any] = {"setup": setup["id"], "busid": busid, "tty": tty, "where": "setup"}
        if busid:
            devs = await self.usb(setup["id"], include_hubs=True)
            dev = next((d for d in devs if d["busid"] == busid), None)
            if dev is None:
                raise FleetError(f"no USB device {busid} on {setup['name']}", 404)
            target["name"] = dev.get("display_name") or dev["name"]
            if dev.get("attached_here"):
                ttys = dev.get("local_ttys") or []
                if not ttys:
                    raise FleetError(f"{busid} is attached here but has no serial port on this PC", 404)
                local = tty if tty in ttys else ttys[0]
                target.update({"where": "here", "path": f"/dev/{local}", "tty": local})
            elif dev["state"] == "in_use":
                raise FleetError(f"{busid} is in use by {used_by_text(dev)}: its serial port is on that PC", 409)
            elif dev["bound"]:
                raise FleetError(f"{busid} is shared over USB/IP. Stop sharing it to use its serial port here.",
                                 409)
            elif not dev["ttys"]:
                raise FleetError(f"{busid} has no serial port", 404)
            else:
                target["tty"] = tty if tty in dev["ttys"] else dev["ttys"][0]
        elif not tty:
            raise FleetError("give a busid or a tty")
        self.serial_targets[key] = target
        return target

    async def _local(self, fn: Any, *args: Any) -> Any:
        from .agent import setup_agent as sa
        try:
            return await asyncio.get_running_loop().run_in_executor(None, fn, *args)
        except sa.ApiError as e:
            raise FleetError(e.message, e.code) from None

    async def serial_open(self, ref: str, busid: str | None = None, tty: str | None = None,
                          baud: int | None = 115200) -> dict[str, Any]:
        t = await self.serial_target(ref, busid, tty, refresh=True)
        if t["where"] == "here":
            info = await self._local(lambda: self.local_hub.open(t["path"], baud).info())
        else:
            info = await self._agent_call(self.client(ref).serial_open(None, t["tty"], baud))
        return dict(info, setup=t["setup"], busid=busid, where=t["where"], name=t.get("name"))

    async def serial_read(self, ref: str, busid: str | None = None, tty: str | None = None, since: int = 0,
                          wait: float = 0, max_bytes: int = 65536) -> dict[str, Any]:
        t = await self.serial_target(ref, busid, tty)
        if t["where"] == "here":
            def go() -> dict[str, Any]:
                return self.local_hub.get(t["path"]).read(since, max_bytes, wait)
            r = await self._local(go)
        else:
            r = await self._agent_call(self.client(ref).serial_read(None, t["tty"], since, wait, max_bytes))
        r["tty"] = t["tty"]
        return r

    async def serial_write(self, ref: str, busid: str | None, tty: str | None, data: bytes) -> dict[str, Any]:
        t = await self.serial_target(ref, busid, tty)
        if t["where"] == "here":
            await self._local(lambda: self.local_hub.get(t["path"]).write(data))
            return {"written": len(data)}
        return await self._agent_call(self.client(ref).serial_write(None, t["tty"], data))

    async def serial_close(self, ref: str, busid: str | None = None, tty: str | None = None) -> dict[str, Any]:
        t = await self.serial_target(ref, busid, tty)
        if t["where"] == "here":
            return await self._local(lambda: self.local_hub.close(t["path"]))
        return await self._agent_call(self.client(ref).serial_close(None, t["tty"]))

    async def serial_reset(self, ref: str, busid: str | None, tty: str | None,
                           method: str = "auto") -> dict[str, Any]:
        t = await self.serial_target(ref, busid, tty)
        if t["where"] == "here":
            from .agent import setup_agent as sa
            s = self.local_hub.get(t["path"])
            cap = sa.HubCapture(self.local_hub, [t["path"]], s.baud)
            return {"notes": await self._local(lambda: sa.do_reset(
                "rts+dtr" if method == "auto" else method, cap, None))}
        return await self._agent_call(self.client(ref).serial_reset(busid, t["tty"], method))

    async def serial_wait_for(self, ref: str, busid: str | None, tty: str | None, pattern: str,
                              timeout: float = 30, since: int | None = None) -> dict[str, Any]:
        """Wait until the serial output matches a regular expression."""
        try:
            rx = re.compile(pattern)
        except re.error as e:
            raise FleetError(f"bad pattern: {e}") from None
        if since is None or since < 0:
            since = (await self.serial_read(ref, busid, tty, since=-1))["offset"]
        text, offset = "", since
        deadline = time.time() + max(0.1, timeout)
        while True:
            left = deadline - time.time()
            r = await self.serial_read(ref, busid, tty, since=offset, wait=max(0.0, min(5.0, left)))
            offset = r["offset"]
            text += r["data"].decode("utf-8", "replace")
            m = rx.search(text)
            if m:
                return {"matched": True, "match": m.group(0), "groups": list(m.groups()),
                        "text": text[-8000:], "offset": offset}
            if time.time() >= deadline:
                return {"matched": False, "text": text[-8000:], "offset": offset}

    async def serial_ports(self, ref: str) -> list[dict[str, Any]]:
        """All serial ports of a setup that can be opened from here."""
        setup = self.setup(ref)
        out = []
        for d in await self.usb(setup["id"]):
            if d.get("attached_here"):
                for t in d.get("local_ttys") or []:
                    out.append({"busid": d["busid"], "tty": t, "where": "here", "name": d["display_name"]})
            elif not d["bound"]:
                for t in d.get("ttys") or []:
                    out.append({"busid": d["busid"], "tty": t, "where": "setup", "name": d["display_name"]})
        return out

    # -- files and flashing ----------------------------------------------------------
    async def upload(self, ref: str, local_path: str, remote_path: str | None = None) -> dict[str, Any]:
        if not os.path.isfile(local_path):
            raise FleetError(f"no file {local_path}", 404)
        return await self._agent_call(self.client(ref).upload(local_path,
                                                              remote_path or os.path.basename(local_path)))

    async def download(self, ref: str, remote_path: str, local_path: str | None = None) -> dict[str, Any]:
        local_path = local_path or os.path.basename(remote_path)
        if os.path.isdir(local_path):
            local_path = os.path.join(local_path, os.path.basename(remote_path))
        return await self._agent_call(self.client(ref).download(remote_path, local_path))

    async def files(self, ref: str, path: str = ".") -> dict[str, Any]:
        return await self._agent_call(self.client(ref).files_list(path))

    def flash_profiles(self) -> dict[str, dict[str, Any]]:
        profiles = {k: dict(v, builtin=True) for k, v in flash_profiles.BUILTIN_FLASH_PROFILES.items()}
        for k, v in (self.store.data().get("flash_profiles") or {}).items():
            profiles[k] = dict(v, builtin=False)
        return profiles

    def save_flash_profile(self, name: str, options: dict[str, Any] | None) -> None:
        name = name.strip()
        if not name:
            raise FleetError("the profile needs a name")
        if name in flash_profiles.BUILTIN_FLASH_PROFILES:
            raise FleetError(f"'{name}' is a built-in profile. Save it with another name.")
        profiles = self.store.data().setdefault("flash_profiles", {})
        if options is None:
            profiles.pop(name, None)
        else:
            profiles[name] = {k: options.get(k) for k in FLASH_KEYS if k in options}
        self.store.save()

    def flash_options(self, ref: str, d: dict[str, Any]) -> dict[str, Any]:
        """The options for the flash dialog: the last ones used for this device, else a suggested profile."""
        setup = self.setup(ref)
        last = (self.store.data().get("flash_last") or {}).get(self.device_key(setup["id"], d))
        if last:
            return dict(last)
        name = flash_profiles.suggest_profile(d)
        p = self.flash_profiles()[name]
        return dict({k: p.get(k) for k in FLASH_KEYS if k in p}, profile=name)

    async def flash(self, ref: str, busid: str, file: str | None = None, profile: str | None = None,
                    options: dict[str, Any] | None = None) -> dict[str, Any]:
        """Flash a firmware file (a path on this PC) with a profile script.

        The file is uploaded to the setup PC and the script runs there. For a device that
        is attached here, the script runs on this PC. With monitor_seconds, the result
        also has the serial output after the flash.
        """
        setup = self.setup(ref)
        opts: dict[str, Any] = {}
        if profile:
            profiles = self.flash_profiles()
            if profile not in profiles:
                raise FleetError(f"unknown flash profile '{profile}' (known: {', '.join(profiles)})", 404)
            opts.update({k: v for k, v in profiles[profile].items() if k in FLASH_KEYS})
        for k, v in (options or {}).items():
            if k == "env" and isinstance(v, dict):
                opts["env"] = dict(opts.get("env") or {}, **v)
            elif k in FLASH_KEYS and v is not None:
                opts[k] = v
        if not (opts.get("script") or "").strip():
            raise FleetError("no flash script: select a profile or give a script")
        if file:
            file = os.path.abspath(os.path.expanduser(file))
            if not os.path.isfile(file):
                raise FleetError(f"no file {file}", 404)
        devs = await self.usb(setup["id"], include_hubs=True)
        dev = next((d for d in devs if d["busid"] == busid), None)
        if dev is None:
            raise FleetError(f"no USB device {busid} on {setup['name']}", 404)
        if dev.get("attached_here"):
            r = await self._flash_local(setup, dev, file, opts)
            where = "this PC"
        elif dev["state"] == "in_use":
            raise FleetError(f"{busid} is in use by {used_by_text(dev)}. Flash it there, or take it over.", 409)
        else:
            remote = None
            if file:
                up = await self.upload(setup["id"], file, f"flash/{busid}/{os.path.basename(file)}")
                remote = up["path"]
            was_bound = bool(dev["bound"])
            if was_bound:
                await self.unbind(setup["id"], busid)
                await asyncio.sleep(1.0)
            body = {k: opts.get(k) for k in ("script", "env", "timeout", "monitor_seconds", "baud", "user", "tty")}
            body["file"] = remote
            try:
                r = await self._agent_call(self.client(setup).flash(busid, body))
            finally:
                if was_bound:
                    try:
                        await self.bind(setup["id"], busid)
                    except FleetError as e:
                        log.warning("could not share %s again: %s", busid, e.message)
            where = setup["name"]
        data = self.store.data()
        data.setdefault("flash_last", {})[self.device_key(setup["id"], dev)] = dict(
            {k: opts.get(k) for k in FLASH_KEYS if k in opts}, profile=profile or opts.get("profile") or "",
            file=file)
        self.store.save()
        ok = r.get("exit_code") == 0 and not r.get("timed_out")
        log.info("flash %s:%s %s", setup["id"], busid, "ok" if ok else f"failed (rc={r.get('exit_code')})")
        return dict(r, setup=setup["id"], busid=busid, where=where, ok=ok, file=file)

    async def _flash_local(self, setup: dict[str, Any], dev: dict[str, Any], file: str | None,
                           opts: dict[str, Any]) -> dict[str, Any]:
        ttys = dev.get("local_ttys") or []
        paths = [f"/dev/{t}" for t in ttys]
        env = dict(os.environ, SM_FILE=file or "", SM_FILE_EXT=os.path.splitext(file or "")[1].lstrip(".").lower(),
                   SM_TTY=paths[0] if paths else "", SM_TTYS=" ".join(paths), SM_BUSID=dev["busid"],
                   SM_VID=dev["vid"], SM_PID=dev["pid"], SM_SERIAL=dev.get("serial") or "",
                   SM_SETUP=setup["name"], SM_PHASE="flash", SM_WHERE="manager",
                   **{str(k): str(v) for k, v in (opts.get("env") or {}).items()})
        hub = self.local_hub
        timeout = float(opts.get("timeout") or 300)

        def go() -> dict[str, Any]:
            paused = hub.pause(paths, "paused for flashing")
            starts = {s.path: s.end for s in paused}
            t0 = time.time()
            try:
                p = subprocess.run(["bash", "-c", opts["script"]], capture_output=True, env=env, timeout=timeout,
                                   stdin=subprocess.DEVNULL)
                r = {"exit_code": p.returncode, "stdout": p.stdout.decode(errors="replace")[-200000:],
                     "stderr": p.stderr.decode(errors="replace")[-200000:], "timed_out": False}
            except subprocess.TimeoutExpired as e:
                r = {"exit_code": None, "stdout": (e.stdout or b"").decode(errors="replace"), "stderr": "",
                     "timed_out": True}
            finally:
                for s in paused:
                    s.resume()
            r["duration"] = round(time.time() - t0, 2)
            secs = float(opts.get("monitor_seconds") or 0)
            if secs > 0 and paths:
                try:
                    s = hub.open(paths[0], opts.get("baud"))
                    start = starts.get(paths[0], s.end)
                    time.sleep(min(120.0, secs))
                    m = s.read(start, 256 * 1024, 0)
                    r["monitor"] = {"tty": ttys[0], "baud": s.baud, "offset": m["offset"],
                                    "text": m["data"].decode("utf-8", "replace")}
                except Exception as e:  # noqa: BLE001
                    r["monitor"] = {"error": str(e)}
            return r

        return await self._local(go)

    # -- commands --------------------------------------------------------------
    async def exec(self, ref: str, command: str, timeout: float = 60, cwd: str | None = None,
                   user: str | None = None) -> dict[str, Any]:
        setup = self.setup(ref)
        r = await self._agent_call(self.client(setup).exec(command, timeout, cwd, user))
        r["setup"] = setup["id"]
        return r

    async def exec_many(self, refs: list[str] | None, command: str, timeout: float = 60) -> list[dict[str, Any]]:
        targets = [self.setup(r) for r in refs] if refs else self.store.setups()

        async def one(s: dict[str, Any]) -> dict[str, Any]:
            try:
                return await self.exec(s["id"], command, timeout)
            except FleetError as e:
                return {"setup": s["id"], "error": e.message, "exit_code": None}

        return list(await asyncio.gather(*(one(s) for s in targets)))

    # -- other -----------------------------------------------------------------
    async def status(self, ref: str) -> dict[str, Any]:
        setup = self.setup(ref)
        return await self._agent_call(self.client(setup).status())

    async def agent_log(self, ref: str, n: int = 200) -> dict[str, Any]:
        return await self._agent_call(self.client(ref).log(n))

    async def start_usbipd(self, ref: str) -> dict[str, Any]:
        return await self._agent_call(self.client(ref).start_usbipd())

    async def find_usb(self, query: str = "") -> list[dict[str, Any]]:
        """Search USB devices on all online setups (name, vid:pid, serial, busid, tty)."""
        q = query.strip().lower()
        results: list[dict[str, Any]] = []

        async def one(s: dict[str, Any]) -> None:
            try:
                devs = await self.usb(s["id"])
            except (FleetError, AgentError):
                return
            for d in devs:
                hay = " ".join(str(x or "") for x in (d["name"], d.get("nickname"), d["vidpid"], d.get("serial"),
                                                     d["busid"], (d.get("mcu") or {}).get("mcu"),
                                                     d.get("manufacturer"), " ".join(d.get("ttys", [])))).lower()
                if not q or all(part in hay for part in q.split()):
                    results.append({"setup": s["id"], "setup_name": s["name"], **d})

        await asyncio.gather(*(one(s) for s in self.store.setups()))
        return results

    async def discover(self, timeout: float = 1.5) -> list[dict[str, Any]]:
        """Broadcast a UDP discovery packet and collect the agents that answer."""
        data = self.store.data()
        port = int(data.get("discovery_port") or 8765)
        found: dict[str, dict[str, Any]] = {}
        loop = asyncio.get_running_loop()

        class Proto(asyncio.DatagramProtocol):
            def datagram_received(self, payload: bytes, addr: tuple) -> None:
                try:
                    info = json.loads(payload.decode())
                except ValueError:
                    return
                if info.get("service") != "setup-agent":
                    return
                info["host"] = addr[0]
                found[f"{addr[0]}:{info.get('port')}"] = info

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.bind(("", 0))
        transport, _ = await loop.create_datagram_endpoint(Proto, sock=sock)
        try:
            for _ in range(2):
                for target in data.get("discovery_targets") or ["255.255.255.255"]:
                    try:
                        transport.sendto(DISCOVERY_MAGIC, (target, port))
                    except OSError as e:
                        log.warning("discovery send to %s failed: %s", target, e)
                await asyncio.sleep(timeout / 2)
        finally:
            transport.close()
        setups = self.store.setups()
        result = []
        for info in found.values():
            keys_match = None
            for s in setups:
                if info["host"] in await self._host_keys(s["host"]) and self.store.agent_port(s) == info.get("port"):
                    keys_match = s["id"]
            info["known_as"] = keys_match
            result.append(info)
        return sorted(result, key=lambda x: (x.get("name") or "", x["host"]))

    # -- setups CRUD -----------------------------------------------------------
    async def add_setup(self, fields: dict[str, Any], test: bool = True) -> dict[str, Any]:
        try:
            s = self.store.add_setup(fields)
        except ConfigError as e:
            raise FleetError(str(e)) from None
        entry = await self.refresh(s["id"]) if test else {}
        return {"setup": ConfigStore.redact(s), "online": entry.get("online"), "error": entry.get("error")}

    async def update_setup(self, sid: str, fields: dict[str, Any]) -> dict[str, Any]:
        try:
            s = self.store.update_setup(sid, fields)
        except ConfigError as e:
            raise FleetError(str(e), 404) from None
        self._resolve_cache.pop(s["host"], None)
        entry = await self.refresh(s["id"])
        return {"setup": ConfigStore.redact(s), "online": entry.get("online"), "error": entry.get("error")}

    def remove_setup(self, sid: str) -> None:
        try:
            self.store.remove_setup(sid)
        except ConfigError as e:
            raise FleetError(str(e), 404) from None
        self.cache.pop(sid, None)
