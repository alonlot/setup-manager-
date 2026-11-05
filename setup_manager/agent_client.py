"""Async HTTP client for one setup agent."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import ssl as _ssl
import time
import urllib.parse
from typing import Any

import aiohttp


class AgentError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.message = message
        self.status = status


def fingerprint_of(der: bytes) -> str:
    return hashlib.sha256(der).hexdigest()


def clean_fingerprint(fp: str | None) -> str:
    """'AB:CD:..' or 'abcd..' -> 'abcd..' (64 hex characters), or '' when it is not a SHA-256 fingerprint."""
    fp = (fp or "").replace(":", "").replace(" ", "").strip().lower()
    return fp if len(fp) == 64 and all(c in "0123456789abcdef" for c in fp) else ""


PLAIN_RECHECK = 120  # seconds: how often to check again if a plain-HTTP agent has TLS now


class AgentClient:
    """One agent. The connection is TLS with a pinned certificate (fingerprint), like SSH:

    * the setup has a fingerprint (from `deploy`, or saved at the first connection): TLS, and the
      certificate must have this fingerprint, else the connection is refused;
    * no fingerprint yet: the first connection reads the certificate and saves its fingerprint
      (on_pin). An agent without TLS (an old agent) works without encryption, unless
      require_tls is set or the setup has transport "tls".
    """

    def __init__(self, session: aiohttp.ClientSession, setup: dict[str, Any], port: int, token: str,
                 transport_cache: dict[str, tuple[str, float]] | None = None, on_pin: Any = None,
                 require_tls: bool = False):
        self.session = session
        self.setup = setup
        self.port = port
        self.token = token
        self.on_pin = on_pin
        self.cache = transport_cache if transport_cache is not None else {}
        self.require_tls = require_tls
        host = setup["host"]
        self._host = f"[{host}]" if ":" in host and not host.startswith("[") else host
        self.fingerprint = clean_fingerprint(setup.get("fingerprint"))
        transport = setup.get("transport") or ("tls" if setup.get("tls") else "auto")
        self.transport = transport
        self.mode: str | None = None
        self._ssl_param: Any = None
        if self.fingerprint or setup.get("verify_tls"):
            self.mode = "tls"
        elif transport == "plain":
            self.mode = "plain"
        else:
            # No pin yet (also with transport "tls"): the first connection probes and pins the
            # certificate. A cached "tls" without a pin would skip that and verify nothing.
            hit = self.cache.get(setup.get("id", ""))
            if hit and hit[0] == "plain" and time.time() - hit[1] < PLAIN_RECHECK:
                self.mode = "plain"

    @property
    def encrypted(self) -> bool:
        return self.mode == "tls"

    @property
    def base(self) -> str:
        return f"{'https' if self.mode == 'tls' else 'http'}://{self._host}:{self.port}"

    @property
    def ws_base(self) -> str:
        return f"{'wss' if self.mode == 'tls' else 'ws'}://{self._host}:{self.port}"

    @property
    def ssl(self) -> Any:
        if self.mode != "tls":
            return True  # not used for http:// URLs
        if self.fingerprint:
            if self._ssl_param is None:  # one object: aiohttp pools connections by it
                self._ssl_param = aiohttp.Fingerprint(bytes.fromhex(self.fingerprint))
            return self._ssl_param
        if self.setup.get("verify_tls"):
            return True  # a certificate from a real CA
        return False

    async def ensure(self) -> None:
        """Find out if the agent uses TLS (and save its certificate) before the first request."""
        if self.mode is None:
            await self._probe()
        if self.mode == "plain" and (self.require_tls or self.transport == "tls"):
            raise AgentError(f"{self.setup['name']} does not use encryption (agent older than 0.4, or installed "
                             f"with --plain). Fix: setup-manager deploy USER@{self.setup['host']}", 495)

    async def _probe(self) -> None:
        ctx = _ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = _ssl.CERT_NONE
        host = self.setup["host"].strip("[]")
        where = f"{self.setup['name']} ({self.setup['host']}:{self.port})"
        try:
            _r, w = await asyncio.wait_for(asyncio.open_connection(host, self.port, ssl=ctx), 6)
        except _ssl.SSLError as e:
            # It answered, but not with TLS: an agent before version 0.4? Make sure that it speaks
            # plain HTTP before we trust that (a broken handshake must not turn encryption off).
            if not await self._answers_http(host):
                raise AgentError(f"TLS handshake with {where} failed: {e}") from None
            self.mode = "plain"
        except (OSError, asyncio.TimeoutError) as e:
            raise AgentError(f"cannot reach {where}: {e}") from None
        else:
            der = w.get_extra_info("ssl_object").getpeercert(binary_form=True)
            w.close()
            try:
                await asyncio.wait_for(w.wait_closed(), 2)
            except (OSError, asyncio.TimeoutError, _ssl.SSLError):
                pass
            self.fingerprint = fingerprint_of(der)
            self._ssl_param = None
            self.mode = "tls"
            if self.on_pin:
                self.on_pin(self.fingerprint)
        self.cache[self.setup.get("id", "")] = (self.mode, time.time())

    async def _answers_http(self, host: str) -> bool:
        try:
            r, w = await asyncio.wait_for(asyncio.open_connection(host, self.port), 6)
        except (OSError, asyncio.TimeoutError):
            return False
        try:
            w.write(b"GET /api/ping HTTP/1.0\r\nHost: %s\r\n\r\n" % host.encode("ascii", "ignore"))
            await w.drain()
            head = await asyncio.wait_for(r.read(5), 6)
            return head == b"HTTP/"
        except (OSError, asyncio.TimeoutError):
            return False
        finally:
            w.close()

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    async def request(self, method: str, path: str, body: dict | None = None,
                      params: dict | None = None, timeout: float = 10) -> Any:
        await self.ensure()
        url = self.base + path
        try:
            async with self.session.request(method, url, json=body, params=params, headers=self.headers,
                                            ssl=self.ssl, timeout=aiohttp.ClientTimeout(total=timeout)) as r:
                try:
                    data = await r.json(content_type=None)
                except (ValueError, aiohttp.ContentTypeError):
                    data = {"error": (await r.text())[:500]}
                if r.status >= 400:
                    msg = data.get("error") if isinstance(data, dict) else None
                    if r.status == 401:
                        msg = "agent rejected the token (check the token of this setup)"
                    elif r.status == 404 and (msg or "").startswith("not found:"):
                        msg = (f"the agent on {self.setup['name']} is too old for this ({path}). Update it: "
                               f"setup-manager deploy USER@{self.setup['host']}")
                    raise AgentError(msg or f"HTTP {r.status}", r.status)
                return data
        except asyncio.TimeoutError:
            raise AgentError(f"timeout talking to {self.setup['name']} ({self.base})") from None
        except aiohttp.ServerFingerprintMismatch:
            raise AgentError(self.mismatch_message(), 495) from None
        except aiohttp.ClientError as e:
            raise AgentError(f"cannot reach {self.setup['name']} ({self.base}): {e}") from None

    def mismatch_message(self) -> str:
        return (f"the TLS certificate of {self.setup['name']} changed! This can be an attack. If you reinstalled "
                f"the agent on {self.setup['host']}, trust the new certificate: setup-manager trust "
                f"{self.setup.get('id', self.setup['host'])} --forget (or Edit setup > clear the fingerprint)")

    async def ping(self) -> dict:
        return await self.request("GET", "/api/ping", timeout=4)

    async def status(self) -> dict:
        return await self.request("GET", "/api/status", timeout=6)

    async def usb(self, include_hubs: bool = False) -> dict:
        return await self.request("GET", "/api/usb", params={"all": "1"} if include_hubs else None, timeout=8)

    async def bind(self, busid: str, force: bool = False, restore_owner: bool = False) -> dict:
        body: dict[str, Any] = {"busid": busid, "force": force}
        if restore_owner:
            body["restore_owner"] = True
        return await self.request("POST", "/api/usb/bind", body, timeout=30)

    async def usbip_allow(self, busid: str) -> dict:
        """Plain USB/IP: the agent lets this PC's IP connect to usbipd for a short time."""
        return await self.request("POST", "/api/usb/allow", {"busid": busid}, timeout=15)

    async def unbind(self, busid: str) -> dict:
        return await self.request("POST", "/api/usb/unbind", {"busid": busid}, timeout=30)

    async def reset(self, busid: str) -> dict:
        return await self.request("POST", "/api/usb/reset", {"busid": busid}, timeout=30)

    async def identify(self, busid: str, options: dict) -> dict:
        body = dict(options, busid=busid)
        attempts = 1 if str(options.get("baud", "auto")) != "auto" else 4
        wait = attempts * (float(options.get("duration") or 4) + 2 * float(options.get("script_timeout") or 30)) + 30
        return await self.request("POST", "/api/usb/identify", body, timeout=min(wait, 900))

    # -- serial monitor --------------------------------------------------------
    @staticmethod
    def _port(busid: str | None, tty: str | None) -> dict:
        return {k: v for k, v in (("busid", busid), ("tty", tty)) if v}

    async def serial_open(self, busid: str | None, tty: str | None, baud: int | None) -> dict:
        return await self.request("POST", "/api/serial/open", dict(self._port(busid, tty), baud=baud))

    async def serial_read(self, busid: str | None, tty: str | None, since: int, wait: float = 0,
                          max_bytes: int = 65536) -> dict:
        params = dict(self._port(busid, tty), since=str(since), wait=str(wait), max=str(max_bytes))
        r = await self.request("GET", "/api/serial/read", params=params, timeout=wait + 10)
        r["data"] = base64.b64decode(r.get("data") or "")
        return r

    async def serial_write(self, busid: str | None, tty: str | None, data: bytes) -> dict:
        return await self.request("POST", "/api/serial/write",
                                  dict(self._port(busid, tty), data_b64=base64.b64encode(data).decode()))

    async def serial_reset(self, busid: str | None, tty: str | None, method: str = "auto") -> dict:
        return await self.request("POST", "/api/serial/reset", dict(self._port(busid, tty), method=method),
                                  timeout=30)

    async def serial_close(self, busid: str | None, tty: str | None) -> dict:
        return await self.request("POST", "/api/serial/close", self._port(busid, tty))

    async def serial_list(self) -> dict:
        return await self.request("GET", "/api/serial")

    # -- files, flash, claims, names ---------------------------------------------
    async def upload(self, local_path: str, remote_path: str) -> dict:
        size = os.path.getsize(local_path)
        await self.ensure()
        try:
            with open(local_path, "rb") as f:
                async with self.session.put(self.base + "/api/files", params={"path": remote_path}, data=f,
                                            headers=dict(self.headers, **{"Content-Length": str(size)}),
                                            ssl=self.ssl, timeout=aiohttp.ClientTimeout(total=600)) as r:
                    if r.status >= 400:
                        raise AgentError(await self._error_text(r), r.status)
                    return await r.json(content_type=None)
        except aiohttp.ServerFingerprintMismatch:
            raise AgentError(self.mismatch_message(), 495) from None
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
            raise AgentError(f"upload to {self.setup['name']} failed: {e}") from None

    @staticmethod
    async def _error_text(r: aiohttp.ClientResponse) -> str:
        """The "error" of a JSON answer, else the text of the body (an HTML error page, a proxy)."""
        try:
            data = await r.json(content_type=None)
        except (ValueError, aiohttp.ContentTypeError):
            data = None
        if isinstance(data, dict) and data.get("error"):
            return str(data["error"])
        try:
            text = (await r.text()).strip()
        except (aiohttp.ClientError, UnicodeDecodeError):
            text = ""
        return f"HTTP {r.status}" + (f": {text[:300]}" if text and not text.startswith("<") else "")

    async def download(self, remote_path: str, local_path: str) -> dict:
        await self.ensure()
        try:
            async with self.session.get(self.base + "/api/files", params={"path": remote_path},
                                        headers=self.headers, ssl=self.ssl,
                                        timeout=aiohttp.ClientTimeout(total=600)) as r:
                if r.status >= 400:
                    raise AgentError(await self._error_text(r), r.status)
                size = 0
                part = local_path + ".part"
                try:
                    with open(part, "wb") as f:
                        async for chunk in r.content.iter_chunked(1024 * 1024):
                            f.write(chunk)
                            size += len(chunk)
                    os.replace(part, local_path)
                except BaseException:
                    try:
                        os.unlink(part)
                    except OSError:
                        pass
                    raise
                return {"path": local_path, "size": size}
        except aiohttp.ServerFingerprintMismatch:
            raise AgentError(self.mismatch_message(), 495) from None
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            raise AgentError(f"download from {self.setup['name']} failed: {e}") from None

    async def files_list(self, path: str = ".") -> dict:
        return await self.request("GET", "/api/files/list", params={"path": path})

    async def flash(self, busid: str, body: dict) -> dict:
        wait = float(body.get("timeout") or 300) + float(body.get("monitor_seconds") or 0) + 30
        return await self.request("POST", "/api/usb/flash", dict(body, busid=busid), timeout=wait)

    async def claim(self, busid: str, client: str) -> dict:
        return await self.request("POST", "/api/usb/claim", {"busid": busid, "client": client}, timeout=5)

    async def release(self, busid: str) -> dict:
        return await self.request("POST", "/api/usb/release", {"busid": busid}, timeout=5)

    async def set_nickname(self, busid: str, name: str) -> dict:
        return await self.request("POST", "/api/usb/nickname", {"busid": busid, "name": name})

    async def start_usbipd(self) -> dict:
        return await self.request("POST", "/api/usbipd/start", {}, timeout=30)

    async def exec(self, command: str, timeout: float = 60, cwd: str | None = None,
                   user: str | None = None) -> dict:
        body: dict[str, Any] = {"command": command, "timeout": timeout}
        if cwd:
            body["cwd"] = cwd
        if user:
            body["user"] = user
        return await self.request("POST", "/api/exec", body, timeout=timeout + 15)

    async def log(self, n: int = 200) -> dict:
        return await self.request("GET", "/api/log", params={"n": str(n)})

    async def get_config(self) -> dict:
        return await self.request("GET", "/api/config")

    async def update_config(self, changes: dict) -> dict:
        return await self.request("POST", "/api/config", changes)

    def shell_url(self, cols: int = 80, rows: int = 24, user: str | None = None) -> str:
        url = f"{self.ws_base}/api/shell?cols={int(cols)}&rows={int(rows)}"
        if user:
            url += "&user=" + urllib.parse.quote(user, safe="")
        return url
