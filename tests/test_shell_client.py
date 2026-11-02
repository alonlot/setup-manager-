"""`setup-manager shell <setup>` in a real pseudo-terminal, against the agent."""
import fcntl
import os
import select
import struct
import subprocess
import sys
import termios
import time

from conftest import ROOT


def _read_until(fd, marker, timeout=15):
    buf = b""
    end = time.time() + timeout
    while marker not in buf and time.time() < end:
        r, _, _ = select.select([fd], [], [], 0.2)
        if r:
            try:
                chunk = os.read(fd, 65536)
            except OSError:
                break
            if not chunk:
                break
            buf += chunk
    return buf


def test_shell_client(manager_config):
    master, slave = os.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 33, 111, 0, 0))
    env = dict(os.environ, PYTHONPATH=str(ROOT))
    p = subprocess.Popen([sys.executable, "-m", "setup_manager", "--config", str(manager_config), "shell", "bench"],
                         stdin=slave, stdout=slave, stderr=slave, env=env, start_new_session=True)
    os.close(slave)
    try:
        assert b"connecting to Bench" in _read_until(master, b"connecting")
        _read_until(master, b"# ")  # wait for the remote prompt
        os.write(master, b"stty size; echo MARK$((40+2))\n")
        out = _read_until(master, b"MARK42")
        assert b"MARK42" in out and b"33 111" in out, out
        # Resize: the client sends the new size to the agent on SIGWINCH.
        fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
        os.kill(p.pid, 28)  # SIGWINCH
        time.sleep(0.5)
        os.write(master, b"stty size\n")
        assert b"40 120" in _read_until(master, b"40 120")
        os.write(master, b"exit 3\n")
        assert b"closed" in _read_until(master, b"closed")
        assert p.wait(10) == 3
    finally:
        if p.poll() is None:
            p.kill()
        os.close(master)


def test_shell_client_unknown_setup(manager_config):
    env = dict(os.environ, PYTHONPATH=str(ROOT))
    p = subprocess.run([sys.executable, "-m", "setup_manager", "--config", str(manager_config), "shell", "nope"],
                       env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=30)
    assert p.returncode == 1 and "unknown setup" in p.stderr
