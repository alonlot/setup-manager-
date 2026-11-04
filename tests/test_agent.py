import json
import urllib.error
import urllib.request

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
