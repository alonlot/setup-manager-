"""Flash profiles: a bash script per tool. They run on the PC that has the device.

Variables: SM_FILE (the firmware), SM_FILE_EXT (bin, hex, elf, uf2, ...),
SM_TTY, SM_TTYS, SM_BUSID, SM_VID, SM_PID, SM_SERIAL, SM_SETUP, and the
profile "env" values (for example SM_TARGET). Edit the scripts in the app, or
save your own profiles.
"""

from __future__ import annotations

from typing import Any

BUILTIN_FLASH_PROFILES: dict[str, dict[str, Any]] = {
    "esptool (ESP32 / ESP8266)": {
        "env": {"SM_OFFSET": "0x0", "SM_BAUD": "460800"},
        "monitor_seconds": 4,
        "script": (
            'T=$(command -v esptool || command -v esptool.py) || { echo "esptool is not installed"; exit 127; }\n'
            '# A merged image goes to 0x0. For an app image only, set SM_OFFSET=0x10000.\n'
            '"$T" --port "$SM_TTY" --baud "$SM_BAUD" write_flash "$SM_OFFSET" "$SM_FILE"\n'),
        "help": "Merged .bin at 0x0 (set SM_OFFSET for an app-only image). Resets the chip after.",
    },
    "st-flash (STM32, ST-LINK)": {
        "env": {"SM_ADDRESS": "0x08000000"},
        "monitor_seconds": 3,
        "script": (
            'SER=${SM_SERIAL:+--serial $SM_SERIAL}\n'
            'case "$SM_FILE_EXT" in\n'
            '  hex) st-flash $SER --reset --format ihex write "$SM_FILE" ;;\n'
            '  *)   st-flash $SER --reset write "$SM_FILE" "$SM_ADDRESS" ;;\n'
            'esac\n'),
        "help": ".bin at SM_ADDRESS, or .hex. Needs stlink-tools.",
    },
    "OpenOCD": {
        "env": {"SM_INTERFACE": "interface/stlink.cfg", "SM_TARGET": "target/stm32f4x.cfg"},
        "monitor_seconds": 3,
        "script": (
            'openocd -f "$SM_INTERFACE" ${SM_SERIAL:+-c "adapter serial $SM_SERIAL"} -f "$SM_TARGET" \\\n'
            '  -c "program {$SM_FILE} verify reset exit"\n'),
        "help": "Set SM_TARGET to your target config (.elf, .hex or .bin at the flash start).",
    },
    "pyOCD (CMSIS-DAP, DAPLink, ST-LINK)": {
        "env": {"SM_TARGET": ""},
        "monitor_seconds": 3,
        "script": 'pyocd flash ${SM_SERIAL:+-u $SM_SERIAL} ${SM_TARGET:+-t $SM_TARGET} "$SM_FILE"\n',
        "help": "Set SM_TARGET (for example stm32f407vg, nrf52840) when pyOCD cannot find it.",
    },
    "nrfjprog (Nordic, J-Link)": {
        "env": {},
        "monitor_seconds": 3,
        "script": (
            'SN=$(echo "$SM_SERIAL" | sed "s/^0*//")\n'
            'nrfjprog ${SN:+-s $SN} --program "$SM_FILE" --sectorerase --verify --reset\n'),
        "help": ".hex file. Needs the Nordic command line tools.",
    },
    "J-Link Commander": {
        "env": {"SM_DEVICE": "CHANGE-ME (e.g. STM32F407VG, NRF52840_XXAA)", "SM_ADDRESS": "0x08000000"},
        "monitor_seconds": 3,
        "script": (
            'CMD=$(mktemp)\n'
            'case "$SM_FILE_EXT" in bin) LOAD="loadbin \\"$SM_FILE\\" $SM_ADDRESS" ;; *) LOAD="loadfile \\"$SM_FILE\\"" ;; esac\n'
            'printf "connect\\nr\\n%s\\nr\\ng\\nexit\\n" "$LOAD" > "$CMD"\n'
            'JLinkExe ${SM_SERIAL:+-USB $SM_SERIAL} -if SWD -speed 4000 -device "$SM_DEVICE" -autoconnect 1 '
            '-NoGui 1 -CommandFile "$CMD"\n'
            'rc=$?; rm -f "$CMD"; exit $rc\n'),
        "help": "Set SM_DEVICE to the J-Link device name.",
    },
    "picotool (RP2040 / RP2350)": {
        "env": {},
        "monitor_seconds": 3,
        "script": 'picotool load -x "$SM_FILE" -f\n',
        "help": ".uf2 or .elf. -f reboots a running Pico SDK app into BOOTSEL first.",
    },
    "avrdude (Arduino Uno / Nano)": {
        "env": {"SM_PART": "atmega328p", "SM_BAUD": "115200"},
        "monitor_seconds": 3,
        "script": 'avrdude -p "$SM_PART" -c arduino -P "$SM_TTY" -b "$SM_BAUD" -D -U flash:w:"$SM_FILE":a\n',
        "help": "Old Nano bootloader: set SM_BAUD=57600. Mega: SM_PART=atmega2560 and -c wiring.",
    },
    "dfu-util (STM32 DFU)": {
        "env": {"SM_ADDRESS": "0x08000000"},
        "monitor_seconds": 3,
        "script": 'dfu-util -a 0 -s "$SM_ADDRESS":leave -D "$SM_FILE"\n',
        "help": "The STM32 must be in DFU mode (BOOT0).",
    },
    "Copy to a USB drive (DAPLink, UF2)": {
        "env": {"SM_DRIVE": ""},
        "monitor_seconds": 4,
        "script": (
            '# Copy the file to the mass-storage drive of the board (DAPLink, UF2 bootloader).\n'
            'D=${SM_DRIVE:-$(lsblk -nrpo NAME,MOUNTPOINT | awk \'$2 ~ /(MAINTENANCE|DAPLINK|RPI-RP2|MBED)/{print $2}\' '
            '| head -n1)}\n'
            '[ -n "$D" ] || { echo "no board drive found: set SM_DRIVE"; exit 1; }\n'
            'cp "$SM_FILE" "$D"/ && sync && echo "copied to $D"\n'),
        "help": "The drive must be mounted on the PC that has the device.",
    },
}


def suggest_profile(dev: dict[str, Any]) -> str:
    """A good first profile for a device, from its MCU result and its USB IDs."""
    vid, pid = dev.get("vid"), dev.get("pid")
    family = ((dev.get("mcu") or {}).get("mcu") or "").upper()
    if vid == "303a" or "ESP" in family or (vid, pid) in (("10c4", "ea60"), ("1a86", "55d4")):
        return "esptool (ESP32 / ESP8266)"
    if vid == "0483" and (pid or "").startswith("37"):
        return "st-flash (STM32, ST-LINK)"
    if vid == "0483" and pid == "df11":
        return "dfu-util (STM32 DFU)"
    if vid == "1366":
        return "nrfjprog (Nordic, J-Link)" if family.startswith("NRF") else "J-Link Commander"
    if vid == "2e8a":
        return "picotool (RP2040 / RP2350)"
    if vid in ("2341", "2a03") or "ATMEGA" in family:
        return "avrdude (Arduino Uno / Nano)"
    if vid == "0d28" or "CMSIS-DAP" in (dev.get("product") or "").upper():
        return "pyOCD (CMSIS-DAP, DAPLink, ST-LINK)"
    if vid == "1a86" and pid == "7523":
        return "esptool (ESP32 / ESP8266)"
    return "OpenOCD"
