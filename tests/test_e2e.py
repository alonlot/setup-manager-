"""Manager <-> agent end to end, with the fake usbip binaries."""
import asyncio
import json

import aiohttp
from aiohttp.test_utils import TestClient, TestServer

from setup_manager.config import ConfigStore
from setup_manager.fleet import Fleet, FleetError
from setup_manager.web import build_app


def run(coro):
    return asyncio.run(coro)


def test_fleet_flow(manager_config):
    async def go():
        async with Fleet(ConfigStore(manager_config)) as f:
            entry = await f.refresh("bench")
            assert entry["online"], entry
            devs = {d["busid"]: d for d in await f.usb("Bench")}  # by name works too
            assert devs["1-1"]["state"] == "available" and not devs["1-1"]["attached_here"]

            r = await f.attach("bench", "1-1")
            assert r["port"]["busid"] == "1-1", r
            devs = {d["busid"]: d for d in await f.usb("bench")}
            assert devs["1-1"]["attached_here"] and devs["1-1"]["state"] == "in_use"
            ports = await f.local_ports()
            assert ports[0]["setup"] == "bench"

            r = await f.attach("bench", "1-1")
            assert "already attached" in r["message"]

            await f.detach("bench", "1-1")
            devs = {d["busid"]: d for d in await f.usb("bench")}
            assert not devs["1-1"]["attached_here"] and devs["1-1"]["state"] == "available"
            assert await f.local_ports() == []

            try:
                await f.attach("bench", "2-1.3")
                raise AssertionError("protected device attached")
            except FleetError as e:
                assert e.status == 403

            r = await f.exec("bench", "echo $((6*7))")
            assert r["stdout"].strip() == "42"
            many = await f.exec_many(None, "echo hi")
            assert many[0]["stdout"] == "hi\n"

            found = await f.find_usb("stlink")
            assert [d["busid"] for d in found] == ["1-1"]
            found = await f.find_usb("ttyusb0")
            assert [d["busid"] for d in found] == ["1-2"]

            disc = await f.discover(timeout=0.6)
            assert disc and disc[0]["name"] == "bench-test" and disc[0]["known_as"] == "bench"
    run(go())


def test_sticky_reattach(manager_config, tmp_path):
    async def go():
        async with Fleet(ConfigStore(manager_config)) as f:
            await f.set_sticky("bench", "1-1", True)
            ports = await f.local_ports()
            assert len(ports) == 1
            # The connection drops (board reset): detach behind the manager's back.
            await f.local.detach(ports[0]["port"])
            assert await f.local_ports() == []
            await f._sticky_pass()
            assert len(await f.local_ports()) == 1
            assert f.sticky_report()[0]["ok"] is True
            # A manual detach turns keep-attached off.
            await f.detach("bench", "1-1")
            assert f.store.sticky() == []
    run(go())


def test_auto_bind_toggle(manager_config):
    async def go():
        async with Fleet(ConfigStore(manager_config)) as f:
            r = await f.set_auto_bind("bench", "1-1", True)
            assert r["auto_bind"] == ["0483:374b:066DFF485457725187"]
            devs = {d["busid"]: d for d in await f.usb("bench")}
            assert devs["1-1"]["auto_bind"] and devs["1-1"]["bound"]
            r = await f.set_auto_bind("bench", "1-1", False)
            assert r["auto_bind"] == []
    run(go())


def test_offline_setup(tmp_path):
    cfg = tmp_path / "m.json"
    cfg.write_text(json.dumps({"setups": [{"id": "dead", "host": "127.0.0.1", "port": 1, "token": "x"}]}))

    async def go():
        async with Fleet(ConfigStore(cfg)) as f:
            e = await f.refresh("dead")
            assert e["online"] is False and "cannot reach" in e["error"]
            try:
                await f.status("nope")
            except FleetError as err:
                assert err.status == 404
    run(go())


def test_web_api_and_shell(manager_config):
    async def go():
        fleet = Fleet(ConfigStore(manager_config))
        await fleet.open()
        app = build_app(fleet, background=False)
        async with TestClient(TestServer(app)) as client:
            r = await client.get("/")
            assert r.status == 200 and "Setup Manager" in await r.text()
            r = await client.get("/static/app.js")
            assert r.status == 200

            await fleet.refresh_all()
            ov = await (await client.get("/api/overview")).json()
            assert ov["setups"][0]["online"] and ov["setups"][0]["has_token"] and "token" not in ov["setups"][0]

            r = await client.post("/api/setups/bench/usb/1-2/attach", json={})
            assert r.status == 200, await r.text()
            local = await (await client.get("/api/local")).json()
            assert local["ports"][0]["busid"] == "1-2"
            r = await client.post("/api/setups/bench/usb/1-2/detach", json={})
            assert r.status == 200
            r = await client.post("/api/setups/bench/usb/1-2/nope", json={})
            assert r.status == 404
            r = await client.post("/api/setups/bench/exec", json={"command": "echo web"})
            assert (await r.json())["stdout"] == "web\n"

            r = await client.post("/api/setups", json={"name": "Second Bench", "host": "127.0.0.2", "port": 1})
            data = await r.json()
            assert data["setup"]["id"] == "second-bench" and data["online"] is False
            r = await client.put("/api/setups/second-bench", json={"notes": "rack 2"})
            assert (await r.json())["setup"]["notes"] == "rack 2"
            assert (await client.delete("/api/setups/second-bench")).status == 200

            # Interactive shell through the manager proxy.
            ws = await client.ws_connect("/api/setups/bench/shell?cols=100&rows=30")
            await ws.send_str(json.dumps({"t": "i", "d": "stty size; echo MARK$((1+1))\n"}))
            buf = b""
            for _ in range(100):
                msg = await asyncio.wait_for(ws.receive(), 10)
                if msg.type == aiohttp.WSMsgType.BINARY:
                    buf += msg.data
                if b"MARK2" in buf and b"30 100" in buf:
                    break
            assert b"MARK2" in buf and b"30 100" in buf, buf
            await ws.send_str(json.dumps({"t": "r", "c": 120, "r": 40}))
            await ws.send_str(json.dumps({"t": "i", "d": "stty size\n"}))
            buf = b""
            for _ in range(100):
                msg = await asyncio.wait_for(ws.receive(), 10)
                if msg.type == aiohttp.WSMsgType.BINARY:
                    buf += msg.data
                if b"40 120" in buf:
                    break
            assert b"40 120" in buf
            await ws.send_str(json.dumps({"t": "i", "d": "exit\n"}))
            got_exit = False
            for _ in range(100):
                msg = await asyncio.wait_for(ws.receive(), 10)
                if msg.type == aiohttp.WSMsgType.TEXT and json.loads(msg.data).get("t") == "exit":
                    got_exit = True
                if msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSING):
                    break
            assert got_exit
            await ws.close()
        await fleet.close()
    run(go())


def test_manager_token(manager_config):
    data = json.loads(manager_config.read_text())
    data["token"] = "mgr-secret"
    manager_config.write_text(json.dumps(data))

    async def go():
        fleet = Fleet(ConfigStore(manager_config))
        await fleet.open()
        async with TestClient(TestServer(build_app(fleet, background=False))) as client:
            assert (await client.get("/api/setups")).status == 401
            assert (await client.get("/")).status == 200
            assert (await client.post("/api/login", json={"token": "bad"})).status == 401
            assert (await client.post("/api/login", json={"token": "mgr-secret"})).status == 200
            assert (await client.get("/api/setups")).status == 200  # cookie
        await fleet.close()
    run(go())


def test_cross_site_blocked(manager_config):
    async def go():
        fleet = Fleet(ConfigStore(manager_config))
        await fleet.open()
        async with TestClient(TestServer(build_app(fleet, background=False))) as client:
            evil = {"Origin": "http://evil.example"}
            r = await client.post("/api/setups/bench/exec", json={"command": "id"}, headers=evil)
            assert r.status == 403
            r = await client.post("/api/setups/bench/exec", data='{"command": "id"}',
                                  headers={"Content-Type": "text/plain"})
            assert r.status == 415
            r = await client.get("/api/setups", headers={"Host": "attacker.example:8800"})
            assert r.status == 403  # DNS rebinding
            try:
                await client.ws_connect("/api/setups/bench/shell", headers=evil)
                raise AssertionError("cross-site websocket accepted")
            except aiohttp.WSServerHandshakeError as e:
                assert e.status == 403
            host = f"{client.host}:{client.port}"
            r = await client.get("/api/setups", headers={"Origin": f"http://{host}"})
            assert r.status == 200
        await fleet.close()
    run(go())
