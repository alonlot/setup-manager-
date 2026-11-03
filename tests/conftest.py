import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
AGENT = ROOT / "setup_manager" / "agent" / "setup_agent.py"
FAKES = Path(__file__).resolve().parent / "fakes"
TOKEN = "test-token-123"

DEVICES = {
    # busid: (vid, pid, manufacturer, product, serial, class, interfaces)
    "1-1": ("0483", "374b", "STMicroelectronics", "STM32 STLink", "066DFF485457725187", "ef",
            [("1.0", "ff", None, []), ("1.1", "08", "usb-storage", []), ("1.2", "02", "cdc_acm", ["ttyACM0"])]),
    "1-2": ("0403", "6001", "FTDI", "FT232R USB UART", "A50285BI", "00", [("1.0", "ff", "ftdi_sio", ["ttyUSB0"])]),
    "1-3": ("0bda", "8153", "Realtek", "USB 10/100/1000 LAN", "", "00", [("1.0", "ff", "r8152", [])]),
    "1-4": ("05e3", "0610", "GenesysLogic", "USB2.1 Hub", "", "09", [("1.0", "09", "hub", [])]),
    "2-1.3": ("1366", "1015", "SEGGER", "J-Link", "000123456789", "00", [("1.0", "02", "cdc_acm", ["ttyACM1"])]),
}


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text + "\n")


def build_sysfs(root: Path):
    devs = root / "bus" / "usb" / "devices"
    drivers = root / "bus" / "usb" / "drivers"
    for d in ("usb", "usbip-host", "cdc_acm", "ftdi_sio", "usb-storage", "r8152", "hub"):
        (drivers / d).mkdir(parents=True, exist_ok=True)
    (root / "module" / "usbip_host").mkdir(parents=True, exist_ok=True)
    (devs / "usb1").mkdir(parents=True, exist_ok=True)  # root hub: must be skipped
    write(devs / "usb1" / "idVendor", "1d6b")
    for i, (busid, (vid, pid, man, prod, serial, cls, ifaces)) in enumerate(DEVICES.items()):
        d = devs / busid
        d.mkdir(parents=True)
        write(d / "idVendor", vid)
        write(d / "idProduct", pid)
        write(d / "manufacturer", man)
        write(d / "product", prod)
        if serial:
            write(d / "serial", serial)
        write(d / "bDeviceClass", cls)
        write(d / "busnum", busid.split("-")[0])
        write(d / "devnum", str(i + 2))
        write(d / "speed", "12")
        write(d / "version", " 2.00")
        os.symlink(drivers / "usb", d / "driver")
        for suffix, icls, drv, ttys in ifaces:
            it = devs / f"{busid}:{suffix}"
            it.mkdir()
            write(it / "bInterfaceClass", icls)
            if drv:
                os.symlink(drivers / drv, it / "driver")
            for t in ttys:
                if t.startswith("ttyUSB"):
                    (it / t).mkdir()
                else:
                    (it / "tty" / t).mkdir(parents=True)
        if busid == "1-3":
            (devs / f"{busid}:1.0" / "net" / "enx001122").mkdir(parents=True)
    return root


def free_port(kind=socket.SOCK_STREAM):
    s = socket.socket(socket.AF_INET, kind)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def sysfs(tmp_path):
    return build_sysfs(tmp_path / "sys")


@pytest.fixture
def agent(tmp_path, sysfs, monkeypatch):
    port = free_port()
    dport = free_port(socket.SOCK_DGRAM)
    cfg = {
        "name": "bench-test",
        "listen": "127.0.0.1",
        "port": port,
        "token": TOKEN,
        "sysfs_root": str(sysfs),
        "usbip_cmd": [sys.executable, str(FAKES / "fake_usbip_host.py")],
        "manage_usbipd": False,
        "discovery_port": dport,
        "protected": ["2-1.3"],
        "auto_bind_interval": 1,
        "devfs_root": str(tmp_path / "dev"),
    }
    (tmp_path / "dev").mkdir(exist_ok=True)
    cfg_path = tmp_path / "agent.json"
    cfg_path.write_text(json.dumps(cfg))
    env = dict(os.environ, FAKE_SYSFS=str(sysfs))
    proc = subprocess.Popen([sys.executable, str(AGENT), "--config", str(cfg_path), "--log-level", "DEBUG"],
                            env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.05)
    else:
        proc.kill()
        raise RuntimeError(proc.stdout.read().decode())
    yield {"port": port, "discovery_port": dport, "sysfs": sysfs, "config": cfg_path, "proc": proc,
           "devfs": tmp_path / "dev"}
    proc.terminate()
    try:
        proc.wait(5)
    except subprocess.TimeoutExpired:
        proc.kill()


@pytest.fixture
def manager_config(tmp_path, agent, monkeypatch):
    state = tmp_path / "usbip-state.json"
    monkeypatch.setenv("FAKE_SYSFS", str(agent["sysfs"]))
    monkeypatch.setenv("FAKE_USBIP_STATE", str(state))
    cfg = {
        "local_usbip": {"command": [sys.executable, str(FAKES / "fake_usbip_client.py")], "sudo": False},
        "discovery_port": agent["discovery_port"],
        "discovery_targets": ["127.0.0.1"],
        "setups": [{"id": "bench", "name": "Bench", "host": "127.0.0.1", "port": agent["port"], "token": TOKEN}],
    }
    path = tmp_path / "manager.json"
    path.write_text(json.dumps(cfg))
    return path


class FakeMcu:
    """A fake board behind a pseudo-terminal.

    On "RESET\\n" it prints a boot log. `baud`: it prints readable text only when the
    port is set to this rate (else garbage), like a real UART at the wrong rate.
    `replies` maps a received command to an answer.
    """

    def __init__(self, devfs: Path, name: str, boot: str, baud: int | None = None, replies=None):
        import pty
        import threading
        self.master, slave = pty.openpty()
        self.slave_path = os.ttyname(slave)
        os.close(slave)
        self.link = devfs / name
        if self.link.exists() or self.link.is_symlink():
            self.link.unlink()
        self.link.symlink_to(self.slave_path)
        self.boot, self.baud, self.replies = boot, baud, replies or {}
        self.received = b""
        self.stop = False
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def _speed(self):
        import fcntl
        import struct
        buf = bytearray(44)
        fcntl.ioctl(self.master, 0x802C542A, buf)  # TCGETS2
        return struct.unpack("IIIIB19sII", bytes(buf))[7]

    def _say(self, text: str):
        if self.baud and self._speed() != self.baud:
            os.write(self.master, bytes((i * 37 + 129) % 256 for i in range(len(text))))
        else:
            os.write(self.master, text.encode())

    def _loop(self):
        import select
        while not self.stop:
            r, _, _ = select.select([self.master], [], [], 0.1)
            if not r:
                continue
            try:
                self.received += os.read(self.master, 4096)
            except OSError:  # EIO while no process has the slave side open
                time.sleep(0.05)
                continue
            while b"\n" in self.received or b"\r" in self.received:
                for sep in (b"\n", b"\r"):
                    if sep in self.received:
                        line, _, self.received = self.received.partition(sep)
                        break
                cmd = line.decode(errors="replace").strip()
                if cmd == "RESET":
                    self._say(self.boot)
                elif cmd in self.replies:
                    self._say(self.replies[cmd])

    def close(self):
        self.stop = True
        self.thread.join(1)
        os.close(self.master)


@pytest.fixture
def fake_mcus():
    made = []

    def make(*args, **kw):
        m = FakeMcu(*args, **kw)
        made.append(m)
        return m
    yield make
    for m in made:
        m.close()
