"""Fleet: the core of the manager. The GUI, the CLI and the MCP server all use it."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import socket
import time
from typing import Any

import aiohttp

from .agent_client import AgentClient, AgentError
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

    def client(self, ref: str | dict[str, Any]) -> AgentClient:
        setup = ref if isinstance(ref, dict) else self.setup(ref)
        assert self.session is not None, "Fleet.open() was not called"
        return AgentClient(self.session, setup, self.store.agent_port(setup), self.store.agent_token(setup))

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
        for p in ports:
            p["setup"] = None
            host = (p.get("host") or "").lower()
            for s, k in zip(setups, keys):
                if host in k and (p.get("remote_port") or 3240) == s.get("usbip_port", 3240):
                    p["setup"] = s["id"]
                    p["setup_name"] = s["name"]
                    # `usbip port` often has no names (no usb.ids): use the agent's name.
                    for d in self.cache.get(s["id"], {}).get("usb") or []:
                        if d["busid"] == p.get("busid"):
                            p["name"] = d["name"]
                            p["serial"] = d.get("serial")
                    break
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
        for d in devices:
            p = self._match_port(ports, setup["id"], d["busid"])
            d["attached_here"] = p is not None
            d["local_port"] = p["port"] if p else None
            d["sticky"] = d["busid"] in sticky
            if d["state"] == "in_use" and not p:
                d["state_detail"] = "in use by another client"
            elif p:
                d["state_detail"] = f"attached to this PC (port {p['port']})"
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
                    raise FleetError(f"{busid} ({dev['name']}) is in use by another client. Use force to take it.",
                                     409)
                await self.unbind(setup["id"], busid)
                dev["bound"] = False
            if not dev["bound"]:
                await self.bind(setup["id"], busid, force=force)
            try:
                out = await self.local.attach(setup["host"], busid, setup.get("usbip_port", 3240))
            except UsbipError as e:
                raise FleetError(f"attach failed: {e}", 500) from None
            for _ in range(24):
                ports = await self.local_ports()
                p = self._match_port(ports, setup["id"], busid)
                if p:
                    break
                await asyncio.sleep(0.25)
            log.info("attached %s:%s (%s)", setup["id"], busid, dev["name"])
            return {"message": out or "attached", "port": p, "device": dev}

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
                hay = " ".join(str(x or "") for x in (d["name"], d["vidpid"], d.get("serial"), d["busid"],
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
