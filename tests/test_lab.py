"""The automation library (setup_manager.lab) and its pytest plugin."""
import asyncio
import os
import subprocess
import sys
import time

import pytest

from conftest import ROOT, TOKEN
from setup_manager import (AmbiguousDevice, BoardBusy, DeviceNotFound, ExpectTimeout, FlashFailed, Lab)
from setup_manager.config import ConfigStore
from setup_manager.fleet import Fleet, FleetError

BOOT = "*** Booting Zephyr OS build v3.6.0 ***\r\nHello World! nrf52840dk/nrf52840\r\n"


@pytest.fixture
def lab(manager_config, agent):
    with Lab(config=manager_config, owner="alice") as lab_:
        yield lab_


def test_lookup_and_commands(lab):
    assert [s.id for s in lab.setups()] == ["bench"]
    s = lab.setup("Bench")
    assert s.online() and s.run("echo hi").check().stdout == "hi\n"
    assert not s.run("exit 4").ok
    assert lab.device("0403:6001").busid == "1-2"
    assert lab.device("A50285BI").busid == "1-2"           # serial number
    assert lab.device("bench:1-1").vidpid == "0483:374b"
    lab.device("0403:6001").set_name("Board B")
    assert lab.device("board b").busid == "1-2"            # nickname
    assert lab.device("j-link").busid == "2-1.3"           # words of the name
    with pytest.raises(AmbiguousDevice) as e:
        lab.device("usb")                                  # FTDI ... USB UART and Realtek USB LAN
    assert len(e.value.candidates) == 2
    with pytest.raises(DeviceNotFound):
        lab.device("no such board")


def test_attached_context_detaches_on_error(lab):
    dev = lab.device("0403:6001")
    with pytest.raises(RuntimeError):
        with dev.attached():
            assert dev.attached_here and dev.state == "in_use"
            raise RuntimeError("test failed in the middle")
    dev.refresh()
    assert not dev.attached_here and dev.state == "available"


def test_console_expect(lab, agent, fake_mcus):
    fake_mcus(agent["devfs"], "ttyACM0", BOOT, replies={"kernel version": "Zephyr version 3.6.0\r\n"})
    dev = lab.device("0483:374b")
    with dev.serial() as con:
        con.send("RESET", end="\n")
        assert con.expect(r"Hello World! (\S+)").group(1) == "nrf52840dk/nrf52840"
        con.send("kernel version", end="\r")
        assert con.expect(r"version (\d+\.\d+\.\d+)").group(1) == "3.6.0"
        with pytest.raises(ExpectTimeout) as e:
            con.expect("never printed", timeout=0.5)
        assert e.value.pattern == "never printed"
        assert "Booting Zephyr" in con.log and "Zephyr version" in con.log
    # The default console of a device.
    dev.console.send("RESET", end="\n")
    assert "Hello" in dev.console.expect("Hello World").group(0)


def test_flash_check(lab, tmp_path):
    fw = tmp_path / "fw.bin"
    fw.write_bytes(b"\x00" * 100)
    dev = lab.device("0483:374b")
    r = dev.flash(str(fw), script='echo "size $(stat -c %s "$SM_FILE") $SM_X"', env={"SM_X": "x1"})
    assert r.ok and "size 100 x1" in r.stdout and r.where == "Bench"
    with pytest.raises(FlashFailed) as e:
        dev.flash(str(fw), script="echo bad >&2; exit 5")
    assert e.value.result.exit_code == 5 and "bad" in e.value.result.stderr
    assert not dev.flash(str(fw), script="exit 1", check=False).ok


def test_reservations(manager_config, agent, fake_mcus):
    fake_mcus(agent["devfs"], "ttyACM0", BOOT)
    with Lab(config=manager_config, owner="alice") as a, Lab(config=manager_config, owner="bob") as b:
        da, db = a.device("0403:6001"), b.device("0403:6001")
        with da.reserve(ttl=60):
            assert b.device("0403:6001").reserved_by["owner"] == "alice"
            with pytest.raises(BoardBusy, match="reserved by alice"):
                db.attach()
            with pytest.raises(BoardBusy):
                db.reserve()
            t0 = time.time()
            with pytest.raises(BoardBusy):
                db.reserve(wait=1.0)
            assert time.time() - t0 >= 0.9
            # The app, CLI and MCP (a plain Fleet without the lease) are blocked too...
            async def plain():
                async with Fleet(ConfigStore(manager_config)) as f:
                    with pytest.raises(FleetError) as e:
                        await f.bind("bench", "1-2")
                    assert e.value.status == 423
                    await f.bind("bench", "1-3", force=True)  # ...but only for the reserved device
                    await f.unbind("bench", "1-3")
            asyncio.run(plain())
            # The owner can use the board.
            with da.attached():
                assert da.attached_here
            assert any(r["owner"] == "alice" for r in b.reservations())
        r = db.reserve()
        r.release()
        # Serial: others can read, but not write, while it is reserved.
        ca = a.device("0483:374b")
        with ca.reserve():
            con_b = b.device("0483:374b").serial()
            con_b.read()
            with pytest.raises(BoardBusy):
                con_b.send("RESET", end="\n")
            ca.console.send("RESET", end="\n")
            assert ca.console.expect("Hello World")
        # Closing a Lab releases its reservations.
        da.reserve()
    with Lab(config=manager_config, owner="carol") as c:
        c.device("0403:6001").reserve().release()


def test_reservation_expires(lab, agent):
    import json
    import urllib.request
    from conftest import agent_url, insecure_ctx
    req = urllib.request.Request(agent_url(agent, "/api/reserve"), method="POST",
                                 data=json.dumps({"busid": "1-1", "owner": "crashed-job", "ttl": 5}).encode(),
                                 headers={"Authorization": f"Bearer {TOKEN}"})
    urllib.request.urlopen(req, timeout=5, context=insecure_ctx()).read()
    dev = lab.device("0483:374b")
    with pytest.raises(BoardBusy, match="crashed-job"):
        dev.reserve()
    dev.reserve(wait=8).release()  # the lease of the dead job ends after its ttl


def test_device_follows_its_serial_number(lab, agent):
    dev = lab.device("A50285BI")
    devices = agent["sysfs"] / "bus" / "usb" / "devices"
    os.rename(devices / "1-2", devices / "1-6")  # plugged into another port
    os.rename(devices / "1-2:1.0", devices / "1-6:1.0")
    dev.bind()  # the old busid fails, the library finds the device again and retries
    assert dev.busid == "1-6" and dev.refresh().state == "bound"
    dev.unbind()


INNER_TEST = '''
import pytest

@pytest.mark.board("0483:374b")
def test_boots(board, lab):
    assert any(r["owner"].startswith("ci-bot") for r in lab.reservations())
    board.console.send("RESET", end="\\n")
    board.console.expect("Hello World", timeout=10)

@pytest.mark.board("0483:374b")
def test_fails_on_purpose(board):
    board.console.send("RESET", end="\\n")
    board.console.expect("Hello", timeout=10)
    assert False, "on purpose"
'''


def test_pytest_plugin(manager_config, agent, fake_mcus, tmp_path):
    fake_mcus(agent["devfs"], "ttyACM0", BOOT)
    d = tmp_path / "hil"
    d.mkdir()
    (d / "test_board.py").write_text(INNER_TEST)
    env = dict(os.environ, PYTHONPATH=str(ROOT))
    p = subprocess.run([sys.executable, "-m", "pytest", "-p", "setup_manager.pytest_plugin", "-p",
                        "no:cacheprovider", "--lab-config", str(manager_config), "--lab-owner", "ci-bot",
                        "-q", "test_board.py"], cwd=d, env=env, capture_output=True, text=True, timeout=120)
    out = p.stdout + p.stderr
    assert "1 failed, 1 passed" in out, out
    assert "serial STMicroelectronics STM32 STLink (ttyACM0)" in out and "Booting Zephyr" in out, out


def test_lost_reservation_is_taken_again(manager_config, agent):
    """Released by force (the app's "take over"), or the agent restarted: the renew loop takes the
    board again and uses the new lease, else the job is locked out of its own board."""
    import json as _json
    import urllib.request
    from conftest import TOKEN, agent_url, insecure_ctx
    with Lab(config=manager_config, owner="alice") as lab:
        dev = lab.device("0483:374b")
        res = dev.reserve(ttl=6)  # renews every 2 s
        lease0 = res.lease
        req = urllib.request.Request(agent_url(agent, "/api/release"), method="POST",
                                     data=_json.dumps({"busid": dev.busid, "force": True}).encode(),
                                     headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"})
        with urllib.request.urlopen(req, context=insecure_ctx(), timeout=5) as r:
            assert _json.loads(r.read())["released"] is True
        time.sleep(3.5)
        assert [r["owner"] for r in lab.reservations()] == ["alice"]
        assert res.active and res.lease != lease0
        dev.bind()  # the owner can still use the board
        dev.unbind()
        res.release()
        assert lab.reservations() == []
