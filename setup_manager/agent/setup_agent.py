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
import fcntl
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
import termios
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

VERSION = "0.1.0"
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
    "tls_cert": "",
    "tls_key": "",
    # User for shells and commands. Empty = the user the agent runs as (root).
    "run_as": "",
    "shell": "/bin/bash",
    "usbip_cmd": "usbip",
    "usbipd_cmd": "usbipd",
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

    # -- usbip / usbipd ------------------------------------------------------
    def usbip(self, *args, timeout=20):
        cmd = split_cmd(self.cfg["usbip_cmd"]) + list(args)
        try:
            p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)
        except FileNotFoundError:
            raise ApiError(500, "usbip is not installed on this PC (command: %s)" % cmd[0])
        except subprocess.TimeoutExpired:
            raise ApiError(504, "usbip %s timed out" % " ".join(args))
        out = p.stdout.decode(errors="replace").strip()
        return p.returncode, out

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
                try:
                    subprocess.run(split_cmd(self.cfg["usbipd_cmd"]) + ["-D"], timeout=10,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
                    notes.append("started usbipd -D")
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
        return {
            "usbip_installed": shutil.which(split_cmd(self.cfg["usbip_cmd"])[0]) is not None,
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
        tool = "runuser" if shutil.which("runuser") else "su"
        argv = [tool, "-l", user]
        if command is not None:
            argv += ["-c", command]
        return argv, user_home(user)
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


def run_command(cfg, command, timeout=60, cwd=None, user=None, extra_env=None):
    if not isinstance(command, str) or not command.strip():
        raise ApiError(400, "command is required")
    try:
        timeout = float(timeout or 60)
    except (TypeError, ValueError):
        raise ApiError(400, "timeout must be a number")
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
        out, err = p.communicate()
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
                     run_script=None, hw_reset=None, script_timeout=30, extra_env=None, max_attempts=4):
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
        cap = SerialCapture(paths, b).start()
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

    # -- API handlers ----------------------------------------------------------
    def ping(self):
        return {"service": "setup-agent", "name": self.cfg["name"], "hostname": socket.gethostname(),
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
            "time": time.time(),
        }

    def usb_list(self, qs):
        include_hubs = qs.get("all", ["0"])[0] in ("1", "true", "yes")
        return {"devices": self.usb.list(include_hubs=include_hubs), "usbip": self.usb.usbip_info()}

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
                                      float(body.get("script_timeout") or 30))
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
        allowed = {"name": str, "auto_bind": list, "protected": list}
        changes = {}
        for key, typ in allowed.items():
            if key in body:
                if not isinstance(body[key], typ):
                    raise ApiError(400, "%s must be a %s" % (key, typ.__name__))
                if typ is list:
                    changes[key] = [str(x).strip() for x in body[key] if str(x).strip()]
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
                reply.update({"port": int(self.cfg["port"]), "tls": bool(self.cfg.get("tls_cert"))})
                s.sendto(json.dumps(reply).encode(), addr)
            except OSError as e:  # pragma: no cover
                log.warning("discovery error: %s", e)
                time.sleep(1)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "setup-agent/" + VERSION

    @property
    def agent(self):
        return self.server.agent

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

    def _dispatch(self, method):
        url = urlparse(self.path)
        path = url.path.rstrip("/") or "/"
        qs = parse_qs(url.query)
        try:
            body = self._body() if method == "POST" else {}
            if path == "/api/ping":
                return self._send_json(200, self.agent.ping())
            if not self._authorized(qs):
                return self._send_json(401, {"error": "unauthorized"})
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
                ("GET", "/api/identify/methods"): lambda: {"reset": RESET_METHODS, "bauds": AUTO_BAUDS},
                ("POST", "/api/usbipd/start"): lambda: {"notes": a.usb.ensure_usbipd(), **a.usb.usbip_info()},
                ("POST", "/api/exec"): lambda: run_command(a.cfg, body.get("command"), body.get("timeout", 60),
                                                           body.get("cwd"), body.get("user")),
                ("GET", "/api/log"): lambda: {"lines": list(LOG_RING)[-int((qs.get("n") or ["200"])[0]):]},
                ("GET", "/api/config"): lambda: a.public_config(),
                ("POST", "/api/config"): lambda: a.update_config(body),
            }
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
    server = Server((cfg["listen"], int(cfg["port"])), agent)
    if cfg.get("tls_cert") and cfg.get("tls_key"):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cfg["tls_cert"], cfg["tls_key"])
        server.socket = ctx.wrap_socket(server.socket, server_side=True)

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
             " (TLS)" if cfg.get("tls_cert") else "")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
