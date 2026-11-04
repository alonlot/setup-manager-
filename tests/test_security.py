"""Encryption: TLS with pinned certificates, the encrypted USB/IP tunnel, old agents, the firewall rule."""
import asyncio
import hashlib
import json
import os
import socket
import ssl
import sys
import threading
import urllib.request

import pytest

from conftest import FAKES, TOKEN, insecure_ctx, start_agent, stop_agent
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


def test_usbip_goes_through_the_tunnel(manager_config, agent):
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
    assert "iptables -I INPUT -p tcp --dport 3240 ! -i lo -m comment --comment setup-agent-usbip -j DROP" in calls
    assert sa.usbip_firewall(False, 3240) == []  # nothing to remove


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
