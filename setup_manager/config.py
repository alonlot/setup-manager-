"""Manager configuration: the list of setups, sticky attachments and local settings.

The config is one JSON file (default ~/.config/setup-manager/config.json, or
$SETUP_MANAGER_HOME/config.json). The app, the CLI and the MCP server share it.
Each process reloads it when the file changes on disk.
"""

from __future__ import annotations

import copy
import json
import os
import re
import tempfile
import threading
from pathlib import Path
from typing import Any

DEFAULTS: dict[str, Any] = {
    # Defaults for setups that do not set their own port / token.
    "agent_defaults": {"port": 8765, "token": ""},
    # Local usbip client. command: null = "usbip" ("usbip.exe" on Windows).
    # sudo: null = use "sudo -n" when not root (Linux only).
    "local_usbip": {"command": None, "sudo": None},
    "poll_interval": 5,
    "discovery_port": 8765,
    "discovery_targets": ["255.255.255.255"],
    "setups": [],
    "sticky": [],
    # rollout: new tokens that are saved before the install (a failed run continues with them).
    "rollout_pending": {},
    # true = refuse agents without TLS (agents before version 0.4) and USB/IP without the tunnel.
    "require_encryption": False,
    # Your name for the setups ("in use by ..."). Empty = user@hostname.
    "client_name": "",
    # Flash: your own profiles and the last options per device.
    "flash_profiles": {},
    "flash_last": {},
    # MCU identification: your own profiles, the last options per device, the results per device.
    "identify_profiles": {},
    "identify_last": {},
    "mcu_results": {},
    # Quick commands in the Run tab of the app.
    "snippets": ["uname -a", "lsusb", "ls -l /dev/ttyACM* /dev/ttyUSB* 2>/dev/null", "dmesg | tail -n 40",
                 "usbip list -l", "uptime", "df -h /"],
}

SETUP_FIELDS = {"name", "host", "port", "token", "tls", "verify_tls", "notes", "tags", "usbip_port", "fingerprint",
                "transport", "ssh", "ssh_port"}


def config_dir() -> Path:
    env = os.environ.get("SETUP_MANAGER_HOME")
    if env:
        return Path(env).expanduser()
    if os.name == "nt":
        return Path(os.environ.get("APPDATA", Path.home())) / "setup-manager"
    return Path.home() / ".config" / "setup-manager"


def slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s or "setup"


class ConfigError(Exception):
    pass


class ConfigStore:
    def __init__(self, path: str | os.PathLike | None = None):
        self.path = Path(path) if path else config_dir() / "config.json"
        self._lock = threading.RLock()
        self._data: dict[str, Any] | None = None
        self._mtime: float | None = None

    # -- file I/O --------------------------------------------------------------
    def _load(self) -> None:
        data = copy.deepcopy(DEFAULTS)
        if self.path.exists():
            with open(self.path, encoding="utf-8") as f:
                loaded = json.load(f)
            for k, v in loaded.items():
                if isinstance(v, dict) and isinstance(data.get(k), dict):
                    data[k].update(v)
                else:
                    data[k] = v
            self._mtime = self.path.stat().st_mtime
        else:
            self._mtime = None
        data["setups"] = [self._normalize(s) for s in data.get("setups", [])]
        self._data = data

    def data(self) -> dict[str, Any]:
        with self._lock:
            mtime = self.path.stat().st_mtime if self.path.exists() else None
            if self._data is None or mtime != self._mtime:
                self._load()
            return self._data  # type: ignore[return-value]

    def save(self) -> None:
        with self._lock:
            data = self.data()
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=self.path.name + ".", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2)
                os.chmod(tmp, 0o600)
                os.replace(tmp, self.path)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
            self._mtime = self.path.stat().st_mtime

    # -- setups ----------------------------------------------------------------
    @staticmethod
    def _normalize(s: dict[str, Any]) -> dict[str, Any]:
        out = {
            "id": s.get("id") or slugify(s.get("name") or s.get("host") or "setup"),
            "name": s.get("name") or s.get("host") or s.get("id"),
            "host": s.get("host", ""),
            "port": int(s["port"]) if s.get("port") else None,
            "token": s.get("token", ""),
            "tls": bool(s.get("tls", False)),
            "verify_tls": bool(s.get("verify_tls", False)),
            # The SHA-256 fingerprint of the agent's TLS certificate (pinned, like SSH known_hosts).
            "fingerprint": (s.get("fingerprint") or "").replace(":", "").strip().lower(),
            # "auto" (TLS when the agent has it), "tls" (only TLS) or "plain" (no encryption).
            "transport": s.get("transport") or ("tls" if s.get("tls") else "auto"),
            # SSH target for deploy / rollout, e.g. "dev@192.168.1.20" (empty = ask, or USER@host).
            "ssh": s.get("ssh") or "",
            "ssh_port": int(s["ssh_port"]) if s.get("ssh_port") else None,
            "notes": s.get("notes", ""),
            "tags": list(s.get("tags") or []),
            "usbip_port": int(s.get("usbip_port") or 3240),
        }
        return out

    def setups(self) -> list[dict[str, Any]]:
        return list(self.data()["setups"])

    def get_setup(self, ref: str) -> dict[str, Any] | None:
        ref_l = str(ref).strip().lower()
        setups = self.setups()
        for key in ("id", "name", "host"):
            for s in setups:
                if str(s.get(key) or "").lower() == ref_l:
                    return s
        return None

    def agent_port(self, setup: dict[str, Any]) -> int:
        return int(setup.get("port") or self.data()["agent_defaults"].get("port") or 8765)

    def agent_token(self, setup: dict[str, Any]) -> str:
        return setup.get("token") or self.data()["agent_defaults"].get("token") or ""

    def add_setup(self, fields: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            if not fields.get("host"):
                raise ConfigError("host is required")
            data = self.data()
            setup = self._normalize({k: v for k, v in fields.items() if k in SETUP_FIELDS})
            base = slugify(fields.get("id") or setup["name"])
            ids = {s["id"] for s in data["setups"]}
            sid, n = base, 2
            while sid in ids:
                sid, n = f"{base}-{n}", n + 1
            setup["id"] = sid
            data["setups"].append(setup)
            self.save()
            return setup

    def update_setup(self, sid: str, fields: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            data = self.data()
            for i, s in enumerate(data["setups"]):
                if s["id"] == sid:
                    merged = dict(s)
                    for k, v in fields.items():
                        if k not in SETUP_FIELDS:
                            continue
                        if k == "token" and v in (None, "", "********"):
                            continue  # keep the stored token when the UI sends an empty field
                        merged[k] = v
                    if fields.get("clear_token"):
                        merged["token"] = ""
                    data["setups"][i] = self._normalize(merged)
                    self.save()
                    return data["setups"][i]
            raise ConfigError(f"no setup {sid}")

    def remove_setup(self, sid: str) -> None:
        with self._lock:
            data = self.data()
            before = len(data["setups"])
            data["setups"] = [s for s in data["setups"] if s["id"] != sid]
            if len(data["setups"]) == before:
                raise ConfigError(f"no setup {sid}")
            data["sticky"] = [x for x in data.get("sticky", []) if x.get("setup") != sid]
            self.save()

    # -- sticky attachments ----------------------------------------------------
    def sticky(self) -> list[dict[str, Any]]:
        return list(self.data().get("sticky", []))

    def set_sticky(self, sid: str, busid: str, enabled: bool, device: dict[str, Any] | None = None) -> None:
        with self._lock:
            data = self.data()
            items = [x for x in data.get("sticky", []) if not (x.get("setup") == sid and x.get("busid") == busid)]
            if enabled:
                entry = {"setup": sid, "busid": busid}
                if device:
                    entry.update({"vidpid": device.get("vidpid"), "serial": device.get("serial"),
                                  "name": device.get("name")})
                items.append(entry)
            data["sticky"] = items
            self.save()

    @staticmethod
    def redact(setup: dict[str, Any]) -> dict[str, Any]:
        out = dict(setup)
        out["has_token"] = bool(out.pop("token", ""))
        return out
