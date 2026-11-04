#!/usr/bin/env python3
"""Fake ssh for the rollout tests: runs the command on this PC. "install.sh" restarts a test agent.

FAKE_AGENTS = {"host": {"config": ..., "pidfile": ..., "sysfs": ...}}  the agents of the "remote" PCs
FAKE_SUDO_PASSWORD   when set, sudo needs this password (sudo -S)
FAKE_INSTALL_FAIL    when set, install.sh fails
"""
import hashlib
import json
import os
import re
import shlex
import signal
import socket
import ssl
import subprocess
import sys
import time

args = sys.argv[1:]
i = 0
while i < len(args) and args[i].startswith("-"):
    i += 2 if args[i] in ("-o", "-p", "-i", "-l") else 1
target, cmd = args[i], " ".join(args[i + 1:])
host = target.split("@")[-1]
with open(os.environ["FAKE_SSH_LOG"], "a") as f:
    f.write(f"{target} {cmd}\n")
agents = json.loads(os.environ["FAKE_AGENTS"])
if host not in agents:
    print(f"ssh: connect to host {host} port 22: Connection refused")
    sys.exit(255)
pw = os.environ.get("FAKE_SUDO_PASSWORD")

if cmd == "sudo -n true":
    if pw:
        print("sudo: a password is required")
        sys.exit(1)
    sys.exit(0)

m = re.match(r"^sudo (-n|-S -p '') bash (\S+)(.*)$", cmd)
if m:
    if m.group(1) != "-n" and sys.stdin.readline().rstrip("\n") != pw:
        print("Sorry, try again.\nsudo: 1 incorrect password attempt")
        sys.exit(1)
    if os.environ.get("FAKE_INSTALL_FAIL"):
        print("E: Unable to locate package usbip")
        sys.exit(100)
    a = agents[host]
    argv = shlex.split(m.group(3))
    with open(a["config"]) as fp:
        cfg = json.load(fp)
    if "--token" in argv:
        cfg["token"] = argv[argv.index("--token") + 1]
    with open(a["config"], "w") as fp:
        json.dump(cfg, fp)
    try:  # restart the agent, like systemctl restart setup-agent
        with open(a["pidfile"]) as fp:
            old = int(fp.read())
        os.kill(old, signal.SIGTERM)
        for _ in range(50):
            os.kill(old, 0)
            time.sleep(0.1)
    except (OSError, ValueError):
        pass
    p = subprocess.Popen([sys.executable, a["script"], "--config", a["config"]], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True,
                         env=dict(os.environ, FAKE_SYSFS=a["sysfs"]))
    with open(a["pidfile"], "w") as fp:
        fp.write(str(p.pid))
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", cfg["port"]), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.05)
    cert = os.path.join(os.path.dirname(a["config"]), "cert.pem")
    print(">> installing agent to /opt/setup-agent")
    print("CONFIG_TOKEN=" + cfg["token"])
    with open(cert) as fp:
        print("CONFIG_FINGERPRINT=" + hashlib.sha256(ssl.PEM_cert_to_DER_cert(fp.read())).hexdigest())
    sys.exit(0)

sys.exit(subprocess.run(["bash", "-c", cmd]).returncode)
