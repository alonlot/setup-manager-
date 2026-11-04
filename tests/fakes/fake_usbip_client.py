#!/usr/bin/env python3
"""Fake `usbip` for the manager PC side: attach/detach/port with a JSON state file.

Attach also marks the device "in use" (usbip_status=2) in the fake sysfs of the setup.
"""
import json
import os
import sys

state_file = os.environ["FAKE_USBIP_STATE"]
root = os.environ["FAKE_SYSFS"]


def load():
    if os.path.exists(state_file):
        with open(state_file) as f:
            return json.load(f)
    return []


def save(ports):
    with open(state_file, "w") as f:
        json.dump(ports, f)


def set_status(busid, value):
    p = os.path.join(root, "bus", "usb", "devices", busid, "usbip_status")
    if os.path.exists(p):
        with open(p, "w") as f:
            f.write(value + "\n")


args = sys.argv[1:]
tcp_port = "3240"
if args[:1] == ["--tcp-port"]:
    tcp_port, args = args[1], args[2:]
ports = load()
if args and args[0] == "attach":
    host = args[args.index("-r") + 1]
    busid = args[args.index("-b") + 1]
    st = os.path.join(root, "bus", "usb", "devices", busid, "usbip_status")
    if not os.path.exists(st) or open(st).read().strip() != "1":
        print("usbip: error: Attach Request for %s failed - Device busy (exported)" % busid)
        sys.exit(1)
    if tcp_port != "3240":
        # An encrypted tunnel (or a direct usbipd): do the import request like the real usbip.
        import socket
        import struct
        try:
            c = socket.create_connection((host, int(tcp_port)), timeout=10)
            c.sendall(struct.pack("!HHI32s", 0x0111, 0x8003, 0, busid.encode()))
            reply = c.recv(64)
        except OSError as e:
            print("usbip: error: tcp connect: %s" % e)
            sys.exit(1)
        if not reply.startswith(b"\x01\x11\x00\x03"):
            print("usbip: error: query: import reply failed")
            sys.exit(1)
        c.close()
    used = {p["port"] for p in ports}
    port = next(i for i in range(16) if i not in used)
    vid = open(os.path.join(root, "bus", "usb", "devices", busid, "idVendor")).read().strip()
    pid = open(os.path.join(root, "bus", "usb", "devices", busid, "idProduct")).read().strip()
    ports.append({"port": port, "host": host, "busid": busid, "tcp_port": tcp_port, "vid": vid, "pid": pid})
    save(ports)
    set_status(busid, "2")
    sys.exit(0)
if args and args[0] == "detach":
    port = int(args[args.index("-p") + 1])
    keep = [p for p in ports if p["port"] != port]
    if len(keep) == len(ports):
        print("usbip: error: invalid port %d" % port)
        sys.exit(1)
    for p in ports:
        if p["port"] == port:
            set_status(p["busid"], "1")
    save(keep)
    print("usbip: info: Port %d is now detached!" % port)
    sys.exit(0)
if args and args[0] == "port":
    print("Imported USB devices")
    print("====================")
    for p in ports:
        print("Port %02d: <Port in Use> at Full Speed(12Mbps)" % p["port"])
        print("       unknown vendor : unknown product (%s:%s)" % (p["vid"], p["pid"]))
        print("       3-%d -> usbip://%s:%s/%s" % (p["port"] + 1, p["host"], p["tcp_port"], p["busid"]))
        print("           -> remote bus/dev 001/004")
    sys.exit(0)
print("unsupported: %s" % args)
sys.exit(2)
