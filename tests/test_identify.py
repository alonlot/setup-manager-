"""MCU identification: the analysis rules, and the full reset + capture + scripts flow."""
import asyncio

from setup_manager.config import ConfigStore
from setup_manager.fleet import Fleet
from setup_manager.mcu_detect import analyze

ZEPHYR_BOOT = "*** Booting Zephyr OS build v3.6.0 ***\r\nHello World! nrf52840dk/nrf52840\r\nuart:~$ "
ESP8266_ROM = "\r\n ets Jan  8 2013,rst cause:2, boot mode:(3,6)\r\n\r\nload 0x4010f000, len 3460, room 16\r\n"
START = "printf 'RESET\\n' > \"$SM_TTY\"\n"


# ---------------------------------------------------------------- analysis only

def test_esp32_s3_rom_is_sure():
    log = ("ESP-ROM:esp32s3-20210327\r\nBuild:Mar 27 2021\r\nrst:0x1 (POWERON),boot:0x8 (SPI_FAST_FLASH_BOOT)\r\n"
           "I (24) boot: ESP-IDF v5.1.2 2nd stage bootloader\r\nI (30) boot.esp32s3: Boot SPI Speed : 80MHz\r\n")
    g = analyze({"vid": "10c4", "pid": "ea60"}, log)
    assert g["mcu"] == "ESP32-S3" and g["sure"] and g["label"] == "ESP32-S3"
    assert "ESP-IDF v5.1.2" in g["firmware"]


def test_esp32_classic_and_esp8266():
    assert analyze({}, "ets Jun  8 2016 00:22:57\r\nrst:0x1 (POWERON_RESET),boot:0x13")["mcu"] == "ESP32"
    g = analyze({"vid": "1a86", "pid": "7523"}, ESP8266_ROM)
    assert g["mcu"] == "ESP8266" and g["sure"]


def test_micropython_rp2040():
    log = "MicroPython v1.22.1 on 2024-01-05; Raspberry Pi Pico W with RP2040\r\nType \"help()\""
    g = analyze({"vid": "2e8a", "pid": "0005"}, log)
    assert g["mcu"] == "RP2040" and g["sure"] and "MicroPython v1.22.1" in g["firmware"]


def test_st_info_beats_usb_hint():
    probe = "Found 1 stlink programmers\n  version:    V2J37S26\n  chipid:     0x413\n  dev-type:   STM32F4x5_F4x7\n"
    g = analyze({"vid": "0483", "pid": "374b"}, "", probe)
    assert g["mcu"] == "STM32F4x5/F4x7" and g["sure"]


def test_usb_only_is_not_sure():
    g = analyze({"vid": "0483", "pid": "374b", "product": "STM32 STLink"}, "")
    assert g["mcu"] == "STM32 (family)" and not g["sure"] and g["label"].startswith("probably")
    g = analyze({"vid": "1a86", "pid": "7523"}, "")
    assert not g["sure"] and g["label"].startswith("maybe")
    g = analyze({"vid": "1234", "pid": "5678"}, "\x00\xff garbage")
    assert g["mcu"] is None and g["label"] == "unknown"
    g = analyze({"vid": "1366", "pid": "1015", "serial": "000123456789"}, "")
    assert g["mcu"] is None and "debug probe" in g["evidence"][-1]


def test_script_output_counts():
    g = analyze({"vid": "1366", "pid": "1015", "serial": "000683123456"}, "", "NRF52840_xxAA_REV2\n")
    assert g["mcu"] == "nRF52840" and g["sure"]


def test_zephyr_board_target():
    g = analyze({}, ZEPHYR_BOOT)
    assert g["mcu"] == "nRF52840" and any("Zephyr" in f for f in g["firmware"])


# ---------------------------------------------------------------- full flow against the agent

def test_identify_with_scripts(manager_config, agent, fake_mcus):
    # 1-1 has /dev/ttyACM0 in the fake sysfs.
    fake_mcus(agent["devfs"], "ttyACM0", ZEPHYR_BOOT, replies={"kernel version": "Zephyr version 3.6.0\r\n"})

    async def go():
        async with Fleet(ConfigStore(manager_config)) as f:
            r = await f.identify("bench", "1-1", {
                "reset": "none", "baud": 115200, "duration": 1.5, "script": START,
                "after_script": "sleep 0.3; printf 'kernel version\\r' > \"$SM_TTY\"; echo after-ran"})
            cap, g = r["capture"], r["guess"]
            assert "Booting Zephyr" in cap["log"] and "Zephyr version 3.6.0" in cap["log"], cap
            assert [s["phase"] for s in cap["scripts"]] == ["start", "after_reset"]
            assert cap["scripts"][1]["stdout"].strip() == "after-ran"
            assert g["mcu"] == "nRF52840" and r["where"] == "Bench"
            # The result shows next to the device, and the options are remembered.
            devs = {d["busid"]: d for d in await f.usb("bench")}
            assert devs["1-1"]["mcu"]["mcu"] == "nRF52840"
            assert f.identify_options("bench", "1-1", devs["1-1"])["duration"] == 1.5
    asyncio.run(go())


def test_identify_auto_baud_and_rebind(manager_config, agent, fake_mcus):
    # 1-2 has /dev/ttyUSB0. The fake ESP8266 is readable only at 74880 baud.
    fake_mcus(agent["devfs"], "ttyUSB0", ESP8266_ROM, baud=74880)

    async def go():
        async with Fleet(ConfigStore(manager_config)) as f:
            await f.bind("bench", "1-2")
            r = await f.identify("bench", "1-2", {"reset": "rts", "duration": 0.8, "script": START})
            cap = r["capture"]
            assert [a["baud"] for a in cap["attempts"]] == [115200, 74880], cap["attempts"]
            assert cap["baud"] == 74880 and cap["attempts"][0]["readable"] < 0.5, cap["attempts"]
            assert any("modem lines not supported" in n for n in cap["notes"])  # a pty has no RTS
            assert r["guess"]["mcu"] == "ESP8266"
            devs = {d["busid"]: d for d in await f.usb("bench")}
            assert devs["1-2"]["bound"], "the device must be shared again, as before"
    asyncio.run(go())


def test_identify_profiles_and_errors(manager_config, agent):
    async def go():
        async with Fleet(ConfigStore(manager_config)) as f:
            assert "ESP32 / ESP8266" in f.identify_profiles()
            f.save_identify_profile("my board", {"reset": "dtr", "baud": 9600, "script": "echo hi"})
            assert f.identify_profiles()["my board"]["baud"] == 9600
            try:
                f.save_identify_profile("Auto", {"reset": "none"})
                raise AssertionError("built-in profile overwritten")
            except Exception as e:  # noqa: BLE001
                assert "built-in" in str(e)
            # No serial port: still a result, from the USB IDs only.
            r = await f.identify("bench", "1-3", {"reset": "none", "duration": 0.5})
            assert r["capture"]["ttys"] == [] and "no serial port" in r["capture"]["notes"][0]
            try:
                await f.identify("bench", "1-1", {"reset": "bogus"})
                raise AssertionError("bad reset accepted")
            except Exception as e:  # noqa: BLE001
                assert "unknown reset method" in str(e)
    asyncio.run(go())
