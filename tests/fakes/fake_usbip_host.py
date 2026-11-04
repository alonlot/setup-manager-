#!/usr/bin/env python3
"""Fake `usbip` for the setup PC side: bind/unbind change the fake sysfs."""
import os
import sys

root = os.environ["FAKE_SYSFS"]
devs = os.path.join(root, "bus", "usb", "devices")
drivers = os.path.join(root, "bus", "usb", "drivers")
args = sys.argv[1:]
if args == ["version"]:
    print("usbip (usbip-utils 2.0)")
    sys.exit(0)
if len(args) == 3 and args[0] in ("bind", "unbind") and args[1] == "-b":
    busid = args[2]
    d = os.path.join(devs, busid)
    if not os.path.isdir(d):
        print("usbip: error: device with the specified bus ID does not exist")
        sys.exit(1)
    link = os.path.join(d, "driver")
    if os.path.lexists(link):
        os.remove(link)
    status = os.path.join(d, "usbip_status")
    if args[0] == "bind":
        os.symlink(os.path.join(drivers, "usbip-host"), link)
        with open(status, "w") as f:
            f.write("1\n")
        print("usbip: info: bind device on busid %s: complete" % busid)
    else:
        os.symlink(os.path.join(drivers, "usb"), link)
        if os.path.exists(status):
            os.remove(status)
        print("usbip: info: unbind device on busid %s: complete" % busid)
    sys.exit(0)
print("usbip: unsupported in fake: %s" % args)
sys.exit(2)
