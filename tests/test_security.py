"""Encryption: TLS with pinned certificates, the encrypted USB/IP tunnel, old agents, the firewall rule."""
import asyncio
import hashlib
import json
import os
import socket
import ssl
import struct
import subprocess
import sys
import threading
import urllib.error
import urllib.request

import pytest

from conftest import FAKES, TOKEN, agent_url, free_port, insecure_ctx, start_agent, stop_agent
from setup_manager.config import ConfigStore
from setup_manager.fleet import Fleet, FleetError


def run(coro):
    return asyncio.run(coro)


def cert_fp(agent):
    cert = os.path.join(os.path.dirname(agent["config"]), "cert.pem")
    with open(cert) as f:
        return hashlib.sha256(ssl.PEM_cert_to_DER_cert(f.read())).hexdigest()


def test_trust_on_first_use_and_pinning(manager_config, agent):
    async def go():
        async with Fleet(ConfigStore(manager_config)) as f:
            assert f.encryption("bench")["fingerprint"] == ""
            await f.refresh("bench")
            # The fingerprint is saved (like SSH known_hosts) and it is the agent's certificate.
            assert ConfigStore(manager_config).get_setup("bench")["fingerprint"] == cert_fp(agent)
            assert f.summaries()[0]["encrypted"] is True
            ping = await f.client("bench").ping()
            assert ping["tls"] and ping["tunnel"] and ping["fingerprint"] == cert_fp(agent)
    run(go())


def test_changed_certificate_is_refused(manager_config, agent):
    async def go():
        async with Fleet(ConfigStore(manager_config)) as f:
            f.trust("bench", "ab" * 32)  # another certificate than the agent has
            e = await f.refresh("bench")
            assert e["online"] is False and "certificate of Bench changed" in e["error"]
            with pytest.raises(FleetError) as err:
                await f.exec("bench", "true")
            assert "attack" in err.value.message
            f.trust("bench", None)  # setup-manager trust bench --forget
            assert (await f.refresh("bench"))["online"]
            assert f.encryption("bench")["fingerprint"] == cert_fp(agent)
    run(go())


class RecordingProxy:
    """A TCP proxy that keeps a copy of all the bytes (what somebody on the network sees)."""

    def __init__(self, target_port):
        self.target = target_port
        self.seen = bytearray()
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while True:
            try:
                c, _ = self.sock.accept()
            except OSError:
                return
            t = socket.create_connection(("127.0.0.1", self.target))
            for a, b in ((c, t), (t, c)):
                threading.Thread(target=self._pipe, args=(a, b), daemon=True).start()

    def _pipe(self, a, b):
        try:
            while True:
                d = a.recv(65536)
                if not d:
                    break
                self.seen += d
                b.sendall(d)
        except OSError:
            pass
        for s in (a, b):
            try:
                s.close()
            except OSError:
                pass


def test_nothing_readable_on_the_network(manager_config, agent):
    proxy = RecordingProxy(agent["port"])
    data = json.loads(manager_config.read_text())
    data["setups"][0]["port"] = proxy.port
    manager_config.write_text(json.dumps(data))

    async def go():
        async with Fleet(ConfigStore(manager_config)) as f:
            assert any(d["name"] == "STMicroelectronics STM32 STLink" for d in await f.usb("bench"))
            r = await f.exec("bench", "echo SECRET-MARKER-$((40+2))")
            assert r["stdout"] == "SECRET-MARKER-42\n"
            await f.attach("bench", "1-2")  # USB/IP goes through the same encrypted port
            await f.detach("bench", "1-2")
    run(go())
    seen = bytes(proxy.seen)
    assert len(seen) > 2000 and seen[:1] == b"\x16"  # TLS records
    for secret in (TOKEN.encode(), b"SECRET-MARKER", b"STLink", b"Authorization", b"SMTUNNEL", b"1-2\x00"):
        assert secret not in seen, secret


def _manager_option(manager_config, **kw):
    data = json.loads(manager_config.read_text())
    data.update(kw)
    manager_config.write_text(json.dumps(data))


def test_usbip_goes_through_the_tunnel(manager_config, agent):
    _manager_option(manager_config, encrypt_usbip=True)  # "Encrypt USB/IP" in the preferences

    async def go():
        async with Fleet(ConfigStore(manager_config)) as f:
            r = await f.attach("bench", "1-2")
            assert "encrypted" in r["message"] or r["port"]
            assert agent["usbipd"].imports == ["1-2"]          # usbipd got the import...
            assert agent["usbipd"].peers == ["127.0.0.1"]      # ...from the agent's relay, not from the network
            ports = await f.local_ports()
            assert ports[0]["setup"] == "bench" and ports[0]["encrypted"] is True
            assert ports[0]["host"] == "127.0.0.1" and ports[0]["remote_port"] != 3240
            devs = {d["busid"]: d for d in await f.usb("bench")}
            assert devs["1-2"]["attached_here"]
            await f.detach("bench", "1-2")
            assert await f.local_ports() == []
    run(go())


def _raw_tunnel(agent, line):
    s = insecure_ctx().wrap_socket(socket.create_connection(("127.0.0.1", agent["port"]), timeout=5))
    s.sendall(line)
    return s


def test_tunnel_needs_the_token(agent):
    s = _raw_tunnel(agent, b"SMTUNNEL wrong-token\n")
    assert s.recv(100).startswith(b"ERR unauthorized")
    assert agent["usbipd"].peers == []  # nothing reached usbipd


def test_tunnel_respects_reservations(manager_config, agent):
    from setup_manager import Lab
    _manager_option(manager_config, encrypt_usbip=True)

    async def plain_attach():
        async with Fleet(ConfigStore(manager_config)) as f:
            with pytest.raises(FleetError) as e:
                await f.attach("bench", "1-2")
            return e.value
    with Lab(config=manager_config, owner="alice") as lab:
        dev = lab.device("0403:6001")
        with dev.reserve():
            dev.bind()  # shared, but reserved: only alice can import it through the tunnel
            err = run(plain_attach())
            assert err.status == 423 and "reserved by alice" in err.message
            assert agent["usbipd"].imports == []
            with dev.attached():
                assert dev.attached_here
            assert agent["usbipd"].imports == ["1-2"]


@pytest.fixture
def plain_agent(tmp_path, sysfs):
    info = start_agent(tmp_path, sysfs, tls=False)  # like an agent before version 0.4
    yield info
    stop_agent(info)


@pytest.fixture
def plain_config(tmp_path, plain_agent, monkeypatch):
    monkeypatch.setenv("FAKE_SYSFS", str(plain_agent["sysfs"]))
    monkeypatch.setenv("FAKE_USBIP_STATE", str(tmp_path / "usbip-state.json"))
    monkeypatch.setenv("SETUP_MANAGER_STATE", str(tmp_path / "state"))
    path = tmp_path / "manager.json"
    path.write_text(json.dumps({
        "local_usbip": {"command": [sys.executable, str(FAKES / "fake_usbip_client.py")], "sudo": False},
        "setups": [{"id": "old", "name": "Old", "host": "127.0.0.1", "port": plain_agent["port"], "token": TOKEN}],
    }))
    return path


def test_old_agent_still_works_but_not_encrypted(plain_config, plain_agent):
    async def go():
        async with Fleet(ConfigStore(plain_config)) as f:
            assert (await f.refresh("old"))["online"]
            assert f.summaries()[0]["encrypted"] is False
            r = await f.attach("old", "1-2")
            assert "not encrypted" in r["message"]
            await f.detach("old", "1-2")
            assert ConfigStore(plain_config).get_setup("old")["fingerprint"] == ""
    run(go())


def test_require_encryption_refuses_old_agents(plain_config, plain_agent):
    data = json.loads(plain_config.read_text())
    data["require_encryption"] = True
    plain_config.write_text(json.dumps(data))

    async def go():
        async with Fleet(ConfigStore(plain_config)) as f:
            e = await f.refresh("old")
            assert e["online"] is False and "does not use encryption" in e["error"]
    run(go())


def test_pinned_setup_never_falls_back_to_plain(plain_config, plain_agent):
    """Downgrade protection: once a setup has a fingerprint, plain HTTP is never used for it."""
    async def go():
        async with Fleet(ConfigStore(plain_config)) as f:
            f.trust("old", "cd" * 32)
            e = await f.refresh("old")
            assert e["online"] is False
    run(go())


def test_plain_http_to_the_tls_port_fails(agent):
    with pytest.raises(OSError):
        urllib.request.urlopen(f"http://127.0.0.1:{agent['port']}/api/ping", timeout=5).read()


def test_firewall_rule(tmp_path, monkeypatch):
    from setup_manager.agent import setup_agent as sa
    log = tmp_path / "calls.txt"
    for tool in ("iptables", "ip6tables"):
        f = tmp_path / tool
        f.write_text(f'#!/bin/sh\necho "{tool} $*" >> {log}\n'
                     f'case "$1" in -C) exit 1;; -D) exit 1;; esac\nexit 0\n')
        f.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    assert sa.usbip_firewall(True, 3240) == ["iptables: blocked", "ip6tables: blocked"]
    calls = log.read_text().splitlines()
    match = "INPUT -p tcp --dport 3240 ! -i lo -m comment --comment setup-agent-usbip"
    # REJECT with a TCP reset: a `usbip attach` by hand fails at once, it does not hang (DROP did).
    assert f"iptables -I {match} -j REJECT --reject-with tcp-reset" in calls
    assert f"ip6tables -I {match} -j REJECT --reject-with tcp-reset" in calls
    assert f"iptables -D {match} -j DROP" in calls  # the rule of an older agent is removed
    assert not any(c.startswith("iptables -I") and c.endswith("-j DROP") for c in calls)
    assert sa.usbip_firewall(False, 3240) == []  # nothing to remove


def test_firewall_replaces_the_old_drop_rule(tmp_path, monkeypatch):
    """An agent update: the DROP rule of the old agent goes, the REJECT rule comes."""
    from setup_manager.agent import setup_agent as sa
    log, state = tmp_path / "calls.txt", tmp_path / "rules"
    state.write_text("DROP\n")
    f = tmp_path / "iptables"
    # A tiny iptables: the file "rules" holds the targets of the setup-agent rules, one per line.
    f.write_text(f'''#!/bin/sh
echo "iptables $*" >> {log}
op=$1; shift
case "$*" in *"-j DROP"*) t=DROP;; *) t=REJECT;; esac
case "$op" in
  -C) grep -qx "$t" {state};;
  -I) echo "$t" >> {state};;
  -D) grep -qx "$t" {state} || exit 1; grep -vx "$t" {state} > {state}.new; mv {state}.new {state};;
esac
''')
    f.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:/usr/bin:/bin")
    assert sa.usbip_firewall(True, 3240) == ["iptables: blocked"]
    assert state.read_text().split() == ["REJECT"]
    assert sa.usbip_firewall(True, 3240) == []  # already there: nothing to do
    assert state.read_text().split() == ["REJECT"]
    assert sa.usbip_firewall(False, 3240) == ["iptables: unblocked"]
    assert state.read_text().split() == []


def test_tls_setup_without_a_pin_is_pinned_on_the_first_connection(manager_config, agent):
    """transport "tls" with an empty fingerprint (after `trust --forget`) must verify from the next connection on."""
    async def go():
        async with Fleet(ConfigStore(manager_config)) as f:
            await f.update_setup("bench", {"transport": "tls", "fingerprint": ""})
            assert ConfigStore(manager_config).get_setup("bench")["fingerprint"] == cert_fp(agent)
            c = f.client("bench")
            await c.ensure()
            assert c.encrypted and c.fingerprint == cert_fp(agent) and c.ssl is not False
            # The app keeps its Fleet open: a cleared fingerprint must be pinned again, not left unverified.
            f.trust("bench", None)
            assert ConfigStore(manager_config).get_setup("bench")["fingerprint"] == ""
            await f.refresh("bench")
            assert ConfigStore(manager_config).get_setup("bench")["fingerprint"] == cert_fp(agent)
    run(go())


# --------------------------------------------------------------------------- plain usbip for devices shared by hand

def _api(agent, method, path, body=None):
    req = urllib.request.Request(agent_url(agent, path), method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, context=insecure_ctx(), timeout=10) as r:
        return json.loads(r.read())


def _plain_usbip(port, request):
    """What a plain `usbip` client gets from the agent's filter (port 3240 from the network goes there)."""
    with socket.create_connection(("127.0.0.1", port), timeout=10) as s:
        s.sendall(request)
        out = b""
        while True:
            chunk = s.recv(65536)
            if not chunk:
                return out
            out += chunk


def _import(busid):
    return struct.pack("!HHI", 0x0111, 0x8003, 0) + busid.encode().ljust(32, b"\0")


def _devlist_busids(reply):
    n = struct.unpack("!I", reply[8:12])[0]
    out, pos = [], 12
    for _ in range(n):
        out.append(reply[pos + 256:pos + 288].split(b"\0", 1)[0].decode())
        pos += 312 + 4 * reply[pos + 311]
    return out


def _bind_by_hand(sysfs, busid):
    subprocess.run([sys.executable, str(FAKES / "fake_usbip_host.py"), "bind", "-b", busid], check=True,
                   env=dict(os.environ, FAKE_SYSFS=str(sysfs)), stdout=subprocess.DEVNULL)


def test_plain_usbip_works_for_devices_shared_by_hand(tmp_path, sysfs):
    relay_port = free_port()
    agent = start_agent(tmp_path, sysfs, usbip_network="manual", usbip_relay_port=relay_port)
    try:
        st = _api(agent, "GET", "/api/status")
        assert st["usbip_network"] == "manual" and st["usbip_relay_port"] == relay_port
        _api(agent, "POST", "/api/usb/bind", {"busid": "1-1"})  # shared by Setup Manager
        _bind_by_hand(sysfs, "1-2")                                # `sudo usbip bind -b 1-2` on the setup PC
        by = {d["busid"]: d["shared_by"] for d in _api(agent, "GET", "/api/usb")["devices"]}
        assert by["1-1"] == "setup-manager" and by["1-2"] == "hand" and by["1-3"] is None
        usbipd = agent["usbipd"]
        usbipd.exported = ["1-1", "1-2"]

        # `usbip attach -r SETUP-PC -b 1-2`: relayed to usbipd.
        assert _plain_usbip(relay_port, _import("1-2")).endswith(b"IMPORTED")
        assert usbipd.imports == ["1-2"]
        # 1-1 was shared by Setup Manager: only the encrypted tunnel. usbip says "Request Failed".
        assert _plain_usbip(relay_port, _import("1-1")) == struct.pack("!HHI", 0x0111, 0x0003, 1)
        assert usbipd.imports == ["1-2"]
        # `usbip list -r SETUP-PC` shows only the device shared by hand.
        assert _devlist_busids(_plain_usbip(relay_port, struct.pack("!HHI", 0x0111, 0x8005, 0))) == ["1-2"]

        # A flash or identify unbinds for a moment and binds again: "by hand" stays "by hand".
        _api(agent, "POST", "/api/usb/unbind", {"busid": "1-2"})
        _api(agent, "POST", "/api/usb/bind", {"busid": "1-2", "restore_owner": True})
        assert _plain_usbip(relay_port, _import("1-2")).endswith(b"IMPORTED")
        # A device that is reserved is refused, also when it was shared by hand.
        r = _api(agent, "POST", "/api/reserve", {"busid": "1-2", "owner": "ci", "ttl": 60})
        assert _plain_usbip(relay_port, _import("1-2")) == struct.pack("!HHI", 0x0111, 0x0003, 1)
        _api(agent, "POST", "/api/release", {"busid": "1-2", "lease": r["lease"]})
    finally:
        stop_agent(agent)

    # An agent restart does not make the device of Setup Manager available without encryption.
    agent = start_agent(tmp_path, sysfs, usbip_network="manual", usbip_relay_port=relay_port)
    try:
        assert _plain_usbip(relay_port, _import("1-1")) == struct.pack("!HHI", 0x0111, 0x0003, 1)
        assert _plain_usbip(relay_port, _import("1-2")).endswith(b"IMPORTED")
        # Setup Manager stops sharing 1-1, then you share it by hand: now plain usbip works for it.
        _api(agent, "POST", "/api/usb/unbind", {"busid": "1-1"})
        _bind_by_hand(sysfs, "1-1")
        assert _plain_usbip(relay_port, _import("1-1")).endswith(b"IMPORTED")
    finally:
        stop_agent(agent)


def test_tunnel_only_has_no_plain_usbip(tmp_path, sysfs):
    relay_port = free_port()
    agent = start_agent(tmp_path, sysfs, usbip_network="tunnel", usbip_relay_port=relay_port)
    try:
        assert _api(agent, "GET", "/api/status")["usbip_network"] == "tunnel"
        with pytest.raises(OSError):
            socket.create_connection(("127.0.0.1", relay_port), timeout=2).close()
    finally:
        stop_agent(agent)


def test_firewall_redirects_port_3240_to_the_filter(tmp_path, monkeypatch):
    from setup_manager.agent import setup_agent as sa
    log = tmp_path / "calls.txt"
    f = tmp_path / "iptables"
    # -S lists a redirect of an older config (another port): it must go.
    f.write_text(f'''#!/bin/sh
echo "iptables $*" >> {log}
case "$*" in
  "-t nat -S PREROUTING") echo "-A PREROUTING -p tcp -m tcp --dport 3240 ! -i lo -m comment --comment setup-agent-usbip -j REDIRECT --to-ports 9999";;
  -C*|"-t nat -C"*|-D*) exit 1;;
esac
exit 0
''')
    f.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:/usr/bin:/bin")
    notes = sa.usbip_firewall(True, 3240, redirect_to=3241)
    calls = log.read_text().splitlines()
    assert ("iptables -t nat -I PREROUTING -p tcp --dport 3240 ! -i lo -m comment --comment setup-agent-usbip "
            "-j REDIRECT --to-ports 3241") in calls
    assert ("iptables -t nat -D PREROUTING -p tcp -m tcp --dport 3240 ! -i lo -m comment --comment "
            "setup-agent-usbip -j REDIRECT --to-ports 9999") in calls
    assert any("goes to the filter" in n for n in notes)
    # The block stays as a safety net (if the redirect is gone, nothing is open).
    assert any(c.startswith("iptables -I INPUT") and "REJECT" in c for c in calls)
    # Tunnel only: the redirect is removed.
    log.write_text("")
    sa.usbip_firewall(True, 3240, redirect_to=None)
    assert any(c.startswith("iptables -t nat -D PREROUTING") for c in log.read_text().splitlines())


def test_the_manager_says_when_a_device_was_shared_by_hand(tmp_path, sysfs):
    agent = start_agent(tmp_path, sysfs, usbip_network="manual", usbip_relay_port=free_port())
    cfg = tmp_path / "manager.json"
    cfg.write_text(json.dumps({"local_usbip": {"command": [sys.executable, str(FAKES / "fake_usbip_client.py")],
                                               "sudo": False},
                               "setups": [{"id": "pc", "name": "PC", "host": "127.0.0.1", "port": agent["port"],
                                           "token": TOKEN}]}))
    try:
        _bind_by_hand(sysfs, "1-2")

        async def go():
            async with Fleet(ConfigStore(cfg)) as f:
                await f.bind("pc", "1-1")
                return {d["busid"]: d["state_detail"] for d in await f.usb("pc")}
        detail = run(go())
        assert detail["1-2"].endswith("(shared by hand: plain usbip works)")
        assert "by hand" not in detail["1-1"]
    finally:
        stop_agent(agent)


def test_plain_usbip_for_the_manager_with_the_token_over_tls(manager_config, agent):
    """The default: USB/IP is plain (to usbipd, no relay), only the token goes (over TLS) to the agent."""
    data = json.loads(manager_config.read_text())
    data["setups"][0]["usbip_port"] = agent["usbipd"].port
    manager_config.write_text(json.dumps(data))
    assert _api(agent, "GET", "/api/ping")["usbip_direct"] is True

    async def go():
        async with Fleet(ConfigStore(manager_config)) as f:
            r = await f.attach("bench", "1-2")
            assert "not encrypted" in r["message"] and "TLS" in r["message"]
            assert agent["usbipd"].imports == ["1-2"]  # straight to usbipd, not through the tunnel
            ports = await f.local_ports()
            assert [(p["setup"], p["busid"], p["encrypted"]) for p in ports] == [("bench", "1-2", False)]
            assert ports[0]["remote_port"] == agent["usbipd"].port
            await f.detach("bench", "1-2")
    run(go())


def test_allow_needs_the_token_and_respects_reservations(agent):
    req = urllib.request.Request(agent_url(agent, "/api/usb/allow"), method="POST",
                                 data=json.dumps({"busid": "1-2"}).encode(),
                                 headers={"Content-Type": "application/json"})
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req, context=insecure_ctx(), timeout=10)
    assert e.value.code == 401
    r = _api(agent, "POST", "/api/usb/allow", {"busid": "1-2"})
    assert r["ip"] == "127.0.0.1" and r["seconds"] > 0
    res = _api(agent, "POST", "/api/reserve", {"busid": "1-2", "owner": "ci", "ttl": 60})
    with pytest.raises(urllib.error.HTTPError) as e:
        _api(agent, "POST", "/api/usb/allow", {"busid": "1-2"})
    assert e.value.code == 423
    _api(agent, "POST", "/api/release", {"busid": "1-2", "lease": res["lease"]})


def test_tunnel_only_agent_makes_the_manager_use_the_tunnel(tmp_path, sysfs):
    agent = start_agent(tmp_path, sysfs, usbip_network="tunnel")
    try:
        assert _api(agent, "GET", "/api/ping")["usbip_direct"] is False
        with pytest.raises(urllib.error.HTTPError) as e:
            _api(agent, "POST", "/api/usb/allow", {"busid": "1-2"})
        assert e.value.code == 409
    finally:
        stop_agent(agent)
