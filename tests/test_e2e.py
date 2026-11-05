"""Manager <-> agent end to end, with the fake usbip binaries."""
import asyncio
import json

import pytest

from setup_manager.config import ConfigStore
from setup_manager.fleet import Fleet, FleetError


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


def test_usb_of_an_offline_setup_is_a_fleet_error(tmp_path):
    cfg = tmp_path / "m.json"
    cfg.write_text(json.dumps({"setups": [{"id": "dead", "host": "127.0.0.1", "port": 1, "token": "x"}]}))

    async def go():
        async with Fleet(ConfigStore(cfg)) as f:
            with pytest.raises(FleetError, match="cannot reach"):
                await f.usb("dead")
            with pytest.raises(FleetError, match="cannot reach"):
                await f.serial_ports("dead")
    run(go())
