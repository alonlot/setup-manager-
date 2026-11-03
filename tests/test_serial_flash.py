"""Serial monitor, file transfer, flashing, "in use by" and nicknames."""
import asyncio
import hashlib
import json
import os

import pytest

from setup_manager.agent.setup_agent import parse_proc_net_tcp
from setup_manager.agent_client import AgentError
from setup_manager.config import ConfigStore
from setup_manager.fleet import Fleet, FleetError, used_by_text

BOOT = "*** Booting Zephyr OS build v3.6.0 ***\r\nHello World! nrf52840dk/nrf52840\r\n"


def run(coro):
    return asyncio.run(coro)


def test_serial_monitor(manager_config, agent, fake_mcus):
    fake_mcus(agent["devfs"], "ttyACM0", BOOT, replies={"kernel version": "Zephyr version 3.6.0\r\n"})

    async def go():
        async with Fleet(ConfigStore(manager_config)) as f:
            h = await f.serial_open("bench", "1-1", baud=115200)
            assert h["tty"] == "ttyACM0" and h["where"] == "setup"
            await f.serial_write("bench", "1-1", None, b"RESET\n")
            r = await f.serial_read("bench", "1-1", since=0, wait=3)
            assert b"Booting Zephyr" in r["data"]
            # A second reader has its own offset and sees the same bytes.
            r2 = await f.serial_read("bench", "1-1", since=0)
            assert r2["data"].startswith(r["data"][:20])
            # wait_for: only new output after the call.
            task = asyncio.create_task(f.serial_wait_for("bench", "1-1", None, r"version (\d+\.\d+)", timeout=5))
            await asyncio.sleep(0.3)
            await f.serial_write("bench", "1-1", None, b"kernel version\r")
            w = await task
            assert w["matched"] and w["groups"] == ["3.6"]
            w = await f.serial_wait_for("bench", "1-1", None, "never-printed", timeout=0.5)
            assert not w["matched"]
            assert any(p["tty"] == "ttyACM0" for p in await f.serial_ports("bench"))
            await f.serial_close("bench", "1-1")
            with pytest.raises(FleetError):
                await f.serial_read("bench", "1-1", since=0)
    run(go())


def test_serial_reconnects_after_reenumeration(manager_config, agent, fake_mcus):
    first = fake_mcus(agent["devfs"], "ttyACM0", "first\r\n")

    async def go():
        async with Fleet(ConfigStore(manager_config)) as f:
            await f.serial_open("bench", "1-1")
            first.close()  # the board disappears...
            await asyncio.sleep(0.5)
            fake_mcus(agent["devfs"], "ttyACM0", "second boot\r\n")  # ...and comes back on a new pty
            r = None
            for _ in range(30):
                await asyncio.sleep(0.2)
                r = await f.serial_read("bench", "1-1", since=0)
                if r["connected"]:
                    break
            texts = [e["text"] for e in r["events"]]
            assert "port disconnected" in texts and "reconnected" in texts, texts
            await f.serial_write("bench", "1-1", None, b"RESET\n")
            r = await f.serial_read("bench", "1-1", since=0, wait=3)
            assert b"second boot" in r["data"]
    run(go())


def test_identify_shares_the_open_port(manager_config, agent, fake_mcus):
    fake_mcus(agent["devfs"], "ttyACM0", BOOT)

    async def go():
        async with Fleet(ConfigStore(manager_config)) as f:
            await f.serial_open("bench", "1-1")
            r = await f.identify("bench", "1-1", {"reset": "none", "baud": 115200, "duration": 1,
                                                  "script": "printf 'RESET\\n' > \"$SM_TTY\""})
            assert "Booting Zephyr" in r["capture"]["log"]
            # The monitor also got the boot log, and its port is still open.
            m = await f.serial_read("bench", "1-1", since=0)
            assert b"Booting Zephyr" in m["data"]
    run(go())


def test_files(manager_config, agent, tmp_path):
    src = tmp_path / "fw.bin"
    src.write_bytes(os.urandom(3 * 1024 * 1024 + 7))

    async def go():
        async with Fleet(ConfigStore(manager_config)) as f:
            r = await f.upload("bench", str(src), "builds/fw.bin")
            assert r["size"] == src.stat().st_size
            assert r["sha256"] == hashlib.sha256(src.read_bytes()).hexdigest()
            listing = await f.files("bench", "builds")
            assert [x["name"] for x in listing["files"]] == ["fw.bin"]
            out = tmp_path / "back.bin"
            await f.download("bench", "builds/fw.bin", str(out))
            assert out.read_bytes() == src.read_bytes()
            with pytest.raises(FleetError) as e:
                await f.upload("bench", str(src), "../../etc/evil")
            assert "leaves the files folder" in e.value.message
    run(go())


def test_flash(manager_config, agent, fake_mcus, tmp_path):
    fake_mcus(agent["devfs"], "ttyACM0", BOOT)
    fw = tmp_path / "zephyr.hex"
    fw.write_text(":10000000DEADBEEF\n")
    script = ('echo "flashing $SM_FILE ($SM_FILE_EXT) for $SM_VID:$SM_PID at $SM_ADDRESS"\n'
              'test -f "$SM_FILE" || exit 3\n'
              "printf 'RESET\\n' > \"$SM_TTY\"\n")

    async def go():
        async with Fleet(ConfigStore(manager_config)) as f:
            await f.serial_open("bench", "1-1")  # an open monitor is paused during the flash
            r = await f.flash("bench", "1-1", str(fw), options={
                "script": script, "env": {"SM_ADDRESS": "0x08000000"}, "monitor_seconds": 1, "timeout": 30})
            assert r["ok"], r
            assert "zephyr.hex (hex) for 0483:374b at 0x08000000" in r["stdout"]
            assert "Booting Zephyr" in r["monitor"]["text"]
            m = await f.serial_read("bench", "1-1", since=0)
            assert any(e["text"] == "paused for flashing" for e in m["events"])
            assert f.flash_options("bench", {"busid": "1-1", "vid": "0483", "pid": "374b",
                                             "serial": "066DFF485457725187"})["file"] == str(fw)
            bad = await f.flash("bench", "1-1", str(fw), options={"script": "exit 7"})
            assert not bad["ok"] and bad["exit_code"] == 7
            assert "st-flash (STM32, ST-LINK)" in f.flash_profiles()
            assert f.flash_options("bench", {"busid": "1-2", "vid": "0403", "pid": "6001"})["profile"]
    run(go())


def test_in_use_by_and_nicknames(manager_config, agent):
    async def go():
        async with Fleet(ConfigStore(manager_config)) as f:
            await f.attach("bench", "1-2")
            raw = (await f.client("bench").usb())["devices"]
            d = next(x for x in raw if x["busid"] == "1-2")
            assert d["used_by"]["client"] == f.client_name and d["used_by"]["ip"] == "127.0.0.1"
            assert used_by_text(d) == f"{f.client_name} (127.0.0.1)"
            await f.detach("bench", "1-2")
            raw = (await f.client("bench").usb())["devices"]
            assert "used_by" not in next(x for x in raw if x["busid"] == "1-2")

            r = await f.set_nickname("bench", "1-1", "Board A (H7 rev2)")
            assert r["device"]["nickname"] == "Board A (H7 rev2)"
            devs = {x["busid"]: x for x in await f.usb("bench")}
            assert devs["1-1"]["display_name"] == "Board A (H7 rev2)"
            assert [x["busid"] for x in await f.find_usb("board a")] == ["1-1"]
            saved = json.loads(agent["config"].read_text())["nicknames"]
            assert saved == {"0483:374b:066dff485457725187": "Board A (H7 rev2)"}
            await f.set_nickname("bench", "1-1", "")
            devs = {x["busid"]: x for x in await f.usb("bench")}
            assert devs["1-1"]["nickname"] is None
    run(go())


def test_old_agent_message(manager_config, agent):
    async def go():
        async with Fleet(ConfigStore(manager_config)) as f:
            with pytest.raises(AgentError) as e:
                await f.client("bench").request("POST", "/api/not-there", {})
            assert "too old" in e.value.message and "setup-manager deploy" in e.value.message
    run(go())


def test_parse_proc_net_tcp():
    text = ("  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"
            "   0: 00000000:0CA8 00000000:0000 0A 00000000:00000000 00:00000000 00000000     0        0 1\n"
            "   1: 0F01A8C0:0CA8 2101A8C0:D4F2 01 00000000:00000000 00:00000000 00000000     0        0 2\n"
            "   2: 0F01A8C0:0016 2201A8C0:D4F3 01 00000000:00000000 00:00000000 00000000     0        0 3\n")
    assert parse_proc_net_tcp(text) == {"192.168.1.33"}
    v6 = ("  sl  local_address                         remote_address                        st\n"
          "   0: 0000000000000000FFFF00000F01A8C0:0CA8 0000000000000000FFFF00002301A8C0:D000 01 0 0 0 0 0 0 0\n")
    assert parse_proc_net_tcp(v6) == {"192.168.1.35"}


def test_cli_serial_wait_for(manager_config, agent, fake_mcus):
    import subprocess
    import sys

    from conftest import ROOT
    fake_mcus(agent["devfs"], "ttyACM0", BOOT)
    env = dict(os.environ, PYTHONPATH=str(ROOT))
    base = [sys.executable, "-m", "setup_manager", "--config", str(manager_config), "serial", "bench", "1-1",
            "--read-only"]
    p = subprocess.Popen(base + ["--wait-for", r"Hello World! (\w+)", "--timeout", "15"], env=env,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL)

    async def trigger():
        async with Fleet(ConfigStore(manager_config)) as f:
            for _ in range(50):  # wait until the CLI has opened the port
                if (await f.client("bench").serial_list())["sessions"]:
                    break
                await asyncio.sleep(0.1)
            await asyncio.sleep(0.5)
            await f.serial_write("bench", "1-1", None, b"RESET\n")
    run(trigger())
    out, _ = p.communicate(timeout=20)
    assert p.returncode == 0 and b"Hello World! nrf52840dk" in out
    p = subprocess.run(base + ["--wait-for", "never", "--timeout", "1"], env=env, capture_output=True,
                       stdin=subprocess.DEVNULL, timeout=20)
    assert p.returncode == 1
