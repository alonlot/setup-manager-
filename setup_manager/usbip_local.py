"""The usbip client on the manager PC: attach, detach and list imported devices."""

from __future__ import annotations

import asyncio
import os
import re
import shlex
import shutil
import sys
from typing import Any

IS_WINDOWS = sys.platform.startswith("win")

PORT_RE = re.compile(r"^\s*Port\s+(\d+)\s*:\s*(.*?)(?:\s+at\s+(.*))?$")
IDS_RE = re.compile(r"\(\s*([0-9a-fA-F]{4})\s*:\s*([0-9a-fA-F]{4})\s*\)\s*$")
URL_RE = re.compile(r"(\S+)?\s*->\s*usbip://(\[[^\]]+\]|[^:/\s]+):(\d+)/(\S+)")
REMOTE_RE = re.compile(r"remote bus/dev\s+(\d+)/(\d+)")


class UsbipError(Exception):
    pass


def parse_port_output(text: str) -> list[dict[str, Any]]:
    """Parse the output of `usbip port` (Linux usbip and usbip-win2)."""
    ports: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None
    for raw in text.splitlines():
        line = raw.rstrip()
        m = PORT_RE.match(line)
        if m:
            cur = {"port": int(m.group(1)), "status": m.group(2).strip(" <>"), "speed": (m.group(3) or "").strip(),
                   "vid": None, "pid": None, "name": None, "host": None, "remote_port": None, "busid": None,
                   "local_busid": None, "remote_busnum": None, "remote_devnum": None}
            ports.append(cur)
            continue
        if cur is None:
            continue
        m = URL_RE.search(line)
        if m:
            cur["local_busid"] = (m.group(1) or "").strip() or None
            cur["host"] = m.group(2).strip("[]")
            cur["remote_port"] = int(m.group(3))
            cur["busid"] = m.group(4)
            continue
        m = REMOTE_RE.search(line)
        if m:
            cur["remote_busnum"], cur["remote_devnum"] = int(m.group(1)), int(m.group(2))
            continue
        m = IDS_RE.search(line)
        if m and cur["vid"] is None:
            cur["vid"], cur["pid"] = m.group(1).lower(), m.group(2).lower()
            name = line[:m.start()].strip()
            if name and "unknown vendor" not in name.lower():
                cur["name"] = re.sub(r"\s*:\s*", " : ", name)
    return ports


class LocalUsbip:
    def __init__(self, settings: dict[str, Any] | None = None):
        self.settings = settings or {}
        self.last_error: str | None = None
        self._resolved: tuple[list[str], bool, str] | None = None

    def update(self, settings: dict[str, Any]) -> None:
        self.settings = settings or {}
        self._resolved = None

    @property
    def use_sudo(self) -> bool:
        sudo = self.settings.get("sudo")
        if sudo is None:
            return not IS_WINDOWS and hasattr(os, "geteuid") and os.geteuid() != 0
        return bool(sudo)

    def _resolve(self, force: bool = False) -> tuple[list[str], bool, str] | None:
        """Linux, one-word command: (argv, works, wrapper error) with the same fallback as the agent.

        On Ubuntu /usr/bin/usbip is a wrapper that fails ("not found for kernel") when the tools of
        the running kernel are missing; then the real binary in /usr/lib/linux-tools*/ is used.
        """
        cmd = self.settings.get("command") or ("usbip.exe" if IS_WINDOWS else "usbip")
        argv = list(cmd) if isinstance(cmd, list) else shlex.split(cmd, posix=not IS_WINDOWS)
        if IS_WINDOWS or len(argv) != 1:
            return None
        if force or self._resolved is None:
            from .agent import setup_agent as sa
            self._resolved = sa.resolve_tool(argv, "usbip", ["version"])
        return self._resolved

    def _command(self) -> list[str]:
        resolved = self._resolve()
        if resolved:
            argv = resolved[0]
        else:
            cmd = self.settings.get("command") or ("usbip.exe" if IS_WINDOWS else "usbip")
            argv = list(cmd) if isinstance(cmd, list) else shlex.split(cmd, posix=not IS_WINDOWS)
        return (["sudo", "-n"] if self.use_sudo else []) + argv

    def _hint(self) -> str | None:
        resolved = self._resolve()
        if resolved and "not found for kernel" in resolved[2]:
            from .agent import setup_agent as sa
            return sa.kernel_tools_hint()
        return None

    async def _run(self, argv: list[str], timeout: float = 30) -> tuple[int, str]:
        try:
            p = await asyncio.create_subprocess_exec(*argv, stdin=asyncio.subprocess.DEVNULL,
                                                     stdout=asyncio.subprocess.PIPE,
                                                     stderr=asyncio.subprocess.STDOUT)
        except FileNotFoundError:
            raise UsbipError(f"'{argv[0]}' not found. Install the usbip client on this PC.") from None
        try:
            out, _ = await asyncio.wait_for(p.communicate(), timeout)
        except asyncio.TimeoutError:
            p.kill()
            raise UsbipError(f"{' '.join(argv)} timed out") from None
        text = out.decode(errors="replace").strip()
        if p.returncode != 0 and "not found for kernel" in text:
            self._resolve(force=True)  # the next call uses the real binary when there is one now
            text += f"\nHint: {self._hint() or 'install linux-tools for this kernel'}"
        if p.returncode != 0 and self.use_sudo and "password is required" in text:
            text += ("\nHint: allow passwordless usbip, e.g. in /etc/sudoers.d/usbip:\n"
                     f"  {os.environ.get('USER', 'youruser')} ALL=(root) NOPASSWD: "
                     f"{shutil.which('usbip') or '/usr/bin/usbip'}, /usr/lib/linux-tools/*/usbip, "
                     f"{shutil.which('modprobe') or '/sbin/modprobe'}")
        return p.returncode or 0, text

    async def usbip(self, *args: str, timeout: float = 30) -> tuple[int, str]:
        return await self._run(self._command() + list(args), timeout)

    async def ensure_vhci(self) -> None:
        if IS_WINDOWS or os.path.exists("/sys/devices/platform/vhci_hcd.0"):
            return
        argv = (["sudo", "-n"] if self.use_sudo else []) + ["modprobe", "vhci-hcd"]
        try:
            await self._run(argv, timeout=15)
        except UsbipError:
            pass

    async def ports(self) -> list[dict[str, Any]]:
        try:
            rc, out = await self.usbip("port", timeout=15)
        except UsbipError as e:
            self.last_error = str(e)
            return []
        if rc != 0:
            # No vhci driver loaded means nothing is attached.
            self.last_error = out if "vhci" not in out.lower() else None
            return []
        self.last_error = None
        return [p for p in parse_port_output(out) if p.get("busid")]

    async def attach(self, host: str, busid: str, tcp_port: int = 3240) -> str:
        await self.ensure_vhci()
        args = ([] if tcp_port == 3240 else ["--tcp-port", str(tcp_port)]) + ["attach", "-r", host, "-b", busid]
        rc, out = await self.usbip(*args, timeout=30)
        if rc != 0:
            raise UsbipError(out or f"usbip attach failed (rc={rc})")
        return out

    async def detach(self, port: int) -> str:
        rc, out = await self.usbip("detach", "-p", str(int(port)), timeout=30)
        if rc != 0:
            raise UsbipError(out or f"usbip detach failed (rc={rc})")
        return out

    def info(self) -> dict[str, Any]:
        argv = self._command()
        exe = argv[2] if self.use_sudo else argv[0]
        resolved = self._resolve()
        found = resolved[1] if resolved else shutil.which(exe) is not None
        hint = self._hint()  # set when the Ubuntu wrapper fails (also when the real binary works)
        error = self.last_error
        if resolved and not resolved[1] and hint and not error:
            error = f"{resolved[2].splitlines()[0]} ({hint})"
        return {"command": " ".join(argv), "found": found, "sudo": self.use_sudo, "path": exe,
                "platform": sys.platform, "error": error, "hint": hint}
