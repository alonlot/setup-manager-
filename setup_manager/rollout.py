"""`setup-manager rollout`: update the agent on every setup PC, with a new unique token for each.

For each setup PC, over SSH:
  1. copy the agent files, 2. run install.sh with sudo and a NEW random token,
  3. save the token and the TLS fingerprint (read over SSH: a safe pin) in the manager config,
  4. wait for the agent and check it (version, TLS, tunnel, usbip, usbipd).
Devices that are attached to this PC from that setup are detached before and attached again after.
The new token is saved as "pending" BEFORE the install: when something fails, a second run continues
(it uses SSH, not the old token), so access is never lost.
"""

from __future__ import annotations

import asyncio
import copy
import getpass
import os
import secrets
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .config import ConfigStore
from .fleet import Fleet, FleetError

AGENT_DIR = Path(__file__).parent / "agent"
AGENT_FILES = ["setup_agent.py", "install.sh", "uninstall.sh", "setup-agent.service", "setup-usbipd.service"]


def bundled_version() -> str:
    for line in (AGENT_DIR / "setup_agent.py").read_text().splitlines():
        if line.startswith("VERSION = "):
            return line.split("=", 1)[1].strip().strip('"')
    return "?"


@dataclass
class Options:
    only: list[str] = field(default_factory=list)
    ssh_user: str | None = None
    ssh_port: int | None = None
    rotate_tokens: bool = True
    install_deps: bool = True
    require_encryption: bool = True
    yes: bool = False
    dry_run: bool = False
    ask: Callable[[str], str] = input
    ask_secret: Callable[[str], str] = getpass.getpass
    out: Any = sys.stdout


@dataclass
class Result:
    setup: str
    ok: bool = False
    steps: list[str] = field(default_factory=list)
    error: str = ""
    version: str = ""
    encrypted: bool = False
    reattached: list[str] = field(default_factory=list)


class Rollout:
    def __init__(self, store: ConfigStore, opts: Options):
        self.store = store
        self.opts = opts
        self.sudo_password: str | None = None
        self.version = bundled_version()

    # -- output and helpers -------------------------------------------------------
    def say(self, text: str = "") -> None:
        self.opts.out.write(text + "\n")
        self.opts.out.flush()

    def _fleet(self, fn: Callable[[Fleet], Any], store: ConfigStore | None = None) -> Any:
        async def go() -> Any:
            async with Fleet(store or self.store) as f:
                return await fn(f)
        return asyncio.run(go())

    def _ssh_base(self) -> list[str]:
        return ["ssh", "-o", "ConnectTimeout=15"] + (["-p", str(self.opts.ssh_port)] if self.opts.ssh_port else [])

    def ssh(self, target: str, command: str, stdin: str | None = None, timeout: float = 120) -> tuple[int, str]:
        p = subprocess.run(self._ssh_base() + [target, command], input=stdin, capture_output=True, text=True,
                           timeout=timeout)
        return p.returncode, (p.stdout + p.stderr).strip()

    def scp(self, target: str, files: list[Path], remote_dir: str) -> tuple[int, str]:
        cmd = ["scp", "-q"] + (["-P", str(self.opts.ssh_port)] if self.opts.ssh_port else [])
        p = subprocess.run(cmd + [str(f) for f in files] + [f"{target}:{remote_dir}/"], capture_output=True,
                           text=True, timeout=120)
        return p.returncode, (p.stdout + p.stderr).strip()

    # -- plan -----------------------------------------------------------------------
    def targets(self) -> list[dict[str, Any]]:
        setups = self.store.setups()
        if self.opts.only:
            wanted = {x.lower() for x in self.opts.only}
            setups = [s for s in setups if s["id"].lower() in wanted or s["name"].lower() in wanted
                      or s["host"].lower() in wanted]
            missing = wanted - {x for s in setups for x in (s["id"].lower(), s["name"].lower(), s["host"].lower())}
            if missing:
                raise SystemExit(f"unknown setup(s): {', '.join(sorted(missing))}")
        return setups

    def ssh_target(self, s: dict[str, Any]) -> str:
        if s.get("ssh"):
            return s["ssh"]
        if self.opts.ssh_user is None:
            default = getpass.getuser()
            answer = "" if self.opts.yes else self.opts.ask(
                f"SSH user on the setup PCs that have no SSH target yet [{default}]: ").strip()
            self.opts.ssh_user = answer or default
        return f"{self.opts.ssh_user}@{s['host']}"

    def survey(self, setups: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        """Current state of each setup (version, encryption, devices that the update disconnects)."""
        async def go(f: Fleet) -> dict[str, dict[str, Any]]:
            out: dict[str, dict[str, Any]] = {}
            ports = await f.local_ports()
            for s in setups:
                info: dict[str, Any] = {"online": False, "version": "", "encrypted": None, "here": [],
                                        "others": []}
                try:
                    st = await f.status(s["id"])
                    info.update(online=True, version=st.get("version", ""), encrypted=bool(st.get("tls")))
                    devs = await f.usb(s["id"])
                    info["others"] = [f"{d.get('display_name') or d['name']} ({d['busid']}) by "
                                      f"{d.get('used_by_text') or 'another client'}"
                                      for d in devs if d["state"] == "in_use" and not d.get("attached_here")]
                except FleetError as e:
                    info["error"] = e.message
                info["here"] = [p["busid"] for p in ports if p.get("setup") == s["id"]]
                out[s["id"]] = info
            return out
        from .lab import _MemoryStore
        try:
            # A copy in memory: the survey saves nothing (the fingerprints come over SSH later).
            return self._fleet(go, _MemoryStore(copy.deepcopy(self.store.data())))
        except Exception as e:  # noqa: BLE001 - the survey is only information
            self.say(f"(could not read the current state: {e})")
            return {}

    # -- one setup --------------------------------------------------------------------
    def upgrade(self, s: dict[str, Any], target: str, here: list[str]) -> Result:
        r = Result(s["id"])
        sid = s["id"]
        try:
            rc, out = self.ssh(target, "echo SM_SSH_OK")
            if rc != 0 or "SM_SSH_OK" not in out:
                raise RuntimeError(f"SSH to {target} failed: {out[-300:]}")
            r.steps.append("ssh ok")
            rc, remote = self.ssh(target, "mktemp -d /tmp/setup-agent-XXXXXX")
            remote = remote.strip().splitlines()[-1] if remote.strip() else ""
            if rc != 0 or not remote.startswith("/tmp/setup-agent-"):
                raise RuntimeError(f"cannot make a folder on {target}: {remote}")
            rc, out = self.scp(target, [AGENT_DIR / f for f in AGENT_FILES], remote)
            if rc != 0:
                raise RuntimeError(f"copy to {target} failed: {out[-300:]}")
            r.steps.append("files copied")

            sudo = "sudo -n"
            rc, out = self.ssh(target, "sudo -n true")
            if rc != 0:
                if self.sudo_password is None:
                    self.sudo_password = self.opts.ask_secret(
                        "sudo password on the setup PCs (asked once, kept in memory only): ")
                sudo = "sudo -S -p ''"

            data = self.store.data()
            token = data.setdefault("rollout_pending", {}).get(sid) if self.opts.rotate_tokens else None
            if self.opts.rotate_tokens and not token:
                token = secrets.token_urlsafe(32)
                data["rollout_pending"][sid] = token  # saved BEFORE the install: a failure can continue
                self.store.save()

            if here:
                self._fleet(lambda f: asyncio.gather(*(f.detach(sid, b, unbind=False) for b in here),
                                                     return_exceptions=True))
                r.steps.append(f"detached here: {', '.join(here)}")

            args = []
            if token:
                args += ["--token", token]
            if self.opts.install_deps:
                args.append("--install-deps")
            cmd = f"{sudo} bash {shlex.quote(remote + '/install.sh')} {' '.join(shlex.quote(a) for a in args)}"
            rc, out = self.ssh(target, cmd, stdin=(self.sudo_password + "\n") if "-S" in sudo else None,
                               timeout=900)
            self.ssh(target, f"rm -rf {shlex.quote(remote)}")
            if rc != 0:
                if "incorrect password" in out or "Sorry, try again" in out:
                    self.sudo_password = None
                raise RuntimeError(f"install.sh failed (rc={rc}): {out[-600:]}")
            values = dict(line.split("=", 1) for line in out.splitlines() if line.startswith("CONFIG_") and "=" in line)
            new_token = values.get("CONFIG_TOKEN", "").strip()
            fingerprint = values.get("CONFIG_FINGERPRINT", "").strip()
            if token and new_token != token:
                raise RuntimeError("the agent did not take the new token (install.sh output has another one)")
            r.steps.append("installed")

            fields = {"token": new_token or s.get("token"), "ssh": target}
            if fingerprint:
                fields.update(fingerprint=fingerprint, transport="tls")
            self.store.update_setup(sid, fields)
            data = self.store.data()
            data.get("rollout_pending", {}).pop(sid, None)
            self.store.save()
            r.steps.append("new token saved" if token else "token kept")

            r.version, r.encrypted = self._wait_and_check(sid, r)
            if here:
                for b in here:
                    try:
                        self._fleet(lambda f, b=b: f.attach(sid, b))
                        r.reattached.append(b)
                    except FleetError as e:
                        r.steps.append(f"attach {b} again failed: {e.message}")
                r.steps.append(f"attached again: {', '.join(r.reattached) or 'none'}")
            r.ok = r.version == self.version and r.encrypted
            if r.version != self.version:
                r.error = f"agent reports version {r.version}, expected {self.version}"
            elif not r.encrypted:
                r.error = "the agent is not encrypted (installed with --plain?)"
        except (RuntimeError, subprocess.TimeoutExpired, FleetError, OSError) as e:
            r.error = getattr(e, "message", None) or str(e)
        return r

    def _wait_and_check(self, sid: str, r: Result) -> tuple[str, bool]:
        deadline = time.time() + 45
        last = ""
        while time.time() < deadline:
            try:
                st = self._fleet(lambda f: f.status(sid))
                enc = self._fleet(lambda f: _encrypted(f, sid))
                usbip = st.get("usbip") or {}
                if not usbip.get("usbip_installed"):
                    r.steps.append("WARNING: usbip is not installed on the setup PC")
                elif not usbip.get("usbipd_running"):
                    r.steps.append("WARNING: usbipd is not running on the setup PC")
                r.steps.append(f"agent {st.get('version')} online" + (", TLS + USB/IP tunnel" if enc else ""))
                return st.get("version", ""), enc and bool(st.get("tunnel"))
            except FleetError as e:
                last = e.message
                time.sleep(1.5)
        raise RuntimeError(f"the agent did not come back after the install: {last}")

    # -- this PC -------------------------------------------------------------------------
    def check_this_pc(self) -> list[str]:
        notes = []
        if not shutil.which("usbip"):
            notes.append("usbip is not installed on this PC (Ubuntu: sudo apt install linux-tools-generic)")
        if not os.path.exists("/etc/sudoers.d/setup-manager") and os.geteuid() != 0:
            notes.append("no sudo rule for usbip on this PC: run ./install-app.sh")
        if not os.path.exists("/sys/module/vhci_hcd") and not os.path.exists("/sys/devices/platform/vhci_hcd.0"):
            notes.append("the vhci-hcd module is not loaded on this PC: sudo modprobe vhci-hcd")
        try:
            import grp
            if os.geteuid() != 0 and "dialout" not in [grp.getgrgid(g).gr_name for g in os.getgroups()]:
                notes.append("you are not in the dialout group on this PC (log out and in after install-app.sh)")
        except (ImportError, KeyError):
            pass
        return notes

    # -- all -------------------------------------------------------------------------------
    def run(self) -> int:
        setups = self.targets()
        if not setups:
            self.say("No setups in the config. Add one first: setup-manager deploy USER@HOST --name NAME")
            return 1
        state = self.survey(setups)
        plan = [(s, self.ssh_target(s)) for s in setups]
        self.say(f"\nAgent version to install: {self.version}"
                 f"{'   (every PC gets a NEW random token)' if self.opts.rotate_tokens else ''}\n")
        self.say(f"  {'SETUP':<18} {'SSH':<28} {'NOW':<16} NOTES")
        warnings = []
        for s, target in plan:
            st = state.get(s["id"], {})
            now = (f"v{st['version']}" + (" TLS" if st.get("encrypted") else " plain")) if st.get("online") \
                else "offline"
            notes = []
            if st.get("here"):
                notes.append(f"attached here: {', '.join(st['here'])} (detached, then attached again)")
            if st.get("others"):
                notes.append("DISCONNECTS: " + "; ".join(st["others"]))
                warnings += st["others"]
            if s["id"] in (self.store.data().get("rollout_pending") or {}):
                notes.append("continues a failed run")
            self.say(f"  {s['name']:<18} {target:<28} {now:<16} {'  '.join(notes)}")
        if warnings:
            self.say("\nWARNING: the agent restarts. The devices above that other people use will disconnect.")
        if self.opts.dry_run:
            self.say("\n(dry run: nothing was changed)")
            return 0
        if not self.opts.yes and self.opts.ask("\nUpdate these setup PCs now? [y/N] ").strip().lower() not in ("y", "yes"):
            self.say("Nothing was changed.")
            return 1

        results = []
        for s, target in plan:
            self.say(f"\n>> {s['name']} ({target})")
            here = state.get(s["id"], {}).get("here") or []
            r = self.upgrade(s, target, here)
            for step in r.steps:
                self.say(f"   - {step}")
            self.say(f"   {'OK' if r.ok else 'FAILED: ' + r.error}")
            results.append(r)

        ok = [r for r in results if r.ok]
        self.say(f"\n===== {len(ok)} of {len(results)} setup PCs updated =====")
        for r in results:
            self.say(f"  {'OK    ' if r.ok else 'FAILED'} {r.setup:<18} {r.version or '-':<8} "
                     f"{'encrypted' if r.encrypted else ''} {r.error}")
        data = self.store.data()
        all_setups_ok = len(ok) == len(results) and len(results) == len(self.store.setups())
        if all_setups_ok and self.opts.require_encryption:
            data["require_encryption"] = True
            self.say("\nRequire encryption: ON (agents without TLS are refused from now on)")
        if len(ok) == len(results) and self.opts.rotate_tokens and data["agent_defaults"].get("token"):
            data["agent_defaults"]["token"] = ""
            self.say("The old shared default token was removed: each PC has its own token now.")
        self.store.save()
        notes = self.check_this_pc()
        if notes:
            self.say("\nThis PC:")
            for n in notes:
                self.say(f"  - {n}")
        if subprocess.run(["pgrep", "-f", "setup_manager.* app$|setup-manager$"], capture_output=True).returncode == 0:
            self.say("\nThe app is open: close it and open it again to use the new version.")
        if len(ok) != len(results):
            self.say("\nRun the same command again to continue the setups that failed (they keep their pending "
                     "token; access is not lost).")
            return 2
        return 0


async def _encrypted(f: Fleet, sid: str) -> bool:
    c = f.client(sid)
    await c.ensure()
    return c.encrypted
