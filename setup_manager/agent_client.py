"""Async HTTP client for one setup agent."""

from __future__ import annotations

import asyncio
import base64
import os
from typing import Any

import aiohttp


class AgentError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.message = message
        self.status = status


class AgentClient:
    def __init__(self, session: aiohttp.ClientSession, setup: dict[str, Any], port: int, token: str):
        self.session = session
        self.setup = setup
        self.port = port
        self.token = token
        host = setup["host"]
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        scheme = "https" if setup.get("tls") else "http"
        self.base = f"{scheme}://{host}:{port}"
        self.ws_base = f"{'wss' if setup.get('tls') else 'ws'}://{host}:{port}"

    @property
    def ssl(self) -> Any:
        if self.setup.get("tls") and not self.setup.get("verify_tls"):
            return False
        return True

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    async def request(self, method: str, path: str, body: dict | None = None,
                      params: dict | None = None, timeout: float = 10) -> Any:
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
        except aiohttp.ClientError as e:
            raise AgentError(f"cannot reach {self.setup['name']} ({self.base}): {e}") from None

    async def ping(self) -> dict:
        return await self.request("GET", "/api/ping", timeout=4)

    async def status(self) -> dict:
        return await self.request("GET", "/api/status", timeout=6)

    async def usb(self, include_hubs: bool = False) -> dict:
        return await self.request("GET", "/api/usb", params={"all": "1"} if include_hubs else None, timeout=8)

    async def bind(self, busid: str, force: bool = False) -> dict:
        return await self.request("POST", "/api/usb/bind", {"busid": busid, "force": force}, timeout=30)

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
        try:
            with open(local_path, "rb") as f:
                async with self.session.put(self.base + "/api/files", params={"path": remote_path}, data=f,
                                            headers=dict(self.headers, **{"Content-Length": str(size)}),
                                            ssl=self.ssl, timeout=aiohttp.ClientTimeout(total=600)) as r:
                    data = await r.json(content_type=None)
                    if r.status >= 400:
                        raise AgentError(data.get("error") or f"HTTP {r.status}", r.status)
                    return data
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            raise AgentError(f"upload to {self.setup['name']} failed: {e}") from None

    async def download(self, remote_path: str, local_path: str) -> dict:
        try:
            async with self.session.get(self.base + "/api/files", params={"path": remote_path},
                                        headers=self.headers, ssl=self.ssl,
                                        timeout=aiohttp.ClientTimeout(total=600)) as r:
                if r.status >= 400:
                    data = await r.json(content_type=None)
                    raise AgentError(data.get("error") or f"HTTP {r.status}", r.status)
                size = 0
                with open(local_path, "wb") as f:
                    async for chunk in r.content.iter_chunked(1024 * 1024):
                        f.write(chunk)
                        size += len(chunk)
                return {"path": local_path, "size": size}
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
            url += f"&user={user}"
        return url
