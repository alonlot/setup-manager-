#!/usr/bin/env python3
"""setup-agent: a small daemon that runs on every Setup PC.

It gives a Setup Manager access to:
  * the status of the PC (CPU, memory, disk, IPs, usbipd state, ...)
  * the USB devices of the PC, with USB/IP bind / unbind / reset
  * shell commands (one-shot exec) and an interactive PTY shell (WebSocket)

It uses only the Python standard library (Python 3.8+), so you can copy this
single file to a PC and run it. It must run as root to bind USB devices.

API (all routes except /api/ping need "Authorization: Bearer <token>"):
  GET  /api/ping                     name, version (no auth)
  GET  /api/status                   PC status
  GET  /api/usb[?all=1]              USB devices (all=1 also shows hubs)
  POST /api/usb/bind    {busid, force}
  POST /api/usb/unbind  {busid}
  POST /api/usb/reset   {busid}
  POST /api/usb/identify {busid, reset, baud, duration, script, after_script, tty, user}
                                     reset the MCU and capture its serial log
  POST /api/usb/flash   {busid, file, script, timeout, monitor_seconds, baud}
  POST /api/usb/claim   {busid, client}   POST /api/usb/release {busid}
  POST /api/usb/nickname {busid, name}
  POST /api/serial/open {busid | tty, baud}      GET /api/serial/read?busid|tty&since&wait&max
  POST /api/serial/write {busid | tty, data | data_b64}   POST /api/serial/reset | close
  PUT  /api/files?path=   (body = the file)      GET /api/files?path=   GET /api/files/list?path=
  POST /api/reserve {busid, owner, ttl[, lease]}  POST /api/release {busid, lease | force}
  GET  /api/reservations   (while a device is reserved, changes to it need header X-SM-Lease)
  POST /api/usbipd/start
  POST /api/exec        {command, timeout, cwd, user}
  GET  /api/shell?cols=&rows=&user=  WebSocket, interactive PTY shell
  GET  /api/log[?n=200]              recent agent log lines
  GET  /api/config                   agent config (token removed)
  POST /api/config      {name, auto_bind, protected}
"""

import argparse
import base64
import collections
import errno
import fcntl
import glob
import hashlib
import hmac
import json
import logging
import os
import pwd
import re
import secrets
import select
import shlex
import shutil
import signal
import socket
import ssl
import struct
import subprocess
import sys
import tempfile
import termios
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

VERSION = "0.4.0"
DEFAULT_CONFIG_PATH = "/etc/setup-agent/config.json"
DISCOVERY_MAGIC = b"SETUP-MANAGER-DISCOVER"
BUSID_RE = re.compile(r"^\d+-\d+(\.\d+)*$")
MAX_BODY = 4 * 1024 * 1024
MAX_OUTPUT = 1024 * 1024
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
USBDEVFS_RESET = 0x5514

DEFAULTS = {
    "name": "",
    "listen": "0.0.0.0",
    "port": 8765,
    "token": "",
    "insecure_no_auth": False,
    # TLS: "auto" (default) = on, with the given tls_cert/tls_key or a self-signed certificate
    # that the agent makes at the first start in tls_dir (default: the folder of the config).
    # false = plain HTTP (not encrypted, only for tests or old managers).
    "tls": "auto",
    "tls_cert": "",
    "tls_key": "",
    "tls_dir": "",
    # USB/IP: "tunnel" (default with TLS) = port 3240 is blocked from the network, USB/IP works only
    # through the encrypted tunnel on the agent port. "open" = port 3240 stays open (not encrypted).
    "usbip_network": "tunnel",
    "usbip_port": 3240,
    # User for shells and commands. Empty = the user the agent runs as (root).
    "run_as": "",
    "shell": "/bin/bash",
    "usbip_cmd": "usbip",
    "usbipd_cmd": "usbipd",
    # Where the real usbip/usbipd binaries of the kernel tools are (Ubuntu). /usr/bin/usbip there is a
    # wrapper that fails when the tools of the running kernel are missing; the agent then uses these.
    # The env var SETUP_AGENT_TOOLS_DIRS (dirs separated by ":") replaces the default.
    "kernel_tools_dirs": [],
    # Start usbipd and load usbip-host when they are not running.
    "manage_usbipd": True,
    # Rules: "1-1.2" (busid), "0483:374b" (vid:pid), "0483:*", "0483:374b:SERIAL"
    "auto_bind": [],
    "auto_bind_interval": 3,
    "protected": [],
    "discovery": True,
    "discovery_port": 8765,
    "sysfs_root": "/sys",
    "devfs_root": "/dev",
    "exec_timeout_max": 3600,
    # Uploaded files (firmware). Relative paths in the files API go here.
    "files_dir": "/var/lib/setup-agent/files",
    "max_upload": 1024 * 1024 * 1024,
    # A serial port that nobody reads for this time is closed.
    "serial_idle_timeout": 900,
    # Device nicknames: rule ("vid:pid:serial" or busid) -> name.
    "nicknames": {},
}

USB_CLASSES = {
    "00": "Per-interface", "01": "Audio", "02": "CDC", "03": "HID", "05": "Physical",
    "06": "Image", "07": "Printer", "08": "Mass storage", "09": "Hub", "0a": "CDC data",
    "0b": "Smart card", "0d": "Security", "0e": "Video", "0f": "Healthcare",
    "10": "Audio/Video", "11": "Billboard", "dc": "Diagnostic", "e0": "Wireless",
    "ef": "Misc", "fe": "App specific", "ff": "Vendor specific",
}

USBIP_STATUS = {"1": "bound", "2": "in_use", "3": "error"}

log = logging.getLogger("setup-agent")
LOG_RING = collections.deque(maxlen=1000)


class RingHandler(logging.Handler):
    def emit(self, record):
        try:
            LOG_RING.append({"ts": record.created, "level": record.levelname, "msg": self.format(record)})
        except Exception:  # pragma: no cover
            pass


class ApiError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


# --------------------------------------------------------------------------- helpers

def read_text(path, default=None):
    try:
        with open(path, "r", errors="replace") as f:
            return f.read().strip()
    except OSError:
        return default


def busid_key(busid):
    return [int(x) for x in re.split(r"[-.]", busid) if x.isdigit()]


def current_user():
    try:
        return pwd.getpwuid(os.geteuid()).pw_name
    except KeyError:
        return str(os.geteuid())


def user_home(user):
    try:
        return pwd.getpwnam(user).pw_dir
    except KeyError:
        return "/"


def split_cmd(value):
    if isinstance(value, (list, tuple)):
        return list(value)
    return shlex.split(str(value))


# Ubuntu: /usr/bin/usbip(d) of linux-tools-common runs /usr/lib/linux-tools/<kernel>/usbip(d) and
# prints "WARNING: usbip not found for kernel ..." (rc=2) when the tools of this kernel are missing.
KERNEL_TOOLS_MSG = "not found for kernel"
KERNEL_TOOLS_DIRS = ["/usr/lib/linux-tools/*", "/usr/lib/linux-tools-*"]


def kernel_tools_hint():
    return "install the usbip tools for this kernel: sudo apt install linux-tools-%s" % os.uname().release


def version_key(text):
    """Sort key for kernel versions: "6.8.0-45-generic" -> (6, 8, 0, 45)."""
    return tuple(int(n) for n in re.findall(r"\d+", text))


def kernel_tools_candidates(name, dirs=None, release=None):
    """The real usbip/usbipd binaries: the one of the running kernel first, then the newest."""
    if dirs is None:
        env = os.environ.get("SETUP_AGENT_TOOLS_DIRS")
        dirs = env.split(":") if env else KERNEL_TOOLS_DIRS
    release = release or os.uname().release
    found = []
    for pattern in dirs:
        for d in glob.glob(pattern):
            path = os.path.join(d, name)
            if path not in found and os.path.isfile(path) and os.access(path, os.X_OK):
                found.append(path)

    def key(path):
        ver = os.path.basename(os.path.dirname(path))
        ver = ver[len("linux-tools-"):] if ver.startswith("linux-tools-") else ver
        this_kernel = ver == release or release.startswith(ver + "-")
        return (this_kernel, version_key(ver))
    return sorted(found, key=key, reverse=True)


def try_tool(argv, timeout=10):
    """Run a command, return (ok, output). A missing command is not ok."""
    try:
        p = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, "%s: %s" % (argv[0], e)
    return p.returncode == 0, p.stdout.decode(errors="replace").strip()


def resolve_tool(cmd, name, probe_args, dirs=None):
    """Find a usbip/usbipd command that works. Returns (argv, ok, error).

    A command of one word (default "usbip") that is missing or is the Ubuntu wrapper without the
    tools of this kernel is replaced by the real binary in the kernel tools dirs. The usbip tools
    of another kernel version work: they use sysfs and the usbipd protocol, which do not change.
    """
    argv = split_cmd(cmd)
    ok, out = try_tool(argv + probe_args)
    if ok:
        return argv, True, ""
    missing = argv and shutil.which(argv[0]) is None
    if len(argv) == 1 and (missing or KERNEL_TOOLS_MSG in out):
        for path in kernel_tools_candidates(name, dirs):
            if try_tool([path] + probe_args)[0]:
                return [path], True, out
    return argv, False, out


def match_rule(rule, dev):
    """Return True if a rule ("busid", "vid:pid", "vid:*", "vid:pid:serial") matches a device."""
    rule = str(rule).strip().lower()
    if not rule:
        return False
    if BUSID_RE.match(rule):
        return dev["busid"] == rule
    parts = rule.split(":")
    if len(parts) < 2:
        return False
    vid, pid = parts[0], parts[1]
    if vid not in ("*", dev["vid"]) or pid not in ("*", dev["pid"]):
        return False
    if len(parts) >= 3:
        serial = ":".join(parts[2:])
        return serial in ("*", (dev.get("serial") or "").lower())
    return True


def device_rule(dev):
    """The rule we write when the user asks to auto-bind one device."""
    if dev.get("serial"):
        return "%s:%s:%s" % (dev["vid"], dev["pid"], dev["serial"])
    return dev["busid"]


class UsbIds:
    """Lazy reader for the usb.ids name database (when the PC has one)."""

    PATHS = ["/usr/share/hwdata/usb.ids", "/usr/share/misc/usb.ids", "/var/lib/usbutils/usb.ids",
             "/usr/share/usb.ids"]

    def __init__(self):
        self._vendors = None
        self._products = None
        self._lock = threading.Lock()

    def _load(self):
        vendors, products = {}, {}
        for path in self.PATHS:
            try:
                f = open(path, encoding="utf-8", errors="replace")
            except OSError:
                continue
            with f:
                vid = None
                for line in f:
                    if not line.strip() or line.startswith("#"):
                        continue
                    if line.startswith("C "):
                        break
                    if line.startswith("\t\t"):
                        continue
                    if line.startswith("\t"):
                        if vid:
                            products[(vid, line[1:5].lower())] = line[5:].strip()
                    else:
                        vid = line[:4].lower()
                        vendors[vid] = line[4:].strip()
            break
        self._vendors, self._products = vendors, products

    def lookup(self, vid, pid):
        with self._lock:
            if self._vendors is None:
                self._load()
        return self._vendors.get(vid), self._products.get((vid, pid))


# --------------------------------------------------------------------------- USB

class UsbManager:
    def __init__(self, cfg):
        self.cfg = cfg
        self.base = os.path.join(cfg["sysfs_root"], "bus", "usb", "devices")
        self.ids = UsbIds()
        self.lock = threading.Lock()
        self._usbip = None  # (argv, ok, error, time) of resolve_tool
        self._usbip_lock = threading.Lock()

    # -- usbip / usbipd ------------------------------------------------------
    def _tools_dirs(self):
        return self.cfg.get("kernel_tools_dirs") or None

    def resolve_usbip(self, force=False):
        """The usbip command to run (cached; a failed result is checked again after 30 s)."""
        with self._usbip_lock:
            r = self._usbip
            if force or r is None or (not r[1] and time.time() - r[3] > 30):
                argv, ok, err = resolve_tool(self.cfg["usbip_cmd"], "usbip", ["version"], self._tools_dirs())
                if ok and err:
                    log.warning("usbip wrapper does not work (%s), using %s", err.splitlines()[0], argv[0])
                r = self._usbip = (argv, ok, err, time.time())
            return r

    def usbip(self, *args, timeout=20):
        argv = self.resolve_usbip()[0]
        for attempt in (1, 2):
            cmd = argv + list(args)
            try:
                p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)
                out = p.stdout.decode(errors="replace").strip()
                rc = p.returncode
            except FileNotFoundError:
                rc, out = 127, "usbip is not installed on this PC (command: %s)" % cmd[0]
            except subprocess.TimeoutExpired:
                raise ApiError(504, "usbip %s timed out" % " ".join(args))
            if rc == 0 or (rc != 127 and KERNEL_TOOLS_MSG not in out) or attempt == 2:
                break
            new = self.resolve_usbip(force=True)[0]  # e.g. the kernel tools were removed
            if new == argv:
                break
            argv = new
        if rc == 127:
            raise ApiError(500, "%s. Re-run install.sh with --install-deps, or %s" % (out, kernel_tools_hint()))
        if rc != 0 and KERNEL_TOOLS_MSG in out:
            out += "\nHint: " + kernel_tools_hint()
        return rc, out

    def usbipd_running(self):
        try:
            pids = [p for p in os.listdir("/proc") if p.isdigit()]
        except OSError:
            return False
        for pid in pids:
            if read_text("/proc/%s/comm" % pid) == "usbipd":
                return True
        return False

    def module_loaded(self):
        return os.path.isdir(os.path.join(self.cfg["sysfs_root"], "module", "usbip_host")) or \
            os.path.isdir(os.path.join(self.cfg["sysfs_root"], "bus", "usb", "drivers", "usbip-host"))

    def ensure_usbipd(self):
        """Load usbip-host and start usbipd when needed. Returns a list of messages."""
        notes = []
        if not self.cfg.get("manage_usbipd"):
            return notes
        if not self.module_loaded():
            p = subprocess.run(["modprobe", "usbip-host"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            notes.append("modprobe usbip-host: rc=%d %s" % (p.returncode, p.stdout.decode(errors="replace").strip()))
        if not self.usbipd_running():
            started = False
            if shutil.which("systemctl"):
                p = subprocess.run(["systemctl", "start", "setup-usbipd.service"],
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
                started = p.returncode == 0
                if started:
                    notes.append("started setup-usbipd.service")
            if not started:
                argv, ok, err = resolve_tool(self.cfg["usbipd_cmd"], "usbipd", ["--version"], self._tools_dirs())
                if not ok and KERNEL_TOOLS_MSG in err:
                    notes.append("usbipd: %s. Hint: %s" % (err.splitlines()[0], kernel_tools_hint()))
                try:
                    subprocess.run(argv + ["-D"], timeout=10,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
                    notes.append("started %s -D" % " ".join(argv))
                except (OSError, subprocess.TimeoutExpired) as e:
                    notes.append("could not start usbipd: %s" % e)
            for _ in range(20):
                if self.usbipd_running():
                    break
                time.sleep(0.1)
        for n in notes:
            log.info(n)
        return notes

    def usbip_info(self):
        argv, ok, err = self.resolve_usbip()[:3]
        wrapper = KERNEL_TOOLS_MSG in err
        return {
            # True when usbip really runs ("usbip version"), not only when /usr/bin/usbip exists.
            "usbip_installed": ok,
            "usbip_path": " ".join(argv),
            "usbip_error": None if ok else (err[:500] or "usbip does not run"),
            # Also set when the agent works around a broken wrapper with the real binary.
            "usbip_hint": kernel_tools_hint() if wrapper else
            None if ok else "install usbip: re-run install.sh with --install-deps",
            "usbipd_running": self.usbipd_running(),
            "module_loaded": self.module_loaded(),
        }

    # -- sysfs ---------------------------------------------------------------
    def _attr(self, busid, name):
        return read_text(os.path.join(self.base, busid, name))

    def _driver(self, path):
        link = os.path.join(path, "driver")
        try:
            return os.path.basename(os.readlink(link))
        except OSError:
            return None

    def _interfaces(self, busid, entries):
        result = []
        prefix = busid + ":"
        for e in sorted(entries):
            if not e.startswith(prefix):
                continue
            path = os.path.join(self.base, e)
            cls = (read_text(os.path.join(path, "bInterfaceClass")) or "").lower()
            ttys, nets = [], []
            try:
                children = os.listdir(path)
            except OSError:
                children = []
            for c in children:
                if c.startswith("tty") and c != "tty":
                    ttys.append(c)
            for sub, target in (("tty", ttys), ("net", nets)):
                try:
                    target.extend(os.listdir(os.path.join(path, sub)))
                except OSError:
                    pass
            result.append({
                "id": e,
                "class": cls,
                "class_name": USB_CLASSES.get(cls, cls),
                "driver": self._driver(path),
                "ttys": sorted(set(ttys)),
                "net": sorted(set(nets)),
            })
        return result

    def _device(self, busid, entries):
        path = os.path.join(self.base, busid)
        vid = (self._attr(busid, "idVendor") or "").lower()
        pid = (self._attr(busid, "idProduct") or "").lower()
        if not vid:
            return None
        manufacturer = self._attr(busid, "manufacturer")
        product = self._attr(busid, "product")
        db_vendor, db_product = None, None
        if not manufacturer or not product:
            db_vendor, db_product = self.ids.lookup(vid, pid)
        manufacturer = manufacturer or db_vendor
        product = product or db_product
        cls = (self._attr(busid, "bDeviceClass") or "").lower()
        driver = self._driver(path)
        status = self._attr(busid, "usbip_status")
        bound = driver == "usbip-host"
        state = USBIP_STATUS.get(status, "bound") if bound else "available"
        interfaces = self._interfaces(busid, entries)
        ttys = sorted({t for i in interfaces for t in i["ttys"]})
        nets = sorted({n for i in interfaces for n in i["net"]})
        name = product or "%s:%s" % (vid, pid)
        if manufacturer and product and not product.lower().startswith(manufacturer.lower()):
            name = "%s %s" % (manufacturer, product)
        dev = {
            "busid": busid,
            "vid": vid,
            "pid": pid,
            "vidpid": "%s:%s" % (vid, pid),
            "manufacturer": manufacturer,
            "product": product,
            "name": name,
            "serial": self._attr(busid, "serial"),
            "busnum": int(self._attr(busid, "busnum") or 0),
            "devnum": int(self._attr(busid, "devnum") or 0),
            "speed": self._attr(busid, "speed"),
            "usb_version": (self._attr(busid, "version") or "").strip(),
            "class": cls,
            "class_name": USB_CLASSES.get(cls, cls),
            "is_hub": cls == "09",
            "driver": driver,
            "bound": bound,
            "state": state,
            "interfaces": interfaces,
            "ttys": ttys,
            "net": nets,
        }
        dev["protected"] = any(match_rule(r, dev) for r in self.cfg.get("protected") or [])
        nicks = self.cfg.get("nicknames") or {}
        dev["nickname"] = nicks.get(device_rule(dev).lower()) or next(
            (n for r, n in nicks.items() if match_rule(r, dev)), None)
        dev["auto_bind"] = any(match_rule(r, dev) for r in self.cfg.get("auto_bind") or [])
        return dev

    def list(self, include_hubs=False):
        try:
            entries = os.listdir(self.base)
        except OSError:
            return []
        devices = []
        for e in entries:
            if not BUSID_RE.match(e):
                continue  # root hubs ("usb1") and interfaces ("1-1:1.0")
            dev = self._device(e, entries)
            if dev is None or (dev["is_hub"] and not include_hubs):
                continue
            devices.append(dev)
        devices.sort(key=lambda d: busid_key(d["busid"]))
        return devices

    def get(self, busid):
        if not isinstance(busid, str) or not BUSID_RE.match(busid):
            raise ApiError(400, "invalid busid %r" % (busid,))
        try:
            entries = os.listdir(self.base)
        except OSError:
            entries = []
        if busid not in entries:
            raise ApiError(404, "no USB device with busid %s" % busid)
        dev = self._device(busid, entries)
        if dev is None:
            raise ApiError(404, "no USB device with busid %s" % busid)
        return dev

    # -- actions ---------------------------------------------------------------
    def bind(self, busid, force=False):
        with self.lock:
            dev = self.get(busid)
            if dev["bound"]:
                return {"device": dev, "message": "already bound"}
            if dev["protected"] and not force:
                raise ApiError(403, "device %s (%s) is protected on this PC" % (busid, dev["name"]))
            if dev["net"] and not force:
                raise ApiError(409, "device %s has network interface(s) %s. Binding it can disconnect "
                                    "this PC. Use force to bind it." % (busid, ", ".join(dev["net"])))
            if dev["is_hub"] and not force:
                raise ApiError(409, "device %s is a hub. Use force to bind it." % busid)
            self.ensure_usbipd()
            rc, out = self.usbip("bind", "-b", busid)
            dev = self.get(busid)
            if not dev["bound"]:
                raise ApiError(500, "usbip bind failed (rc=%d): %s" % (rc, out))
            log.info("bound %s (%s)", busid, dev["name"])
            return {"device": dev, "message": out}

    def unbind(self, busid):
        with self.lock:
            dev = self.get(busid)
            if not dev["bound"]:
                return {"device": dev, "message": "not bound"}
            rc, out = self.usbip("unbind", "-b", busid)
            dev = self.get(busid)
            if dev["bound"]:
                raise ApiError(500, "usbip unbind failed (rc=%d): %s" % (rc, out))
            log.info("unbound %s (%s)", busid, dev["name"])
            return {"device": dev, "message": out}

    def reset(self, busid):
        dev = self.get(busid)
        node = os.path.join(self.cfg["devfs_root"], "bus", "usb", "%03d" % dev["busnum"], "%03d" % dev["devnum"])
        try:
            fd = os.open(node, os.O_WRONLY)
        except OSError as e:
            raise ApiError(500, "cannot open %s: %s" % (node, e))
        try:
            fcntl.ioctl(fd, USBDEVFS_RESET, 0)
        except OSError as e:
            raise ApiError(500, "USB reset of %s failed: %s" % (busid, e))
        finally:
            os.close(fd)
        log.info("reset %s (%s)", busid, dev["name"])
        return {"message": "reset sent to %s" % busid}


# --------------------------------------------------------------------------- status

class CpuSampler:
    def __init__(self):
        self.percent = None
        self._prev = None
        t = threading.Thread(target=self._loop, name="cpu", daemon=True)
        t.start()

    @staticmethod
    def _read():
        line = (read_text("/proc/stat") or "").splitlines()
        if not line:
            return None
        vals = [int(x) for x in line[0].split()[1:]]
        idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
        return sum(vals), idle

    def _loop(self):
        while True:
            cur = self._read()
            if cur and self._prev:
                total = cur[0] - self._prev[0]
                idle = cur[1] - self._prev[1]
                if total > 0:
                    self.percent = round(100.0 * (total - idle) / total, 1)
            self._prev = cur
            time.sleep(2)


def os_release():
    data = {}
    for line in (read_text("/etc/os-release") or "").splitlines():
        k, _, v = line.partition("=")
        data[k] = v.strip().strip('"')
    return data.get("PRETTY_NAME") or "Linux"


def meminfo():
    data = {}
    for line in (read_text("/proc/meminfo") or "").splitlines():
        k, _, v = line.partition(":")
        try:
            data[k] = int(v.split()[0]) * 1024
        except (ValueError, IndexError):
            pass
    total = data.get("MemTotal", 0)
    avail = data.get("MemAvailable", data.get("MemFree", 0))
    return {"total": total, "available": avail, "used": total - avail}


def ip_addresses():
    addrs = []
    if shutil.which("ip"):
        try:
            p = subprocess.run(["ip", "-j", "addr"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=3)
            for iface in json.loads(p.stdout.decode() or "[]"):
                name = iface.get("ifname")
                if name == "lo":
                    continue
                for a in iface.get("addr_info", []):
                    if a.get("scope") == "link":
                        continue
                    addrs.append({"iface": name, "addr": a.get("local"), "family": a.get("family"),
                                  "prefix": a.get("prefixlen")})
        except (OSError, ValueError, subprocess.TimeoutExpired):
            pass
    if not addrs:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("10.255.255.255", 1))
            addrs.append({"iface": "", "addr": s.getsockname()[0], "family": "inet", "prefix": None})
            s.close()
        except OSError:
            pass
    return addrs


def temperatures():
    temps = []
    base = "/sys/class/thermal"
    try:
        zones = sorted(os.listdir(base))
    except OSError:
        return temps
    for z in zones:
        if not z.startswith("thermal_zone"):
            continue
        t = read_text(os.path.join(base, z, "temp"))
        if t and t.lstrip("-").isdigit():
            temps.append({"zone": read_text(os.path.join(base, z, "type")) or z, "celsius": int(t) / 1000.0})
    return temps


# --------------------------------------------------------------------------- commands & shell

def shell_argv(cfg, user, command=None):
    shell = cfg.get("shell") or "/bin/bash"
    if not os.path.exists(shell):
        shell = "/bin/sh"
    me = current_user()
    if user and user != me:
        if os.geteuid() != 0:
            raise ApiError(403, "agent is not root, it cannot run as user %s" % user)
        try:
            pwd.getpwnam(user)
        except KeyError:
            raise ApiError(400, "unknown user %s" % user)
        if command is not None:
            # Not a login shell (-l): that would drop the environment (SM_* of flash and identify)
            # and the working directory that Popen sets.
            if shutil.which("runuser"):
                return ["runuser", "-u", user, "--", shell, "-c", command], user_home(user)
            return ["su", "-p", "-s", shell, user, "-c", command], user_home(user)
        tool = "runuser" if shutil.which("runuser") else "su"
        return [tool, "-l", user], user_home(user)
    if command is not None:
        return [shell, "-lc", command], user_home(me)
    return [shell, "-l"], user_home(me)


def base_env(user):
    env = dict(os.environ)
    try:
        pw = pwd.getpwnam(user)
        env.update({"HOME": pw.pw_dir, "USER": pw.pw_name, "LOGNAME": pw.pw_name})
    except KeyError:
        pass
    env.setdefault("PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin")
    env.setdefault("LANG", "C.UTF-8")
    env["TERM"] = "xterm-256color"
    return env


def num(value, name, kind=float):
    """A request parameter as a number, or a 400 error that names it."""
    try:
        return kind(value)
    except (TypeError, ValueError):
        raise ApiError(400, "%s must be a number" % name)


def run_command(cfg, command, timeout=60, cwd=None, user=None, extra_env=None):
    if not isinstance(command, str) or not command.strip():
        raise ApiError(400, "command is required")
    timeout = num(timeout or 60, "timeout")
    timeout = max(1.0, min(timeout, float(cfg.get("exec_timeout_max") or 3600)))
    user = user or cfg.get("run_as") or current_user()
    argv, home = shell_argv(cfg, user, command)
    start = time.time()
    try:
        p = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             cwd=cwd or home, env=dict(base_env(user), **(extra_env or {})),
                             start_new_session=True)
    except OSError as e:
        raise ApiError(500, "cannot start command: %s" % e)
    timed_out = False
    try:
        out, err = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except OSError:
            pass
        try:
            out, err = p.communicate(timeout=2)
        except subprocess.TimeoutExpired as e:
            # A grandchild left the process group but still holds our pipes: do not wait for it.
            out, err = e.stdout or b"", e.stderr or b""
            p.stdout.close()
            p.stderr.close()
            p.kill()
            p.wait()
    truncated = len(out) > MAX_OUTPUT or len(err) > MAX_OUTPUT
    log.info("exec as %s (rc=%s, %.1fs): %s", user, p.returncode, time.time() - start, command[:200])
    return {
        "command": command,
        "user": user,
        "exit_code": p.returncode,
        "stdout": out[-MAX_OUTPUT:].decode(errors="replace"),
        "stderr": err[-MAX_OUTPUT:].decode(errors="replace"),
        "timed_out": timed_out,
        "truncated": truncated,
        "duration": round(time.time() - start, 3),
    }


# --------------------------------------------------------------------------- MCU identification (serial capture)

TCGETS2 = 0x802C542A
TCSETS2 = 0x402C542B
BOTHER = 0o010000
CBAUD = 0o010017
AUTO_BAUDS = [115200, 74880, 9600, 57600, 230400, 460800, 921600]
RESET_METHODS = {
    "auto": "Choose from the USB IDs",
    "rts": "ESP32/ESP8266 style: pulse RTS (EN pin)",
    "dtr": "Arduino style: pulse DTR",
    "rts+dtr": "Both: pulse RTS, then DTR",
    "repl": "MicroPython/CircuitPython soft reset (Ctrl-C, Ctrl-D)",
    "probe": "Debug probe tool (st-flash or pyocd)",
    "usb": "USB port reset of the device",
    "none": "No reset (only the scripts)",
}
ST_LINK_PIDS = {"3744", "3748", "374a", "374b", "374d", "374e", "374f", "3752", "3753", "3754", "3757"}
USB_BRIDGES = {("10c4", "ea60"), ("1a86", "55d4"), ("1a86", "7523"), ("1a86", "7522"), ("0403", "6001"),
               ("0403", "6010"), ("0403", "6014"), ("0403", "6015"), ("067b", "2303")}


def is_debug_probe(dev):
    vid, pid, prod = dev.get("vid"), dev.get("pid"), (dev.get("product") or "").lower()
    return ((vid == "0483" and pid in ST_LINK_PIDS) or vid in ("0d28", "1366") or (vid, pid) == ("2e8a", "000c")
            or "cmsis-dap" in prod or "daplink" in prod)


def auto_reset_method(dev):
    vid, pid = dev.get("vid"), dev.get("pid")
    if vid == "303a":
        return "rts"
    if (vid, pid) in USB_BRIDGES:
        return "rts+dtr"
    if vid in ("2341", "2a03"):
        return "dtr"
    if is_debug_probe(dev):
        return "probe"
    if (vid, pid) in (("2e8a", "0005"),) or vid == "239a":
        return "repl"
    return "none"


def _set_serial(fd, baud):
    iflag, oflag, cflag, lflag, ispeed, ospeed, cc = termios.tcgetattr(fd)
    iflag &= ~(termios.IGNBRK | termios.BRKINT | termios.PARMRK | termios.ISTRIP | termios.INLCR | termios.IGNCR
               | termios.ICRNL | termios.IXON | termios.IXOFF | termios.IXANY)
    oflag &= ~termios.OPOST
    lflag &= ~(termios.ECHO | termios.ECHONL | termios.ICANON | termios.ISIG | termios.IEXTEN)
    cflag &= ~(termios.CSIZE | termios.PARENB | termios.CSTOPB | getattr(termios, "CRTSCTS", 0))
    cflag |= termios.CS8 | termios.CREAD | termios.CLOCAL
    cc[termios.VMIN] = 0
    cc[termios.VTIME] = 0
    speed = getattr(termios, "B%d" % baud, None)
    termios.tcsetattr(fd, termios.TCSANOW, [iflag, oflag, cflag, lflag, speed or termios.B38400,
                                             speed or termios.B38400, cc])
    if speed is None:  # a non-standard rate (e.g. 74880 for the ESP8266 ROM): Linux termios2
        buf = bytearray(44)
        fcntl.ioctl(fd, TCGETS2, buf)
        i, o, c, l_, line, ccs, _isp, _osp = struct.unpack("IIIIB19sII", bytes(buf))
        c = (c & ~CBAUD & ~(CBAUD << 16)) | BOTHER | (BOTHER << 16)
        fcntl.ioctl(fd, TCSETS2, struct.pack("IIIIB19sII", i, o, c, l_, line, ccs, baud, baud))


def _modem(fd, line, on):
    bit = termios.TIOCM_DTR if line == "dtr" else termios.TIOCM_RTS
    fcntl.ioctl(fd, termios.TIOCMBIS if on else termios.TIOCMBIC, struct.pack("I", bit))


def readable_ratio(data):
    if not data:
        return 0.0
    ok = sum(1 for b in data if 32 <= b < 127 or b in (9, 10, 13, 27))
    return round(ok / float(len(data)), 3)


class SerialCapture:
    """Read one or more serial ports in a thread. Reopens a port that disappears (re-enumeration)."""

    def __init__(self, paths, baud, max_bytes=256 * 1024):
        self.paths = list(paths)
        self.baud = baud
        self.max_bytes = max_bytes
        self.data = dict((p, bytearray()) for p in self.paths)
        self.events = dict((p, []) for p in self.paths)
        self.fds = {}
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._loop, name="serial-capture", daemon=True)
        self.t0 = time.time()
        for p in self.paths:
            self._open(p, first=True)

    def _event(self, path, text):
        self.events[path].append("%.2fs %s" % (time.time() - self.t0, text))

    def _open(self, path, first=False):
        try:
            fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        except OSError as e:
            if first:
                self._event(path, "cannot open: %s" % e)
            return False
        try:
            _set_serial(fd, self.baud)
            termios.tcflush(fd, termios.TCIFLUSH)
        except (OSError, termios.error) as e:
            self._event(path, "cannot set %d baud: %s" % (self.baud, e))
        with self.lock:
            self.fds[path] = fd
        if not first:
            self._event(path, "reopened")
        return True

    def start(self):
        self.thread.start()
        return self

    def _loop(self):
        last_try = 0.0
        while not self.stop_event.is_set():
            with self.lock:
                fds = dict(self.fds)
            missing = [p for p in self.paths if p not in fds]
            if missing and time.time() - last_try > 0.2:
                last_try = time.time()
                for p in missing:
                    if os.path.exists(p):
                        self._open(p)
            if not fds:
                time.sleep(0.05)
                continue
            try:
                ready, _, _ = select.select(list(fds.values()), [], [], 0.1)
            except (OSError, ValueError):
                ready = []
            for path, fd in fds.items():
                if fd not in ready:
                    continue
                try:
                    chunk = os.read(fd, 4096)
                except BlockingIOError:
                    continue
                except OSError:
                    chunk = b""
                if not chunk:
                    self._event(path, "port disconnected")
                    self._close(path)
                    continue
                buf = self.data[path]
                if len(buf) < self.max_bytes:
                    buf.extend(chunk[: self.max_bytes - len(buf)])

    def _close(self, path):
        with self.lock:
            fd = self.fds.pop(path, None)
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass

    def modem(self, line, on):
        errors = []
        with self.lock:
            fds = dict(self.fds)
        for path, fd in fds.items():
            try:
                _modem(fd, line, on)
            except OSError as e:
                errors.append("%s: %s" % (os.path.basename(path), e.strerror or e))
        return errors

    def write(self, data):
        with self.lock:
            fds = dict(self.fds)
        for fd in fds.values():
            try:
                os.write(fd, data)
            except OSError:
                pass

    def close(self):
        self.stop_event.set()
        if self.thread.is_alive():
            self.thread.join(2)
        for p in list(self.fds):
            self._close(p)

    def total(self):
        return sum(len(b) for b in self.data.values())

    def all_bytes(self):
        return b"".join(bytes(b) for b in self.data.values())


def do_reset(method, cap, hw_reset=None):
    """Reset the MCU. Returns a list of notes."""
    notes = []

    def pulse(line, seconds=0.1):
        errs = cap.modem(line, True)
        time.sleep(seconds)
        errs += cap.modem(line, False)
        return errs

    if method in ("rts", "dtr", "rts+dtr"):
        errs = cap.modem("rts", False) + cap.modem("dtr", False)
        time.sleep(0.05)
        if method in ("rts", "rts+dtr"):
            errs += pulse("rts")
        if method == "rts+dtr":
            time.sleep(0.3)
        if method in ("dtr", "rts+dtr"):
            errs += pulse("dtr")
        if errs:
            notes.append("modem lines not supported: %s" % "; ".join(sorted(set(errs))))
        else:
            notes.append("reset with %s" % method.upper())
    elif method == "repl":
        cap.write(b"\r\x03\x03")
        time.sleep(0.2)
        cap.write(b"\x04")
        notes.append("sent Ctrl-C, Ctrl-C, Ctrl-D (soft reset)")
    elif method in ("probe", "usb"):
        if hw_reset is None:
            notes.append("%s reset is not available here" % method)
        else:
            notes.append(hw_reset(method))
    elif method == "none":
        notes.append("no reset")
    else:
        raise ApiError(400, "unknown reset method %r (use one of: %s)" % (method, ", ".join(RESET_METHODS)))
    return notes


def identify_capture(dev, paths, baud="auto", duration=4.0, reset="auto", script="", after_script="",
                     run_script=None, hw_reset=None, script_timeout=30, extra_env=None, max_attempts=4,
                     capture_factory=None):
    """Start capturing the serial port(s), run the start script, reset, run the after-reset script,
    capture for `duration` seconds. With baud="auto", try other baud rates while the text is garbage."""
    try:
        duration = max(0.5, min(120.0, float(duration or 4)))
    except (TypeError, ValueError):
        raise ApiError(400, "duration must be a number")
    method = auto_reset_method(dev) if (reset or "auto") == "auto" else reset
    if method not in RESET_METHODS:
        raise ApiError(400, "unknown reset method %r (use one of: %s)" % (method, ", ".join(RESET_METHODS)))
    if str(baud or "auto") == "auto":
        bauds = AUTO_BAUDS[:max_attempts] if paths else [115200]
    else:
        try:
            bauds = [int(baud)]
        except ValueError:
            raise ApiError(400, "baud must be a number or 'auto'")
    result = {"reset": method, "reset_requested": reset or "auto", "ttys": [os.path.basename(p) for p in paths],
              "attempts": [], "scripts": [], "notes": []}
    if not paths:
        result["notes"].append("this device has no serial port: no log to capture")
    best = None
    for b in bauds:
        cap = (capture_factory or SerialCapture)(paths, b).start()
        env = {"SM_TTY": paths[0] if paths else "", "SM_TTYS": " ".join(paths), "SM_BAUD": str(b),
               "SM_BUSID": dev.get("busid", ""), "SM_VID": dev.get("vid", ""), "SM_PID": dev.get("pid", ""),
               "SM_SERIAL": dev.get("serial") or "", "SM_RESET": method}
        env.update(extra_env or {})
        notes = []
        try:
            if script and run_script:
                r = run_script(script, dict(env, SM_PHASE="start"), script_timeout)
                r["phase"] = "start"
                result["scripts"].append(r)
            notes += do_reset(method, cap, hw_reset)
            after = {}
            t = None
            if after_script and run_script:
                def run_after():
                    after.update(run_script(after_script, dict(env, SM_PHASE="after_reset"), script_timeout))
                t = threading.Thread(target=run_after, daemon=True)
                t.start()
            time.sleep(duration)
            if t is not None:
                t.join(script_timeout)
                after["phase"] = "after_reset"
                result["scripts"].append(after)
            time.sleep(0.2)
        finally:
            cap.close()
        data = cap.all_bytes()
        attempt = {"baud": b, "bytes": len(data), "readable": readable_ratio(data), "notes": notes,
                   "events": dict((os.path.basename(p), e) for p, e in cap.events.items() if e)}
        result["attempts"].append(attempt)
        score = attempt["readable"] * min(1.0, len(data) / 64.0)
        if best is None or score > best[0]:
            best = (score, attempt, cap)
        if len(bauds) == 1 or len(data) < 16 or attempt["readable"] >= 0.9:
            break
    _, attempt, cap = best
    result["baud"] = attempt["baud"]
    result["notes"] += attempt["notes"]
    result["captures"] = [{"tty": os.path.basename(p), "bytes": len(cap.data[p]),
                           "text": bytes(cap.data[p]).decode("utf-8", "replace")} for p in paths]
    parts = []
    for c in result["captures"]:
        if len(result["captures"]) > 1:
            parts.append("===== /dev/%s =====" % c["tty"])
        parts.append(c["text"])
    result["log"] = "\n".join(parts)
    return result


def probe_command(dev, action):
    """The command line to reset ('reset') or describe ('info') the target of a debug probe, or None."""
    vid, pid, sn = dev.get("vid"), dev.get("pid"), dev.get("serial") or ""
    st_link = vid == "0483" and pid in ST_LINK_PIDS
    if action == "info":
        if st_link and shutil.which("st-info"):
            return ["st-info", "--probe"] + (["--serial", sn] if sn else [])
        return None
    if st_link and shutil.which("st-flash"):
        return ["st-flash"] + (["--serial", sn] if sn else []) + ["reset"]
    if is_debug_probe(dev) and vid != "1366" and shutil.which("pyocd"):
        return ["pyocd", "reset"] + (["-u", sn] if sn else [])
    return None


def run_probe(dev, action, timeout=30):
    cmd = probe_command(dev, action)
    if cmd is None:
        return None, ("no tool found for this probe (install stlink-tools or pyocd), or use a start script"
                      if action == "reset" else "")
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)
        out = p.stdout.decode(errors="replace").strip()
        if p.returncode != 0 and action == "info" and "--serial" in cmd:
            p = subprocess.run(cmd[:2], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)
            out = p.stdout.decode(errors="replace").strip()
        return p.returncode, "%s (rc=%d)%s" % (" ".join(cmd), p.returncode, (": " + out[-500:]) if out else "")
    except (OSError, subprocess.TimeoutExpired) as e:
        return -1, "%s failed: %s" % (" ".join(cmd), e)


# --------------------------------------------------------------------------- serial hub (live serial monitor)

class SerialSession:
    """One serial port kept open, with a ring buffer that has absolute byte offsets.

    Many clients can read the same port: each one keeps its own offset. The
    port is opened again when it disappears and comes back (re-enumeration).
    pause() closes the port for tools that need it alone (flashing), and
    resume() opens it again.
    """

    def __init__(self, path, baud=115200, max_bytes=2 * 1024 * 1024):
        self.path = path
        self.baud = int(baud)
        self.max_bytes = max_bytes
        self.buf = bytearray()
        self.start = 0
        self.fd = None  # owned by the reader thread: it is the only one that closes it while it runs
        self.want_close = False  # ask the reader thread to close the fd (pause)
        self.paused = False
        self.closed = False
        self.events = collections.deque(maxlen=200)
        self.cond = threading.Condition()
        self.opened_at = time.time()
        self.last_used = time.time()
        self.wlock = threading.Lock()
        if not self._open():
            raise ApiError(404, "cannot open %s" % path)
        self.thread = threading.Thread(target=self._loop, name="serial-%s" % os.path.basename(path), daemon=True)
        self.thread.start()

    @property
    def end(self):
        return self.start + len(self.buf)

    def _event(self, text):
        with self.cond:  # read() iterates the events under this lock
            self.events.append({"offset": self.end, "time": time.time(), "text": text})

    def _open(self):
        try:
            fd = os.open(self.path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        except OSError as e:
            self._event("cannot open: %s" % (e.strerror or e))
            return False
        try:
            _set_serial(fd, self.baud)
        except (OSError, termios.error) as e:
            self._event("cannot set %d baud: %s" % (self.baud, e))
        with self.cond:
            if self.fd is not None or self.paused:  # opened by someone else, or paused, meanwhile
                os.close(fd)
                return self.fd is not None
            self.fd = fd
        return True

    def _close_fd(self):
        """Close the fd now. Only when the reader thread is not running (or did not answer)."""
        with self.cond:
            self._close_locked()

    def _close_locked(self):
        fd, self.fd = self.fd, None
        self.want_close = False
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        self.cond.notify_all()

    def _loop(self):
        last_try = 0.0
        while not self.closed:
            with self.cond:
                if self.want_close:
                    self._close_locked()
                fd = self.fd
            if fd is None:
                if not self.paused and time.time() - last_try > 0.3 and os.path.exists(self.path):
                    last_try = time.time()
                    if self._open():
                        self._event("reconnected")
                time.sleep(0.05)
                continue
            try:
                ready, _, _ = select.select([fd], [], [], 0.2)
            except (OSError, ValueError):
                ready = []
                time.sleep(0.05)
            if not ready:
                continue
            with self.cond:
                if self.want_close or self.fd != fd:  # closed meanwhile: the number can belong to another file now
                    continue
                try:
                    chunk = os.read(fd, 65536)
                except BlockingIOError:
                    continue
                except OSError:
                    chunk = b""
                if not chunk:
                    self._close_locked()
                    self._event("port disconnected")
                    continue
                self.buf.extend(chunk)
                if len(self.buf) > self.max_bytes:
                    drop = len(self.buf) - self.max_bytes
                    del self.buf[:drop]
                    self.start += drop
                self.cond.notify_all()

    def read(self, since=0, max_bytes=65536, wait=0.0):
        """Data from offset `since` (0 = the oldest data that is still in the buffer)."""
        self.last_used = time.time()
        deadline = time.time() + max(0.0, min(30.0, float(wait or 0)))
        with self.cond:
            since = self.end if since is None or since < 0 else since
            reset = since > self.end  # from a session that was closed and opened again
            if reset:
                since = self.end
            while since >= self.end and not self.closed:
                left = deadline - time.time()
                if left <= 0:
                    break
                self.cond.wait(left)
            lost = since < self.start
            s = max(since, self.start)
            data = bytes(self.buf[s - self.start: s - self.start + max(1, int(max_bytes))])
            events = [e for e in self.events if e["offset"] >= since]
            r = {"data": data, "offset": s + len(data), "start": self.start, "end": self.end, "lost": lost,
                 "events": events, "connected": self.fd is not None, "paused": self.paused, "baud": self.baud}
            if reset:
                r["reset"] = True
            return r

    def write(self, data):
        self.last_used = time.time()
        with self.wlock:
            deadline = time.time() + 5
            while data:
                with self.cond:  # the reader thread closes the fd only under this lock
                    fd = self.fd
                    if fd is None:
                        raise ApiError(409, "%s is not connected%s" % (self.path,
                                                                       " (paused)" if self.paused else ""))
                    try:
                        n = os.write(fd, data)
                        data = data[n:]
                        continue
                    except BlockingIOError:
                        if time.time() > deadline:
                            raise ApiError(504, "write to %s timed out" % self.path)
                    except OSError as e:
                        raise ApiError(500, "write to %s failed: %s" % (self.path, e))
                try:
                    select.select([], [fd], [], 0.2)
                except (OSError, ValueError):
                    time.sleep(0.05)

    def set_baud(self, baud):
        baud = int(baud)
        if baud == self.baud:
            return
        self.baud = baud
        with self.cond:
            if self.fd is not None:
                try:
                    _set_serial(self.fd, baud)
                except (OSError, termios.error) as e:
                    raise ApiError(400, "cannot set %d baud: %s" % (baud, e))
        self._event("baud rate %d" % baud)

    def modem(self, line, on):
        with self.cond:
            if self.fd is None:
                return ["%s: not connected" % os.path.basename(self.path)]
            try:
                _modem(self.fd, line, on)
                return []
            except OSError as e:
                return ["%s: %s" % (os.path.basename(self.path), e.strerror or e)]

    def pause(self, why="paused"):
        """Close the port (for flashing). Returns when the reader thread has let go of it."""
        with self.cond:
            self.paused = True
            self.want_close = True
            self.cond.notify_all()
            deadline = time.time() + 0.3
            while self.fd is not None and self.thread.is_alive() and time.time() < deadline:
                self.cond.wait(0.05)
            self._close_locked()  # only needed when the reader thread did not answer
        self._event(why)

    def resume(self):
        if not self.paused:
            return
        with self.cond:
            self.paused = False
            self.want_close = False
        for _ in range(50):  # the port can need some time to come back after flashing
            if os.path.exists(self.path) and self._open():
                self._event("resumed")
                return
            time.sleep(0.1)
        self._event("resumed, waiting for the port")

    def close(self):
        self.closed = True
        with self.cond:
            self.cond.notify_all()
        # Stop the reader before the fd is closed: else a new session can get the same fd
        # number while the old thread still reads it, and the old thread takes its data.
        if threading.current_thread() is not self.thread:
            self.thread.join(1.0)
        self._close_fd()

    def info(self):
        return {"tty": os.path.basename(self.path), "path": self.path, "baud": self.baud, "end": self.end,
                "start": self.start, "connected": self.fd is not None, "paused": self.paused,
                "opened_at": self.opened_at, "last_used": self.last_used}


class SerialHub:
    def __init__(self, idle_timeout=900):
        self.sessions = {}
        self.lock = threading.Lock()
        self.idle_timeout = idle_timeout
        threading.Thread(target=self._reaper, name="serial-reaper", daemon=True).start()

    def open(self, path, baud=None):
        with self.lock:
            s = self.sessions.get(path)
            if s is None or s.closed:
                s = SerialSession(path, baud or 115200)
                self.sessions[path] = s
                log.info("serial %s opened at %d baud", path, s.baud)
            elif baud:
                s.set_baud(baud)
            s.last_used = time.time()
            return s

    def get(self, path):
        s = self.sessions.get(path)
        if s is None or s.closed:
            raise ApiError(404, "%s is not open (open it first)" % os.path.basename(path))
        return s

    def close(self, path):
        with self.lock:
            s = self.sessions.pop(path, None)
        if s:
            s.close()
            log.info("serial %s closed", path)
        return {"closed": bool(s)}

    def pause(self, paths, why):
        paused = []
        for p in paths:
            s = self.sessions.get(p)
            if s and not s.closed and not s.paused:
                s.pause(why)
                paused.append(s)
        return paused

    def list(self):
        return [s.info() for s in self.sessions.values() if not s.closed]

    def _reaper(self):
        while True:
            time.sleep(30)
            for path, s in list(self.sessions.items()):
                if time.time() - s.last_used > self.idle_timeout:
                    log.info("serial %s idle, closing", path)
                    self.close(path)


class HubCapture:
    """SerialCapture interface on top of the hub, so identify and the monitor share the port."""

    def __init__(self, hub, paths, baud):
        self.hub = hub
        self.paths = list(paths)
        self.sessions = {}
        self.created = []
        self.restore = {}  # path -> the baud rate the monitor had before, for sessions that stay open
        self.offsets = {}
        self.data = dict((p, bytearray()) for p in self.paths)
        self.events = dict((p, []) for p in self.paths)
        for p in self.paths:
            existing = hub.sessions.get(p)
            before = None if existing is None or existing.closed else existing.baud
            try:
                s = hub.open(p, baud)
            except ApiError as e:
                self.events[p].append(e.message)
                continue
            if before is None:
                self.created.append(p)
            else:
                self.restore[p] = before
            self.sessions[p] = s
            self.offsets[p] = s.end

    def start(self):
        return self

    def modem(self, line, on):
        errors = []
        for s in self.sessions.values():
            errors += s.modem(line, on)
        return errors

    def write(self, data):
        for s in self.sessions.values():
            try:
                s.write(data)
            except ApiError:
                pass

    def close(self):
        for p, s in self.sessions.items():
            r = s.read(self.offsets[p], 256 * 1024, 0)
            self.data[p] = bytearray(r["data"])
            self.events[p] = [e["text"] for e in r["events"]]
        for p in self.created:  # a port that the monitor did not use before: give it back
            self.hub.close(p)
        for p, baud in self.restore.items():  # the monitor keeps its own baud rate
            try:
                self.sessions[p].set_baud(baud)
            except ApiError:
                pass

    def all_bytes(self):
        return b"".join(bytes(b) for b in self.data.values())


# --------------------------------------------------------------------------- who uses a device (USB/IP clients)

def _hex_ip(h):
    if len(h) == 8:
        return socket.inet_ntop(socket.AF_INET, struct.pack("<I", int(h, 16)))
    raw = b"".join(struct.pack("<I", int(h[i:i + 8], 16)) for i in range(0, 32, 8))
    ip = socket.inet_ntop(socket.AF_INET6, raw)
    return ip[7:] if ip.startswith("::ffff:") else ip


def parse_proc_net_tcp(text, port=3240):
    """Remote IPs with an ESTABLISHED connection to the local `port` (from /proc/net/tcp or tcp6)."""
    ips = set()
    for line in text.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 4 or parts[3] != "01":
            continue
        try:
            lport = int(parts[1].rsplit(":", 1)[1], 16)
            if lport == port:
                ips.add(_hex_ip(parts[2].rsplit(":", 1)[0]))
        except (ValueError, IndexError, OSError):
            continue
    return ips


def usbip_client_ips(port=3240):
    ips = set()
    for name in ("/proc/net/tcp", "/proc/net/tcp6"):
        ips |= parse_proc_net_tcp(read_text(name) or "", port)
    return ips


_RDNS = {}


def reverse_name(ip):
    hit = _RDNS.get(ip)
    if hit and time.time() - hit[0] < 600:
        return hit[1]
    result = []

    def lookup():
        try:
            result.append(socket.gethostbyaddr(ip)[0].split(".")[0])
        except (OSError, UnicodeError):
            pass
    t = threading.Thread(target=lookup, daemon=True)
    t.start()
    t.join(0.5)  # do not block the request on a slow DNS server
    name = result[0] if result else None
    _RDNS[ip] = (time.time(), name)
    return name


# --------------------------------------------------------------------------- files (firmware upload)

def safe_path(cfg, path, create_dirs=False):
    """Relative paths go under files_dir and cannot leave it. Absolute paths are used as they are."""
    base = os.path.abspath(cfg.get("files_dir") or "/var/lib/setup-agent/files")
    if not path or not isinstance(path, str):
        raise ApiError(400, "path is required")
    if os.path.isabs(path):
        full = os.path.abspath(path)
    else:
        full = os.path.abspath(os.path.join(base, path))
        if full != base and not full.startswith(base + os.sep):
            raise ApiError(400, "path leaves the files folder: %s" % path)
    if create_dirs:
        os.makedirs(os.path.dirname(full), exist_ok=True)
    return full


def chown_to(user, path):
    if not user or os.geteuid() != 0:
        return
    try:
        pw = pwd.getpwnam(user)
        os.chown(path, pw.pw_uid, pw.pw_gid)
    except (KeyError, OSError):
        pass


# --------------------------------------------------------------------------- TLS front end and USB/IP tunnel
#
# With TLS on (the default), the HTTP server listens on 127.0.0.1 only. A TLS
# front end listens on the network port. For each connection it does the TLS
# handshake and then relays the bytes, in ONE thread per connection (OpenSSL
# does not allow one TLS connection to be used from two threads at the same time).
# A connection that starts with "SMTUNNEL <token> [leases]\n" is a USB/IP tunnel:
# it is relayed to the local usbipd, so USB/IP is encrypted and needs the token.

REAL_ADDR = {}  # local port of the relay's connection to the HTTP server -> real client address
TUNNEL_MAGIC = b"SMTUNNEL "
OP_REQ_IMPORT = 0x8003


def cert_fingerprint(cert_path):
    """SHA-256 of the certificate (DER), as lowercase hex. The manager pins this value."""
    with open(cert_path) as f:
        der = ssl.PEM_cert_to_DER_cert(f.read())
    return hashlib.sha256(der).hexdigest()


def ensure_cert(cfg, config_path):
    """Return (cert, key): the configured files, or a self-signed pair made with openssl at the first start."""
    if cfg.get("tls_cert") and cfg.get("tls_key"):
        return cfg["tls_cert"], cfg["tls_key"]
    folder = cfg.get("tls_dir") or (os.path.dirname(os.path.abspath(config_path)) if config_path else
                                    "/etc/setup-agent")
    cert, key = os.path.join(folder, "cert.pem"), os.path.join(folder, "key.pem")
    if os.path.exists(cert) and os.path.exists(key):
        return cert, key
    if not shutil.which("openssl"):
        raise ApiError(500, "openssl is not installed: cannot make a TLS certificate")
    os.makedirs(folder, exist_ok=True)
    cmd = ["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes",
           "-days", "3650", "-subj", "/CN=%s" % socket.gethostname(), "-keyout", key, "-out", cert]
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if p.returncode != 0:
        raise ApiError(500, "openssl failed: %s" % p.stdout.decode(errors="replace")[-300:])
    os.chmod(key, 0o600)
    log.info("made a TLS certificate %s (sha256 %s)", cert, cert_fingerprint(cert))
    return cert, key


def relay(a, b, first_to_b=b"", idle=None):
    """Copy bytes both ways between socket a (TLS or plain) and socket b, in this thread only."""
    a.setblocking(False)
    b.setblocking(False)
    to_b, to_a = bytearray(first_to_b), bytearray()
    a_eof = b_eof = False
    limit = 4 * 1024 * 1024
    last = time.time()
    while True:
        if (a_eof and not to_b) or (b_eof and not to_a):
            break
        rl = ([a] if not a_eof and len(to_b) < limit else []) + ([b] if not b_eof and len(to_a) < limit else [])
        wl = ([b] if to_b else []) + ([a] if to_a else [])
        pending = isinstance(a, ssl.SSLSocket) and not a_eof and a.pending() > 0
        try:
            r, w, _ = select.select(rl, wl, [], 0 if pending else 30)
        except (OSError, ValueError):
            break
        if not r and not w and not pending:
            if idle and time.time() - last > idle:
                break
            continue
        last = time.time()
        if (a in r or pending) and not a_eof:
            try:
                data = a.recv(65536)
                if data:
                    to_b += data
                else:
                    a_eof = True
            except (ssl.SSLWantReadError, ssl.SSLWantWriteError, BlockingIOError):
                pass
            except (OSError, ssl.SSLError):
                a_eof = True
        if b in r and not b_eof:
            try:
                data = b.recv(65536)
                if data:
                    to_a += data
                else:
                    b_eof = True
            except BlockingIOError:
                pass
            except OSError:
                b_eof = True
        try:
            if b in w and to_b:
                del to_b[:b.send(to_b)]
        except BlockingIOError:
            pass
        except OSError:
            break
        try:
            if a in w and to_a:
                del to_a[:a.send(to_a)]
        except (ssl.SSLWantReadError, ssl.SSLWantWriteError, BlockingIOError):
            pass
        except (OSError, ssl.SSLError):
            break
    for s in (a, b):
        try:
            s.close()
        except OSError:
            pass


class TlsFront:
    def __init__(self, agent, listen, port, cert, key, http_port):
        self.agent = agent
        self.http_port = http_port
        self.ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        self.ctx.load_cert_chain(cert, key)
        family = socket.AF_INET6 if ":" in listen else socket.AF_INET
        self.sock = socket.socket(family, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((listen, int(port)))
        self.sock.listen(64)

    def serve(self):
        while True:
            try:
                conn, addr = self.sock.accept()
            except OSError as e:
                if self.sock.fileno() < 0:  # the listening socket was closed: shutting down
                    return
                log.warning("TLS accept failed: %s", e)  # EMFILE, ENOBUFS, ECONNABORTED, ...: try again
                time.sleep(0.1)
                continue
            threading.Thread(target=self._handle, args=(conn, addr), name="tls", daemon=True).start()

    def _handle(self, conn, addr):
        try:
            conn.settimeout(10)
            tls = self.ctx.wrap_socket(conn, server_side=True)
            first = tls.recv(65536)  # the client speaks first (HTTP, WebSocket, tunnel)
        except (OSError, ssl.SSLError, ValueError) as e:
            log.debug("TLS handshake with %s failed: %s", addr[0], e)
            try:
                conn.close()
            except OSError:
                pass
            return
        if first.startswith(TUNNEL_MAGIC):
            return self._tunnel(tls, addr, first)
        try:
            back = socket.create_connection(("127.0.0.1", self.http_port), timeout=10)
        except OSError:
            tls.close()
            return
        local_port = back.getsockname()[1]
        REAL_ADDR[local_port] = addr
        try:
            relay(tls, back, first)
        finally:
            REAL_ADDR.pop(local_port, None)

    def _tunnel(self, tls, addr, first):
        try:
            tls.settimeout(30)
            self._tunnel_inner(tls, addr, first)
        except (OSError, ssl.SSLError, ValueError, struct.error) as e:
            log.debug("USB/IP tunnel from %s ended: %s", addr[0], e)
            try:
                tls.close()
            except OSError:
                pass

    def _tunnel_inner(self, tls, addr, first):
        """USB/IP over TLS: check the token (and the reservation of the device), then relay to usbipd."""
        a = self.agent
        line, sep, rest = first.partition(b"\n")
        while not sep and len(line) < 4096:
            more = tls.recv(4096)
            if not more:
                tls.close()
                return
            line, sep, rest = (line + more).partition(b"\n")
        parts = line.decode(errors="replace").split()
        token = parts[1] if len(parts) > 1 else ""
        leases = parts[2].split(",") if len(parts) > 2 else []
        ok = a.cfg.get("insecure_no_auth") or (a.cfg.get("token") and hmac.compare_digest(
            token.encode(), str(a.cfg["token"]).encode()))
        if not ok:
            log.warning("USB/IP tunnel from %s: wrong token", addr[0])
            tls.sendall(b"ERR unauthorized\n")
            tls.close()
            return
        # The first USB/IP request: for an import, check the reservation of the device.
        while len(rest) < 8:
            more = tls.recv(4096)
            if not more:
                tls.close()
                return
            rest += more
        busid = None
        if struct.unpack("!HH", rest[:4])[1] == OP_REQ_IMPORT:
            while len(rest) < 40:
                more = tls.recv(4096)
                if not more:
                    tls.close()
                    return
                rest += more
            busid = rest[8:40].split(b"\0", 1)[0].decode(errors="replace")
            try:
                a.check_lease(busid, leases)
            except ApiError as e:
                log.warning("USB/IP tunnel from %s refused: %s", addr[0], e.message)
                tls.sendall(b"ERR " + e.message.encode()[:300] + b"\n")
                tls.close()
                return
        try:
            back = socket.create_connection(("127.0.0.1", int(a.cfg.get("usbip_port") or 3240)), timeout=10)
        except OSError as e:
            tls.sendall(("ERR usbipd is not reachable: %s\n" % e).encode())
            tls.close()
            return
        tls.sendall(b"OK\n")
        tid = id(back)
        a.tunnels[tid] = {"ip": addr[0], "busid": busid, "at": time.time()}
        log.info("USB/IP tunnel from %s%s", addr[0], " for %s" % busid if busid else "")
        try:
            relay(tls, back, rest)
        finally:
            a.tunnels.pop(tid, None)


FW_COMMENT = "setup-agent-usbip"


def usbip_firewall(block, port=3240):
    """Block (or unblock) USB/IP from the network, so that it works only through the encrypted tunnel.

    The rule refuses the connection (TCP reset): a `usbip attach` by hand from another PC fails at
    once with "connection refused", instead of hanging for two minutes (older agents used DROP).
    """
    done = []
    match = ["INPUT", "-p", "tcp", "--dport", str(port), "!", "-i", "lo", "-m", "comment", "--comment", FW_COMMENT]
    rule = match + ["-j", "REJECT", "--reject-with", "tcp-reset"]
    old_rule = match + ["-j", "DROP"]

    def delete_all(tool, r):
        n = 0
        while subprocess.run([tool, "-D"] + r, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
            n += 1
        return n

    for tool in ("iptables", "ip6tables"):
        if not shutil.which(tool):
            continue
        replaced = delete_all(tool, old_rule)  # the DROP rule of an older agent
        if block:
            exists = subprocess.run([tool, "-C"] + rule, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if exists.returncode != 0:
                p = subprocess.run([tool, "-I"] + rule, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
                done.append("%s: %s" % (tool, "blocked" if p.returncode == 0 else p.stdout.decode(errors="replace")))
        else:
            n = replaced + delete_all(tool, rule)
            done += ["%s: unblocked" % tool] * n
    return done


class WebSocket:
    """Minimal RFC 6455 server-side WebSocket on top of a BaseHTTPRequestHandler connection."""

    MAX_MESSAGE = 16 * 1024 * 1024

    def __init__(self, rfile, sock):
        self.rfile = rfile
        self.sock = sock
        self.wlock = threading.Lock()
        self.closed = False

    def _read(self, n):
        data = b""
        while len(data) < n:
            chunk = self.rfile.read(n - len(data))
            if not chunk:
                raise EOFError("connection closed")
            data += chunk
        return data

    @staticmethod
    def _unmask(data, mask):
        if not data:
            return data
        m = (mask * (len(data) // 4 + 1))[:len(data)]
        return (int.from_bytes(data, "big") ^ int.from_bytes(m, "big")).to_bytes(len(data), "big")

    def recv(self):
        """Return (opcode, payload) of the next data message. Raises EOFError on close."""
        msg_op, buf = None, b""
        while True:
            b1, b2 = self._read(2)
            fin, op = b1 & 0x80, b1 & 0x0F
            ln = b2 & 0x7F
            if ln == 126:
                ln = struct.unpack("!H", self._read(2))[0]
            elif ln == 127:
                ln = struct.unpack("!Q", self._read(8))[0]
            if ln > self.MAX_MESSAGE:
                self.close(1009)
                raise EOFError("message too big")
            mask = self._read(4) if b2 & 0x80 else None
            data = self._read(ln)
            if mask:
                data = self._unmask(data, mask)
            if op == 0x8:
                self.close()
                raise EOFError("closed by peer")
            if op == 0x9:
                self.send(0xA, data)
                continue
            if op == 0xA:
                continue
            if op == 0x0:
                buf += data
            else:
                msg_op, buf = op, data
            if len(buf) > self.MAX_MESSAGE:
                self.close(1009)
                raise EOFError("message too big")
            if fin:
                return msg_op, buf

    def send(self, op, data):
        if isinstance(data, str):
            data = data.encode()
        ln = len(data)
        if ln < 126:
            header = struct.pack("!BB", 0x80 | op, ln)
        elif ln < 65536:
            header = struct.pack("!BBH", 0x80 | op, 126, ln)
        else:
            header = struct.pack("!BBQ", 0x80 | op, 127, ln)
        with self.wlock:
            if self.closed and op != 0x8:
                raise EOFError("closed")
            self.sock.sendall(header + data)

    def send_text(self, text):
        self.send(0x1, text)

    def send_binary(self, data):
        self.send(0x2, data)

    def close(self, code=1000):
        with self.wlock:
            if self.closed:
                return
            self.closed = True
            try:
                self.sock.sendall(struct.pack("!BBH", 0x88, 2, code))
            except OSError:
                pass


def set_winsize(fd, rows, cols):
    try:
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", int(rows), int(cols), 0, 0))
    except (OSError, ValueError):
        pass


def _write_all(fd, data):
    while data:
        n = os.write(fd, data)
        data = data[n:]


def shell_session(ws, cfg, cols=80, rows=24, user=None):
    user = user or cfg.get("run_as") or current_user()
    argv, home = shell_argv(cfg, user)
    master, slave = os.openpty()
    set_winsize(slave, rows, cols)

    def preexec():
        os.setsid()
        fcntl.ioctl(0, termios.TIOCSCTTY, 0)

    try:
        proc = subprocess.Popen(argv, stdin=slave, stdout=slave, stderr=slave, cwd=home, env=base_env(user),
                                preexec_fn=preexec, close_fds=True)
    except OSError as e:
        os.close(master)
        os.close(slave)
        ws.send_text(json.dumps({"t": "exit", "code": -1, "error": str(e)}))
        ws.close()
        return
    os.close(slave)
    log.info("shell opened as %s (pid %d)", user, proc.pid)
    stop = threading.Event()

    def pump():
        try:
            while not stop.is_set():
                r, _, _ = select.select([master], [], [], 0.5)
                if master not in r:
                    if proc.poll() is not None:
                        break
                    continue
                try:
                    data = os.read(master, 65536)
                except OSError:
                    break
                if not data:
                    break
                ws.send_binary(data)
        except (EOFError, OSError):
            pass
        finally:
            stop.set()
            try:
                code = proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                code = None
            try:
                ws.send_text(json.dumps({"t": "exit", "code": code}))
            except (EOFError, OSError):
                pass
            ws.close()
            # Unblock recv() when the peer does not answer the close frame.
            threading.Timer(5, _shutdown_socket, args=(ws.sock,)).start()

    t = threading.Thread(target=pump, name="pty-pump", daemon=True)
    t.start()
    try:
        while not stop.is_set():
            op, payload = ws.recv()
            if op == 0x2:
                _write_all(master, payload)
            elif op == 0x1:
                try:
                    msg = json.loads(payload.decode(errors="replace"))
                except ValueError:
                    continue
                if msg.get("t") == "i":
                    _write_all(master, str(msg.get("d", "")).encode())
                elif msg.get("t") == "r":
                    set_winsize(master, msg.get("r", 24), msg.get("c", 80))
    except (EOFError, OSError, ValueError):
        pass
    finally:
        stop.set()
        if proc.poll() is None:
            for sig in (signal.SIGHUP, signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(proc.pid, sig)
                except OSError:
                    break
                try:
                    proc.wait(timeout=1)
                    break
                except subprocess.TimeoutExpired:
                    continue
        t.join(3)
        try:
            os.close(master)
        except OSError:
            pass
        log.info("shell closed (pid %d)", proc.pid)


def _shutdown_socket(sock):
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass


# --------------------------------------------------------------------------- agent

class Agent:
    def __init__(self, cfg, config_path=None):
        self.cfg = cfg
        self.config_path = config_path
        self.usb = UsbManager(cfg)
        self.cpu = CpuSampler()
        self.started = time.time()
        self.cfg_lock = threading.Lock()
        self.identify_locks = {}
        self.hub = SerialHub(float(cfg.get("serial_idle_timeout") or 900))
        self.claims = {}
        self.reservations = {}
        self.tunnels = {}
        self.tls_fingerprint = None
        self.res_lock = threading.Lock()

    # -- API handlers ----------------------------------------------------------
    def ping(self):
        return {"service": "setup-agent", "name": self.cfg["name"], "hostname": socket.gethostname(),
                "tls": bool(self.tls_fingerprint), "tunnel": bool(self.tls_fingerprint),
                "fingerprint": self.tls_fingerprint,
                "version": VERSION}

    def status(self):
        uptime = read_text("/proc/uptime")
        disk = shutil.disk_usage("/")
        try:
            load = list(os.getloadavg())
        except OSError:
            load = None
        devices = self.usb.list()
        return {
            "name": self.cfg["name"],
            "hostname": socket.gethostname(),
            "version": VERSION,
            "os": os_release(),
            "kernel": os.uname().release,
            "arch": os.uname().machine,
            "uptime": float(uptime.split()[0]) if uptime else None,
            "agent_uptime": round(time.time() - self.started, 1),
            "load": load,
            "cpu_count": os.cpu_count(),
            "cpu_percent": self.cpu.percent,
            "memory": meminfo(),
            "disk": {"total": disk.total, "used": disk.used, "free": disk.free},
            "ips": ip_addresses(),
            "temperatures": temperatures(),
            "usbip": self.usb.usbip_info(),
            "usb_count": len(devices),
            "usb_bound": sum(1 for d in devices if d["bound"]),
            "usb_in_use": sum(1 for d in devices if d["state"] == "in_use"),
            "run_as": self.cfg.get("run_as") or current_user(),
            "agent_user": current_user(),
            "tls": bool(self.tls_fingerprint),
            "tunnel": bool(self.tls_fingerprint),
            "fingerprint": self.tls_fingerprint,
            "time": time.time(),
        }

    def usb_list(self, qs):
        include_hubs = qs.get("all", ["0"])[0] in ("1", "true", "yes")
        devices = self.usb.list(include_hubs=include_hubs)
        self.annotate_users(devices)
        self.annotate_reservations(devices)
        return {"devices": devices, "usbip": self.usb.usbip_info()}

    def annotate_users(self, devices):
        """Add "used_by" to the devices that a USB/IP client uses: from claims, else from TCP connections."""
        in_use = [d for d in devices if d["state"] == "in_use"]
        for busid in [b for b, c in self.claims.items()
                      if not any(d["busid"] == b for d in in_use) and time.time() - c["at"] > 15]:
            self.claims.pop(busid, None)  # detached: forget the claim (after a grace time for a slow attach)
        if not in_use:
            return
        ips = usbip_client_ips() - {"127.0.0.1", "::1"}  # loopback = our own tunnel relay
        ips |= {t["ip"] for t in self.tunnels.values()}
        claimed_ips = {c["ip"] for c in self.claims.values()}
        others = sorted(ips - claimed_ips)
        by_tunnel = dict((t["busid"], t) for t in self.tunnels.values() if t.get("busid"))
        for d in in_use:
            c = self.claims.get(d["busid"])
            t = by_tunnel.get(d["busid"])
            if c:
                d["used_by"] = dict(c, source="claim", connected=c["ip"] in ips if ips else None)
            elif t:
                d["used_by"] = {"ip": t["ip"], "host": reverse_name(t["ip"]), "source": "tunnel"}
            elif len(others) == 1:
                d["used_by"] = {"ip": others[0], "host": reverse_name(others[0]), "source": "tcp"}
            elif others:
                d["used_by"] = {"ips": others, "source": "tcp"}

    def claim(self, body, ip):
        busid = body.get("busid")
        self.usb.get(busid)
        self.claims[busid] = {"client": str(body.get("client") or ip)[:80], "ip": ip, "at": time.time()}
        return {"claimed": busid}

    def release(self, body):
        return {"released": bool(self.claims.pop(body.get("busid"), None))}

    # -- reservations (locks for automation) ----------------------------------------
    # A reservation is a lock on one device, with an owner, a lease (secret) and an
    # expiry time. Only while a device is reserved, the actions on it that change it
    # (bind, unbind, reset, identify, flash, serial write/reset) need the lease
    # (header X-SM-Lease). Devices that are not reserved work as before.
    def _res_key(self, dev):
        return device_rule(dev).lower()

    def _active(self, key):
        r = self.reservations.get(key)
        if r and r["expires"] < time.time():
            self.reservations.pop(key, None)
            log.info("reservation of %s by %s expired", key, r["owner"])
            return None
        return r

    @staticmethod
    def _public(r):
        return dict((k, v) for k, v in r.items() if k != "lease")

    def annotate_reservations(self, devices):
        with self.res_lock:
            for d in devices:
                r = self._active(self._res_key(d))
                if r:
                    d["reserved_by"] = self._public(r)

    def reserve(self, body):
        dev = self.usb.get(body.get("busid"))
        owner = str(body.get("owner") or "").strip()[:80]
        if not owner:
            raise ApiError(400, "owner is required")
        ttl = max(5.0, min(24 * 3600.0, num(body.get("ttl") or 300, "ttl")))
        key = self._res_key(dev)
        with self.res_lock:
            r = self._active(key)
            if r and not (body.get("lease") and hmac.compare_digest(str(body["lease"]), r["lease"])):
                raise ApiError(423, "%s is reserved by %s until %s" % (
                    dev["busid"], r["owner"], time.strftime("%H:%M:%S", time.localtime(r["expires"]))))
            if not r:
                r = {"key": key, "busid": dev["busid"], "owner": owner, "lease": secrets.token_urlsafe(24),
                     "at": time.time(), "note": str(body.get("note") or "")[:200]}
                log.info("%s reserved by %s for %ds", dev["busid"], owner, ttl)
            r["expires"] = time.time() + ttl
            r["busid"] = dev["busid"]
            self.reservations[key] = r
            return dict(r)

    def release_reservation(self, body):
        dev = self.usb.get(body.get("busid")) if body.get("busid") else None
        key = self._res_key(dev) if dev else str(body.get("key") or "")
        with self.res_lock:
            r = self._active(key)
            if not r:
                return {"released": False}
            if not body.get("force") and not hmac.compare_digest(str(body.get("lease") or ""), r["lease"]):
                raise ApiError(423, "the lease does not match the reservation of %s" % r["owner"])
            self.reservations.pop(key, None)
        log.info("reservation of %s by %s %s", key, r["owner"], "broken" if body.get("force") else "released")
        return {"released": True, "owner": r["owner"]}

    def list_reservations(self):
        with self.res_lock:
            return {"reservations": [self._public(r) for k, r in list(self.reservations.items())
                                     if self._active(k)]}

    def check_lease(self, busid, leases):
        """Raise 423 when the device is reserved and none of the given leases is its lease."""
        if not self.reservations or not busid:
            return
        try:
            dev = self.usb.get(busid)
        except ApiError:
            return
        with self.res_lock:
            r = self._active(self._res_key(dev))
        if r and not any(hmac.compare_digest(x, r["lease"]) for x in leases):
            raise ApiError(423, "%s is reserved by %s until %s" % (
                busid, r["owner"], time.strftime("%H:%M:%S", time.localtime(r["expires"]))))

    def busid_for_tty(self, tty):
        tty = os.path.basename(str(tty or ""))
        for d in self.usb.list(include_hubs=True):
            if tty in d["ttys"]:
                return d["busid"]
        return None

    def set_nickname(self, body):
        dev = self.usb.get(body.get("busid"))
        name = str(body.get("name") or "").strip()[:60]
        nicks = dict(self.cfg.get("nicknames") or {})
        for r in [r for r in nicks if match_rule(r, dev)]:
            nicks.pop(r)
        if name:
            nicks[device_rule(dev).lower()] = name
        self.update_config({"nicknames": nicks})
        return {"device": self.usb.get(dev["busid"])}

    # -- serial monitor ----------------------------------------------------------
    def serial_path(self, body):
        """The device path from {tty} or {busid[, tty]}. The device must not be shared over USB/IP."""
        tty = os.path.basename(str(body.get("tty") or ""))
        if body.get("busid"):
            dev = self.usb.get(body["busid"])
            if dev["bound"]:
                raise ApiError(409, "%s is shared over USB/IP: its serial port is on the client PC" % dev["busid"])
            if not dev["ttys"]:
                raise ApiError(404, "%s has no serial port" % dev["busid"])
            if tty and tty not in dev["ttys"]:
                raise ApiError(404, "%s has no port %s (ports: %s)" % (dev["busid"], tty, ", ".join(dev["ttys"])))
            tty = tty or dev["ttys"][0]
        if not re.match(r"^tty[A-Za-z]+[0-9]+$", tty or ""):
            raise ApiError(400, "invalid serial port %r" % tty)
        return os.path.join(self.cfg["devfs_root"], tty)

    def serial_open(self, body):
        s = self.hub.open(self.serial_path(body), body.get("baud"))
        return s.info()

    def serial_read(self, qs):
        q = dict((k, v[0]) for k, v in qs.items())
        s = self.hub.get(self.serial_path(q))
        r = s.read(num(q.get("since", 0), "since", int), num(q.get("max", 65536), "max", int),
                   num(q.get("wait", 0), "wait"))
        r["data"] = base64.b64encode(r["data"]).decode()
        return r

    def serial_write(self, body):
        s = self.hub.get(self.serial_path(body))
        data = base64.b64decode(body["data_b64"]) if body.get("data_b64") else str(body.get("data", "")).encode()
        s.write(data)
        return {"written": len(data)}

    def serial_reset(self, body):
        path = self.serial_path(body)
        s = self.hub.get(path)
        dev = None
        if body.get("busid"):
            dev = self.usb.get(body["busid"])
        method = body.get("method") or "auto"
        if method == "auto":
            method = auto_reset_method(dev or {})
        cap = HubCapture(self.hub, [path], s.baud)

        def hw_reset(m):
            if dev is None:
                return "%s reset needs the busid" % m
            if m == "usb":
                return self.usb.reset(dev["busid"])["message"]
            return run_probe(dev, "reset")[1]
        notes = do_reset(method, cap, hw_reset)
        return {"method": method, "notes": notes}

    # -- files and flashing --------------------------------------------------------
    def files_list(self, qs):
        path = safe_path(self.cfg, (qs.get("path") or [""])[0] or ".")
        if not os.path.isdir(path):
            raise ApiError(404, "no folder %s" % path)
        out = []
        for name in sorted(os.listdir(path)):
            full = os.path.join(path, name)
            try:
                st = os.stat(full)
            except OSError:  # a dangling symlink, or a file that disappeared
                try:
                    st = os.lstat(full)
                except OSError:
                    continue
            out.append({"name": name, "size": st.st_size, "mtime": st.st_mtime, "dir": os.path.isdir(full)})
        return {"path": path, "files": out}

    def files_delete(self, body):
        path = safe_path(self.cfg, body.get("path"))
        if os.path.isdir(path):
            raise ApiError(400, "%s is a folder" % path)
        os.remove(path)
        return {"deleted": path}

    def flash(self, body):
        """Run a flash script for a device: $SM_FILE is the firmware, $SM_TTY the serial port."""
        dev = self.usb.get(body.get("busid"))
        if dev["bound"]:
            raise ApiError(409, "%s is shared over USB/IP: stop sharing it first" % dev["busid"])
        script = body.get("script") or ""
        if not script.strip():
            raise ApiError(400, "script is required")
        fw = safe_path(self.cfg, body["file"]) if body.get("file") else ""
        if fw and not os.path.isfile(fw):
            raise ApiError(404, "no file %s on this PC" % fw)
        paths = [os.path.join(self.cfg["devfs_root"], t) for t in dev["ttys"]]
        env = {"SM_FILE": fw, "SM_FILE_EXT": os.path.splitext(fw)[1].lstrip(".").lower(),
               "SM_TTY": paths[0] if paths else "", "SM_TTYS": " ".join(paths), "SM_BUSID": dev["busid"],
               "SM_VID": dev["vid"], "SM_PID": dev["pid"], "SM_SERIAL": dev.get("serial") or "",
               "SM_SETUP": self.cfg["name"], "SM_PHASE": "flash"}
        env.update(dict((str(k), str(v)) for k, v in (body.get("env") or {}).items()))
        lock = self.identify_locks.setdefault(dev["busid"], threading.Lock())
        if not lock.acquire(False):
            raise ApiError(409, "%s is busy (identify or flash running)" % dev["busid"])
        paused = self.hub.pause(paths, "paused for flashing")
        starts = dict((ps.path, ps.end) for ps in paused)  # the monitor shows what comes after the flash
        try:
            result = run_command(self.cfg, script, num(body.get("timeout") or 300, "timeout"), None,
                                 body.get("user"), extra_env=env)
        finally:
            for s in paused:
                s.resume()
            lock.release()
        log.info("flash %s with %s: rc=%s", dev["busid"], os.path.basename(fw) or "-", result["exit_code"])
        result["env"] = env
        monitor = num(body.get("monitor_seconds") or 0, "monitor_seconds")
        if monitor > 0:
            result["monitor"] = self._monitor_after(dev["busid"], body.get("tty"), body.get("baud"), monitor, starts)
        return result

    def _monitor_after(self, busid, tty, baud, seconds, starts=None):
        """After flashing: open the serial port (it can take time to come back) and read for some seconds."""
        deadline = time.time() + min(120.0, seconds) + 5
        last = None
        while time.time() < deadline:
            try:
                path = self.serial_path({"busid": busid, "tty": tty})
                s = self.hub.open(path, baud)
                start = (starts or {}).get(path, s.end)  # only what the new firmware prints
                time.sleep(min(120.0, seconds))
                r = s.read(start, 256 * 1024, 0)
                return {"tty": os.path.basename(path), "baud": s.baud, "offset": r["offset"],
                        "text": r["data"].decode("utf-8", "replace")}
            except ApiError as e:
                last = e.message
                time.sleep(0.3)
        return {"error": last or "no serial port after flashing"}



    def identify(self, body):
        """Reset the MCU behind a USB device and capture its serial log (see identify_capture)."""
        busid = body.get("busid")
        dev = self.usb.get(busid)
        if dev["bound"]:
            raise ApiError(409, "%s is shared over USB/IP, so its serial port is not on this PC. Stop sharing it, "
                                "or identify it on the PC that attached it." % busid)
        ttys = dev["ttys"]
        if body.get("tty"):
            ttys = [t for t in ttys if t == os.path.basename(str(body["tty"]))]
            if not ttys:
                raise ApiError(404, "%s has no port %s (ports: %s)" % (busid, body["tty"], ", ".join(dev["ttys"])))
        paths = [os.path.join(self.cfg["devfs_root"], t) for t in ttys]
        user = body.get("user") or None

        def run_script(script, env, timeout):
            env = dict(env, SM_SETUP=self.cfg["name"])
            return run_command(self.cfg, script, timeout, None, user, extra_env=env)

        def hw_reset(method):
            if method == "usb":
                return self.usb.reset(busid)["message"]
            rc, detail = run_probe(dev, "reset")
            return detail

        lock = self.identify_locks.setdefault(busid, threading.Lock())
        if not lock.acquire(False):
            raise ApiError(409, "an identification of %s is already running" % busid)
        try:
            result = identify_capture(dev, paths, body.get("baud", "auto"), body.get("duration", 4),
                                      body.get("reset", "auto"), body.get("script") or "",
                                      body.get("after_script") or "", run_script, hw_reset,
                                      num(body.get("script_timeout") or 30, "script_timeout"),
                                      capture_factory=lambda ps, b: HubCapture(self.hub, ps, b))
            rc, info = run_probe(dev, "info")
            result["probe"] = info
        finally:
            lock.release()
        result["device"] = self.usb.get(busid) if os.path.isdir(os.path.join(self.usb.base, busid)) else dev
        log.info("identify %s: reset=%s baud=%s, %d bytes", busid, result["reset"], result["baud"],
                 sum(c["bytes"] for c in result["captures"]))
        return result

    def public_config(self):
        cfg = dict(self.cfg)
        cfg.pop("token", None)
        return cfg

    def update_config(self, body):
        allowed = {"name": str, "auto_bind": list, "protected": list, "nicknames": dict}
        changes = {}
        for key, typ in allowed.items():
            if key in body:
                if not isinstance(body[key], typ):
                    raise ApiError(400, "%s must be a %s" % (key, typ.__name__))
                if typ is list:
                    changes[key] = [str(x).strip() for x in body[key] if str(x).strip()]
                elif typ is dict:
                    changes[key] = dict((str(k).strip().lower(), str(v).strip()[:60])
                                        for k, v in body[key].items() if str(v).strip())
                else:
                    changes[key] = body[key].strip() or socket.gethostname()
        with self.cfg_lock:
            self.cfg.update(changes)
            if self.config_path:
                try:
                    on_disk = {}
                    if os.path.exists(self.config_path):
                        with open(self.config_path) as f:
                            on_disk = json.load(f)
                    on_disk.update(changes)
                    tmp = self.config_path + ".tmp"
                    with open(tmp, "w") as f:
                        json.dump(on_disk, f, indent=2)
                    os.chmod(tmp, 0o600)
                    os.replace(tmp, self.config_path)
                except (OSError, ValueError) as e:
                    log.warning("could not save config: %s", e)
        log.info("config updated: %s", ", ".join(sorted(changes)))
        return self.public_config()

    # -- background loops ------------------------------------------------------
    def autobind_loop(self):
        retry_after = {}
        while True:
            time.sleep(max(1, float(self.cfg.get("auto_bind_interval") or 3)))
            rules = self.cfg.get("auto_bind") or []
            if not rules:
                continue
            try:
                for dev in self.usb.list():
                    if dev["bound"] or dev["protected"] or not dev["auto_bind"]:
                        continue
                    if retry_after.get(dev["busid"], 0) > time.time():
                        continue
                    try:
                        self.usb.bind(dev["busid"])
                        log.info("auto-bound %s (%s)", dev["busid"], dev["name"])
                    except ApiError as e:
                        retry_after[dev["busid"]] = time.time() + 30
                        log.warning("auto-bind of %s failed: %s", dev["busid"], e.message)
            except Exception:  # pragma: no cover
                log.exception("auto-bind loop error")

    def discovery_loop(self):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("", int(self.cfg["discovery_port"])))
        except OSError as e:
            log.warning("discovery disabled: %s", e)
            return
        log.info("discovery listening on udp/%s", self.cfg["discovery_port"])
        while True:
            try:
                data, addr = s.recvfrom(2048)
                if data.strip() != DISCOVERY_MAGIC:
                    continue
                reply = dict(self.ping())
                reply.update({"port": int(self.cfg["port"])})
                s.sendto(json.dumps(reply).encode(), addr)
            except OSError as e:  # pragma: no cover
                log.warning("discovery error: %s", e)
                time.sleep(1)


# The actions that need the lease while a device is reserved.
LEASE_ROUTES = {("POST", "/api/usb/bind"), ("POST", "/api/usb/unbind"), ("POST", "/api/usb/reset"),
                ("POST", "/api/usb/identify"), ("POST", "/api/usb/flash"), ("POST", "/api/serial/write"),
                ("POST", "/api/serial/reset")}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "setup-agent/" + VERSION
    body_read = 0  # bytes of the request body that _upload consumed (for _drain)

    @property
    def agent(self):
        return self.server.agent

    def real_ip(self):
        """The client address (behind the TLS front end, the address of the real client)."""
        if self.client_address[0] in ("127.0.0.1", "::1"):
            real = REAL_ADDR.get(self.client_address[1])
            if real:
                return real[0]
        return self.client_address[0]

    def log_message(self, fmt, *args):
        log.debug("%s %s", self.address_string(), fmt % args)

    def _send_json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self, qs):
        cfg = self.agent.cfg
        if cfg.get("insecure_no_auth"):
            return True
        token = cfg.get("token") or ""
        if not token:
            return False
        header = self.headers.get("Authorization", "")
        given = header[7:].strip() if header.startswith("Bearer ") else (qs.get("token") or [""])[0]
        return hmac.compare_digest(given.encode(), token.encode())

    def _body(self):
        ln = int(self.headers.get("Content-Length") or 0)
        if ln > MAX_BODY:
            raise ApiError(413, "request body too large")
        raw = self.rfile.read(ln) if ln else b""
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode())
        except ValueError:
            raise ApiError(400, "body must be JSON")
        if not isinstance(data, dict):
            raise ApiError(400, "body must be a JSON object")
        return data

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PUT(self):
        self._dispatch("PUT")

    def _upload(self, qs):
        """PUT /api/files?path=...: stream the request body to a file."""
        cfg = self.agent.cfg
        ln = num(self.headers.get("Content-Length") or -1, "Content-Length", int)
        if ln < 0:
            raise ApiError(411, "Content-Length is required")
        if ln > int(cfg.get("max_upload") or 0):
            raise ApiError(413, "file too large (max_upload is %d bytes)" % cfg.get("max_upload"))
        try:
            path = safe_path(cfg, (qs.get("path") or [""])[0], create_dirs=True)
            fd, tmp = tempfile.mkstemp(prefix=os.path.basename(path) + ".", suffix=".part",
                                       dir=os.path.dirname(path))
        except OSError as e:
            raise ApiError(403 if e.errno in (errno.EACCES, errno.EPERM) else 500,
                           "cannot write %s: %s" % ((qs.get("path") or [""])[0], e.strerror or e))
        digest = hashlib.sha256()
        left = ln
        try:
            with os.fdopen(fd, "wb") as f:
                while left > 0:
                    chunk = self.rfile.read(min(1024 * 1024, left))
                    if not chunk:
                        raise ApiError(400, "upload cut off")
                    self.body_read += len(chunk)
                    f.write(chunk)
                    digest.update(chunk)
                    left -= len(chunk)
            os.chmod(tmp, 0o644)
            chown_to(cfg.get("run_as"), tmp)
            os.replace(tmp, path)
        except OSError as e:
            raise ApiError(403 if e.errno in (errno.EACCES, errno.EPERM) else 500,
                           "cannot write %s: %s" % (path, e.strerror or e))
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        log.info("upload %s (%d bytes)", path, ln)
        return {"path": path, "size": ln, "sha256": digest.hexdigest()}

    def _drain(self, limit=64 * 1024 * 1024):
        """Read and drop (the rest of) a request body that we refuse, then close the connection."""
        self.close_connection = True
        try:
            left = int(self.headers.get("Content-Length") or 0) - self.body_read
        except ValueError:
            return
        if left > limit:
            return
        while left > 0:
            chunk = self.rfile.read(min(1024 * 1024, left))
            if not chunk:
                break
            left -= len(chunk)

    def _download(self, qs):
        path = safe_path(self.agent.cfg, (qs.get("path") or [""])[0])
        if not os.path.isfile(path):
            raise ApiError(404, "no file %s" % path)
        size = os.path.getsize(path)
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(size))
        self.end_headers()
        with open(path, "rb") as f:
            shutil.copyfileobj(f, self.wfile, 1024 * 1024)

    def _dispatch(self, method):
        url = urlparse(self.path)
        path = url.path.rstrip("/") or "/"
        qs = parse_qs(url.query)
        try:
            if path == "/api/ping":
                if method == "POST":
                    self._body()
                return self._send_json(200, self.agent.ping())
            if not self._authorized(qs):
                self.close_connection = True  # do not read a body we will not use
                return self._send_json(401, {"error": "unauthorized"})
            if path == "/api/files" and method == "PUT":
                self.body_read = 0
                try:
                    return self._send_json(200, self._upload(qs))
                except Exception:
                    # Read the body first, so the client gets the answer (not a connection reset).
                    self._drain(int(self.agent.cfg.get("max_upload") or 0))
                    raise
            if path == "/api/files" and method == "GET":
                return self._download(qs)
            body = self._body() if method == "POST" else {}
            if path == "/api/shell" and method == "GET":
                return self._shell(qs)
            a = self.agent
            routes = {
                ("GET", "/api/status"): lambda: a.status(),
                ("GET", "/api/usb"): lambda: a.usb_list(qs),
                ("POST", "/api/usb/bind"): lambda: a.usb.bind(body.get("busid"), bool(body.get("force"))),
                ("POST", "/api/usb/unbind"): lambda: a.usb.unbind(body.get("busid")),
                ("POST", "/api/usb/reset"): lambda: a.usb.reset(body.get("busid")),
                ("POST", "/api/usb/identify"): lambda: a.identify(body),
                ("POST", "/api/usb/flash"): lambda: a.flash(body),
                ("POST", "/api/usb/claim"): lambda: a.claim(body, self.real_ip()),
                ("POST", "/api/usb/release"): lambda: a.release(body),
                ("POST", "/api/usb/nickname"): lambda: a.set_nickname(body),
                ("GET", "/api/serial"): lambda: {"sessions": a.hub.list()},
                ("POST", "/api/serial/open"): lambda: a.serial_open(body),
                ("GET", "/api/serial/read"): lambda: a.serial_read(qs),
                ("POST", "/api/serial/write"): lambda: a.serial_write(body),
                ("POST", "/api/serial/reset"): lambda: a.serial_reset(body),
                ("POST", "/api/serial/close"): lambda: a.hub.close(a.serial_path(body)),
                ("GET", "/api/files/list"): lambda: a.files_list(qs),
                ("POST", "/api/files/delete"): lambda: a.files_delete(body),
                ("POST", "/api/reserve"): lambda: a.reserve(body),
                ("POST", "/api/release"): lambda: a.release_reservation(body),
                ("GET", "/api/reservations"): lambda: a.list_reservations(),
                ("GET", "/api/identify/methods"): lambda: {"reset": RESET_METHODS, "bauds": AUTO_BAUDS},
                ("POST", "/api/usbipd/start"): lambda: {"notes": a.usb.ensure_usbipd(), **a.usb.usbip_info()},
                ("POST", "/api/exec"): lambda: run_command(a.cfg, body.get("command"), body.get("timeout", 60),
                                                           body.get("cwd"), body.get("user")),
                ("GET", "/api/log"): lambda: {"lines": list(LOG_RING)[-num((qs.get("n") or ["200"])[0], "n", int):]},
                ("GET", "/api/config"): lambda: a.public_config(),
                ("POST", "/api/config"): lambda: a.update_config(body),
            }
            if a.reservations and (method, path) in LEASE_ROUTES:
                busid = body.get("busid") or (a.busid_for_tty(body.get("tty")) if body.get("tty") else None)
                leases = [x.strip() for x in self.headers.get("X-SM-Lease", "").split(",") if x.strip()]
                if body.get("lease"):
                    leases.append(str(body["lease"]))
                a.check_lease(busid, leases)
            fn = routes.get((method, path))
            if fn is None:
                return self._send_json(404, {"error": "not found: %s %s" % (method, path)})
            return self._send_json(200, fn())
        except ApiError as e:
            return self._send_json(e.code, {"error": e.message})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            log.exception("error in %s %s", method, path)
            try:
                return self._send_json(500, {"error": "%s: %s" % (type(e).__name__, e)})
            except OSError:
                pass

    def _shell(self, qs):
        if self.headers.get("Upgrade", "").lower() != "websocket":
            raise ApiError(400, "WebSocket upgrade required")
        key = self.headers.get("Sec-WebSocket-Key")
        if not key:
            raise ApiError(400, "missing Sec-WebSocket-Key")
        user = (qs.get("user") or [None])[0]
        shell_argv(self.agent.cfg, user or self.agent.cfg.get("run_as") or current_user())  # validate first
        accept = base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
        self.send_response(101, "Switching Protocols")
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)
        self.end_headers()
        self.wfile.flush()
        self.close_connection = True
        ws = WebSocket(self.rfile, self.connection)

        def num(name, default):
            try:
                return max(1, min(1000, int((qs.get(name) or [default])[0])))
            except ValueError:
                return default

        shell_session(ws, self.agent.cfg, num("cols", 80), num("rows", 24), user)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, agent):
        if ":" in addr[0]:
            self.address_family = socket.AF_INET6
        super().__init__(addr, Handler)
        self.agent = agent


# --------------------------------------------------------------------------- main

def load_config(path):
    cfg = dict(DEFAULTS)
    if path and os.path.exists(path):
        with open(path) as f:
            cfg.update(json.load(f))
    if os.environ.get("SETUP_AGENT_TOKEN"):
        cfg["token"] = os.environ["SETUP_AGENT_TOKEN"]
    if not cfg.get("name"):
        cfg["name"] = socket.gethostname()
    return cfg


def main(argv=None):
    ap = argparse.ArgumentParser(description="Setup PC agent for Setup Manager")
    ap.add_argument("--config", default=os.environ.get("SETUP_AGENT_CONFIG", DEFAULT_CONFIG_PATH))
    ap.add_argument("--listen", help="address to listen on (default 0.0.0.0)")
    ap.add_argument("--port", type=int, help="TCP port (default 8765)")
    ap.add_argument("--token", help="API token (prefer the config file or SETUP_AGENT_TOKEN)")
    ap.add_argument("--name", help="display name of this setup")
    ap.add_argument("--insecure-no-auth", action="store_true", help="accept requests without a token (testing only)")
    ap.add_argument("--gen-token", action="store_true", help="print a new random token and exit")
    ap.add_argument("--log-level", default="INFO")
    ap.add_argument("--version", action="version", version=VERSION)
    args = ap.parse_args(argv)

    if args.gen_token:
        print(secrets.token_urlsafe(32))
        return 0

    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    stream = logging.StreamHandler()
    stream.setFormatter(fmt)
    ring = RingHandler()
    ring.setFormatter(fmt)
    log.addHandler(stream)
    log.addHandler(ring)
    log.setLevel(args.log_level.upper())

    cfg = load_config(args.config)
    for key in ("listen", "port", "token", "name"):
        if getattr(args, key):
            cfg[key] = getattr(args, key)
    if args.insecure_no_auth:
        cfg["insecure_no_auth"] = True
    if not cfg.get("token") and not cfg.get("insecure_no_auth"):
        log.error("no token configured. Set \"token\" in %s (generate one with --gen-token).", args.config)
        return 2
    if os.geteuid() != 0:
        log.warning("not running as root: USB bind/unbind and user switching will not work")

    agent = Agent(cfg, args.config if os.path.exists(args.config) else None)
    use_tls = str(cfg.get("tls", "auto")).lower() not in ("false", "0", "no", "off")
    front = None
    if use_tls:
        try:
            cert, key = ensure_cert(cfg, args.config)
        except (ApiError, OSError) as e:
            log.error("cannot start TLS: %s", getattr(e, "message", e))
            return 3
        server = Server(("127.0.0.1", 0), agent)  # only the TLS front end can reach it
        front = TlsFront(agent, cfg["listen"], cfg["port"], cert, key, server.server_address[1])
        agent.tls_fingerprint = cert_fingerprint(cert)
        threading.Thread(target=front.serve, name="tls-front", daemon=True).start()
    else:
        server = Server((cfg["listen"], int(cfg["port"])), agent)
        log.warning("TLS is off: the API and the shell are not encrypted")
    if os.geteuid() == 0:
        block = bool(use_tls) and cfg.get("usbip_network", "tunnel") == "tunnel"
        for note in usbip_firewall(block, int(cfg.get("usbip_port") or 3240)):
            log.info("USB/IP port: %s", note)

    if os.geteuid() == 0:
        threading.Thread(target=agent.usb.ensure_usbipd, name="usbipd-init", daemon=True).start()
    threading.Thread(target=agent.autobind_loop, name="auto-bind", daemon=True).start()
    if cfg.get("discovery"):
        threading.Thread(target=agent.discovery_loop, name="discovery", daemon=True).start()

    def stop(signum, frame):
        log.info("stopping (signal %d)", signum)
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    log.info("setup-agent %s '%s' listening on %s:%s%s", VERSION, cfg["name"], cfg["listen"], cfg["port"],
             " (TLS, sha256 %s)" % agent.tls_fingerprint if front else " (NOT encrypted)")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
