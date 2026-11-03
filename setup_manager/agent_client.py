"""Async HTTP client for one setup agent."""

from __future__ import annotations

import asyncio
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
