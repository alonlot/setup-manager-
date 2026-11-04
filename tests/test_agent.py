import json
import os
import shutil
import socket
import time
import urllib.error
import urllib.request

import pytest

from conftest import TOKEN, agent_url, insecure_ctx


def call(agent, method, path, body=None, token=TOKEN):
    req = urllib.request.Request(agent_url(agent, path), method=method,
                                 data=json.dumps(body).encode() if body is not None else None)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    if body is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=10, context=insecure_ctx()) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_ping_no_auth(agent):
    code, data = call(agent, "GET", "/api/ping", token=None)
    assert code == 200 and data["name"] == "bench-test" and data["service"] == "setup-agent"


def test_auth_required(agent):
    assert call(agent, "GET", "/api/status", token=None)[0] == 401
    assert call(agent, "GET", "/api/status", token="wrong")[0] == 401


def test_status(agent):
    code, st = call(agent, "GET", "/api/status")
    assert code == 200
    assert st["usb_count"] == 4  # hub hidden
    assert st["memory"]["total"] > 0
    assert "usbipd_running" in st["usbip"]
    assert st["usbip"]["usbip_installed"] is True


def test_usb_list(agent):
    code, data = call(agent, "GET", "/api/usb")
    devs = {d["busid"]: d for d in data["devices"]}
    assert list(devs) == ["1-1", "1-2", "1-3", "2-1.3"]  # natural order, no hub, no root hub
    st = devs["1-1"]
    assert st["vidpid"] == "0483:374b" and st["serial"] == "066DFF485457725187"
    assert st["ttys"] == ["ttyACM0"] and st["state"] == "available" and not st["bound"]
    assert devs["1-2"]["ttys"] == ["ttyUSB0"]
    assert devs["1-3"]["net"] == ["enx001122"]
    assert devs["2-1.3"]["protected"] is True
    _, data = call(agent, "GET", "/api/usb?all=1")
    assert "1-4" in {d["busid"] for d in data["devices"]}


def test_bind_unbind(agent):
    code, r = call(agent, "POST", "/api/usb/bind", {"busid": "1-1"})
    assert code == 200, r
    assert r["device"]["bound"] and r["device"]["state"] == "bound"
    code, r = call(agent, "POST", "/api/usb/bind", {"busid": "1-1"})
    assert code == 200 and r["message"] == "already bound"
    code, r = call(agent, "POST", "/api/usb/unbind", {"busid": "1-1"})
    assert code == 200 and not r["device"]["bound"]


def test_bind_safety(agent):
    assert call(agent, "POST", "/api/usb/bind", {"busid": "2-1.3"})[0] == 403  # protected
    code, r = call(agent, "POST", "/api/usb/bind", {"busid": "1-3"})  # network adapter
    assert code == 409 and "network" in r["error"]
    assert call(agent, "POST", "/api/usb/bind", {"busid": "1-3", "force": True})[0] == 200
    assert call(agent, "POST", "/api/usb/bind", {"busid": "9-9"})[0] == 404
    assert call(agent, "POST", "/api/usb/bind", {"busid": "1-1; rm -rf /"})[0] == 400


def test_exec(agent):
    code, r = call(agent, "POST", "/api/exec", {"command": "echo hello; echo oops >&2; exit 3"})
    assert code == 200
    assert r["stdout"] == "hello\n" and r["stderr"] == "oops\n" and r["exit_code"] == 3
    code, r = call(agent, "POST", "/api/exec", {"command": "sleep 5", "timeout": 1})
    assert r["timed_out"] is True


def test_config_update_and_autobind(agent):
    import time
    code, cfg = call(agent, "POST", "/api/config", {"auto_bind": ["0403:6001"]})
    assert code == 200 and cfg["auto_bind"] == ["0403:6001"] and "token" not in cfg
    saved = json.loads(agent["config"].read_text())
    assert saved["auto_bind"] == ["0403:6001"] and saved["token"] == TOKEN
    for _ in range(40):
        _, data = call(agent, "GET", "/api/usb")
        dev = next(d for d in data["devices"] if d["busid"] == "1-2")
        if dev["bound"]:
            break
        time.sleep(0.2)
    assert dev["bound"] and dev["auto_bind"]


def test_log(agent):
    call(agent, "POST", "/api/exec", {"command": "true"})
    _, r = call(agent, "GET", "/api/log")
    assert any("exec as" in line["msg"] for line in r["lines"])


# ----------------------------------------------------------------------------- agent internals

def _sa():
    from setup_manager.agent import setup_agent as sa
    return sa


def _other_user():
    import pwd
    sa = _sa()
    return next(p.pw_name for p in pwd.getpwall() if p.pw_name != sa.current_user())


def test_run_as_argv_is_not_a_login_shell(monkeypatch):
    """`runuser -l` / `su -l` would drop the environment (SM_*) and the cwd that Popen sets."""
    sa = _sa()
    monkeypatch.setattr(sa.os, "geteuid", lambda: 0)
    user = _other_user()
    cfg = {"shell": "/bin/sh"}
    monkeypatch.setattr(sa.shutil, "which", lambda name: "/usr/sbin/runuser")
    argv, home = sa.shell_argv(cfg, user, "echo $SM_FILE")
    assert argv == ["runuser", "-u", user, "--", "/bin/sh", "-c", "echo $SM_FILE"]
    assert sa.shell_argv(cfg, user)[0] == ["runuser", "-l", user]  # the interactive shell stays a login shell
    monkeypatch.setattr(sa.shutil, "which", lambda name: None)
    argv, home = sa.shell_argv(cfg, user, "echo $SM_FILE")
    assert argv == ["su", "-p", "-s", "/bin/sh", user, "-c", "echo $SM_FILE"]


@pytest.mark.skipif(os.geteuid() != 0, reason="user switching needs root")
def test_run_as_keeps_env_and_cwd(tmp_path):
    import pwd
    import tempfile
    sa = _sa()
    try:
        user = pwd.getpwnam("nobody").pw_name
    except KeyError:
        pytest.skip("no user 'nobody'")
    cwd = tempfile.mkdtemp(prefix="sm-cwd-")  # pytest's tmp_path is 0700: the other user cannot see it
    os.chmod(cwd, 0o755)
    try:
        cfg = {"shell": "/bin/sh"}
        r = sa.run_command(cfg, 'echo "$(id -un) $(pwd) $SM_FILE $HOME"', 10, cwd=cwd, user=user,
                           extra_env={"SM_FILE": "/fw/app.bin"})
        assert r["exit_code"] == 0, r
        assert r["stdout"].split() == [user, cwd, "/fw/app.bin", pwd.getpwnam(user).pw_dir]
    finally:
        os.rmdir(cwd)


@pytest.mark.skipif(not shutil.which("setsid"), reason="needs setsid")
def test_exec_timeout_with_a_grandchild_that_keeps_the_pipes():
    sa = _sa()
    t = time.time()
    r = sa.run_command({"shell": "/bin/sh"}, "echo before; setsid sleep 4 & sleep 30", 1)
    assert r["timed_out"] is True and r["stdout"] == "before\n"
    assert time.time() - t < 4.5, "the timeout must not wait for the escaped grandchild"


def _pty_session(baud=115200):
    import pty
    sa = _sa()
    master, slave = pty.openpty()
    path = os.ttyname(slave)
    os.close(slave)
    return master, sa.SerialSession(path, baud)


def test_serial_pause_hands_the_fd_to_the_reader_thread():
    """After pause() the reader thread must not read the old fd number: it can belong to another file
    (in flash(): the pipes of the flash tool), and the data would end up in the serial buffer."""
    master, s = _pty_session()
    try:
        for _ in range(20):
            os.write(master, b"hello")
            time.sleep(0.02)
            s.pause("test")
            assert s.fd is None and not s.info()["connected"]
            r, w = os.pipe()  # gets the lowest free fd number: the one the session just closed
            os.set_blocking(r, False)
            os.write(w, b"PIPEDATA")
            time.sleep(0.25)
            assert os.read(r, 100) == b"PIPEDATA", "the serial reader stole the data of another fd"
            assert b"PIPEDATA" not in bytes(s.buf)
            os.close(r)
            os.close(w)
            s.resume()
            assert s.info()["connected"]
        assert any(e["text"] == "test" for e in s.events) and any(e["text"] == "resumed" for e in s.events)
    finally:
        s.close()
        os.close(master)


def test_serial_read_since_after_the_end():
    master, s = _pty_session()
    try:
        os.write(master, b"abc")
        for _ in range(50):
            if s.end == 3:
                break
            time.sleep(0.02)
        r = s.read(since=10 ** 9, wait=0)  # an offset from before the port was closed and opened again
        assert r["reset"] is True and r["offset"] == 3 and r["data"] == b""
        r = s.read(since=1, wait=0)
        assert "reset" not in r and r["data"] == b"bc"
    finally:
        s.close()
        os.close(master)


def test_files_list_with_a_dangling_symlink(agent):
    files_dir = json.loads(agent["config"].read_text())["files_dir"]
    os.makedirs(files_dir, exist_ok=True)
    os.symlink("/nonexistent/target", os.path.join(files_dir, "dangling"))
    with open(os.path.join(files_dir, "real.bin"), "wb") as f:
        f.write(b"x" * 10)
    code, r = call(agent, "GET", "/api/files/list?path=.")
    assert code == 200, r
    assert [x["name"] for x in r["files"]] == ["dangling", "real.bin"]
    assert r["files"][1]["size"] == 10


def test_bad_numbers_give_400(agent):
    code, r = call(agent, "GET", "/api/log?n=x")
    assert (code, r["error"]) == (400, "n must be a number")
    code, r = call(agent, "POST", "/api/reserve", {"busid": "1-1", "owner": "me", "ttl": "soon"})
    assert (code, r["error"]) == (400, "ttl must be a number")
    code, r = call(agent, "POST", "/api/usb/flash", {"busid": "1-1", "script": "true", "timeout": "long"})
    assert (code, r["error"]) == (400, "timeout must be a number")
    code, r = call(agent, "POST", "/api/exec", {"command": "true", "timeout": "long"})
    assert (code, r["error"]) == (400, "timeout must be a number")


def _put(agent, path, body, content_length=None):
    """A raw PUT /api/files, so a body can be cut off. Returns the response text ('' when none)."""
    s = socket.create_connection(("127.0.0.1", agent["port"]), timeout=10)
    if agent["tls"]:
        s = insecure_ctx().wrap_socket(s)
    head = ("PUT /api/files?path=%s HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer %s\r\n"
            "Content-Length: %d\r\n\r\n" % (path, TOKEN, len(body) if content_length is None else content_length))
    s.sendall(head.encode() + body)
    try:
        out = b""
        while True:  # the status line, the headers and the whole body (as told by Content-Length)
            chunk = s.recv(65536)
            if not chunk:
                break
            out += chunk
            head, sep, rest = out.partition(b"\r\n\r\n")
            if sep:
                import re
                m = re.search(rb"(?i)content-length: *(\d+)", head)
                if m and len(rest) >= int(m.group(1)):
                    break
    except OSError:
        out = b""
    s.close()
    return out.decode(errors="replace")


def test_refused_upload_gets_an_answer_and_leaves_no_part_file(agent):
    files_dir = json.loads(agent["config"].read_text())["files_dir"]
    os.makedirs(files_dir, exist_ok=True)
    with open(os.path.join(files_dir, "blocker"), "wb") as f:
        f.write(b"i am a file, not a folder")
    # The path component "blocker" is a file: makedirs fails with an OSError. The 2 MB body must be
    # read anyway, so that the client gets the error and not a connection reset.
    resp = _put(agent, "blocker/fw.bin", b"x" * (2 * 1024 * 1024))
    assert resp.startswith("HTTP/1.1 500") and "cannot write" in resp, resp[:300]
    # A body that is cut off: the temp file is removed.
    resp = _put(agent, "cut.bin", b"0123456789", content_length=100000)
    for _ in range(50):
        if not [n for n in os.listdir(files_dir) if n.endswith(".part")]:
            break
        time.sleep(0.1)
    assert sorted(os.listdir(files_dir)) == ["blocker"], os.listdir(files_dir)
    # A good upload still works, with the temp file gone.
    resp = _put(agent, "ok.bin", b"0123456789")
    assert resp.startswith("HTTP/1.1 200"), resp[:300]
    assert sorted(os.listdir(files_dir)) == ["blocker", "ok.bin"]


def test_identify_capture_restores_the_monitor_baud():
    import pty
    sa = _sa()
    master, slave = pty.openpty()
    path = os.ttyname(slave)
    os.close(slave)
    hub = sa.SerialHub()
    try:
        s = hub.open(path, 9600)  # the user's monitor
        cap = sa.HubCapture(hub, [path], 115200).start()
        assert s.baud == 115200
        cap.close()
        assert s.baud == 9600 and not s.closed, "the monitor keeps its own baud rate and stays open"
        # A port the monitor did not use is closed again.
        hub.close(path)
        cap = sa.HubCapture(hub, [path], 74880).start()
        cap.close()
        assert hub.list() == []
    finally:
        hub.close(path)
        os.close(master)


def test_tls_front_survives_accept_errors(monkeypatch):
    import errno
    sa = _sa()
    calls = []

    class FakeSock:
        closed = False

        def accept(self):
            calls.append(1)
            if len(calls) == 2:
                self.closed = True  # the shutdown path: the listening socket is closed
            raise OSError(errno.EMFILE, "Too many open files")

        def fileno(self):
            return -1 if self.closed else 7

    front = sa.TlsFront.__new__(sa.TlsFront)
    front.sock = FakeSock()
    monkeypatch.setattr(sa.time, "sleep", lambda t: None)
    front.serve()  # returns only because the socket was closed, after a retry
    assert len(calls) == 2


UBUNTU_WRAPPER = """#!/bin/sh
echo "WARNING: usbip not found for kernel 6.8.0-45" >&2
echo "" >&2
echo "  You may need to install the following packages for this specific kernel:" >&2
echo "    linux-tools-6.8.0-45-generic" >&2
exit 2
"""


def _kernel_tools(tmp_path, real=True):
    """A broken Ubuntu wrapper /usr/bin/usbip, and (real=True) the real binary in the kernel tools dir."""
    import sys
    from conftest import FAKES
    wrapper = tmp_path / "bin" / "usbip"
    wrapper.parent.mkdir()
    wrapper.write_text(UBUNTU_WRAPPER)
    wrapper.chmod(0o755)
    old = tmp_path / "linux-tools" / "5.15.0-9-generic" / "usbip"  # older tools that do not run
    old.parent.mkdir(parents=True)
    old.write_text("#!/bin/sh\nexit 1\n")
    old.chmod(0o755)
    if real:
        tool = tmp_path / "linux-tools" / "6.8.0-45-generic" / "usbip"
        tool.parent.mkdir()
        tool.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKES / "fake_usbip_host.py"}" "$@"\n')
        tool.chmod(0o755)
    return {"usbip_cmd": str(wrapper), "kernel_tools_dirs": [str(tmp_path / "linux-tools" / "*")]}


def test_bind_works_when_the_ubuntu_wrapper_misses_the_kernel_tools(tmp_path, sysfs):
    from conftest import start_agent, stop_agent
    info = start_agent(tmp_path, sysfs, **_kernel_tools(tmp_path))
    try:
        code, st = call(info, "GET", "/api/status")
        assert code == 200
        u = st["usbip"]
        assert u["usbip_installed"] is True, u
        assert u["usbip_path"] == str(tmp_path / "linux-tools" / "6.8.0-45-generic" / "usbip")
        assert "linux-tools-" in u["usbip_hint"] and u["usbip_error"] is None
        code, r = call(info, "POST", "/api/usb/bind", {"busid": "1-1"})
        assert code == 200 and r["device"]["bound"], r
    finally:
        stop_agent(info)


def test_bind_without_kernel_tools_gives_the_apt_hint(tmp_path, sysfs):
    from conftest import start_agent, stop_agent
    info = start_agent(tmp_path, sysfs, **_kernel_tools(tmp_path, real=False))
    try:
        _, st = call(info, "GET", "/api/status")
        u = st["usbip"]
        assert u["usbip_installed"] is False
        assert "not found for kernel" in u["usbip_error"]
        assert "sudo apt install linux-tools-" in u["usbip_hint"]
        code, r = call(info, "POST", "/api/usb/bind", {"busid": "1-1"})
        assert code == 500
        assert "not found for kernel" in r["error"] and "sudo apt install linux-tools-" in r["error"], r
    finally:
        stop_agent(info)


def test_kernel_tools_order(tmp_path):
    sa = _sa()
    assert sorted(["6.8.0-9-generic", "6.10.0-1-generic", "6.8.0-45-generic"], key=sa.version_key) == \
        ["6.8.0-9-generic", "6.8.0-45-generic", "6.10.0-1-generic"]
    for d in ("linux-tools/6.8.0-45-generic", "linux-tools/6.11.0-8-generic", "linux-tools/6.9.0-1-generic",
              "linux-tools-6.5.0-3", "linux-tools/nothing-here"):
        (tmp_path / d).mkdir(parents=True)
        if d != "linux-tools/nothing-here":
            (tmp_path / d / "usbip").write_text("#!/bin/sh\n")
            (tmp_path / d / "usbip").chmod(0o755)
    dirs = [str(tmp_path / "linux-tools" / "*"), str(tmp_path / "linux-tools-*")]
    got = [os.path.basename(os.path.dirname(p)) for p in sa.kernel_tools_candidates("usbip", dirs, "6.5.0-3-generic")]
    assert got == ["linux-tools-6.5.0-3", "6.11.0-8-generic", "6.9.0-1-generic", "6.8.0-45-generic"]
    got = sa.kernel_tools_candidates("usbip", dirs, "7.0.0-1-generic")
    assert os.path.basename(os.path.dirname(got[0])) == "6.11.0-8-generic"
