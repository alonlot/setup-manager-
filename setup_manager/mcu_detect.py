"""Guess which MCU is behind a USB device, from its boot log, its USB IDs and debug probe output.

The result is a list of candidates with a confidence (0..1) and the evidence
for each one. `sure` is True only when one candidate is strong and clearly
ahead of the others. Add new signatures to LOG_RULES, CHIP_PATTERNS or USB_HINTS.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07")

ESP_CHIPS = {
    "esp32": "ESP32", "esp32s2": "ESP32-S2", "esp32s3": "ESP32-S3", "esp32c2": "ESP32-C2", "esp32c3": "ESP32-C3",
    "esp32c5": "ESP32-C5", "esp32c6": "ESP32-C6", "esp32c61": "ESP32-C61", "esp32h2": "ESP32-H2",
    "esp32p4": "ESP32-P4", "esp8266": "ESP8266",
}

# (regex, candidate or callable(match) -> candidate, family, weight, evidence text). Applied to the log.
LOG_RULES: list[tuple[str, Any, str, float, str]] = [
    # Espressif ROM and ESP-IDF.
    (r"ESP-ROM:\s*(esp32[a-z0-9]*)", lambda m: ESP_CHIPS.get(m.group(1).lower(), m.group(1).upper()), "ESP32", 0.95,
     "Espressif ROM banner"),
    (r"ets Jun\s+8 2016", "ESP32", "ESP32", 0.85, "ESP32 ROM date (ets Jun 8 2016)"),
    (r"ets Jan\s+8 2013", "ESP8266", "ESP32", 0.9, "ESP8266 ROM date (ets Jan 8 2013)"),
    (r"\bboot\.(esp32[a-z0-9]*):", lambda m: ESP_CHIPS.get(m.group(1).lower(), m.group(1).upper()), "ESP32", 0.9,
     "ESP-IDF bootloader line"),
    (r"\b(?:cpu_start|esp_image|spi_flash|heap_init):", "ESP32 (family)", "ESP32", 0.6, "ESP-IDF log tags"),
    (r"rst:0x[0-9a-f]+ \(\w+\),boot:0x[0-9a-f]+", "ESP32 (family)", "ESP32", 0.7, "Espressif reset reason line"),
    (r"Chip is (ESP32[-\w]*)", lambda m: m.group(1).upper().split("-D0")[0], "ESP32", 0.9, "esptool chip line"),
    # Raspberry Pi.
    (r"\bRP2040\b", "RP2040", "RP2", 0.85, "RP2040 named in the log"),
    (r"\bRP2350\b", "RP2350", "RP2", 0.85, "RP2350 named in the log"),
    # Nordic / Zephyr board targets (board/soc names in sample output).
    (r"\b(nrf5[0-9]{3,4}|nrf91[0-9]{2}|nrf54[a-z][0-9]{2})(?:dk|_dk|dongle)?(?:[/@_]\w+)?\b",
     lambda m: "nRF" + m.group(1)[3:].upper(), "nRF", 0.75, "Nordic board or SoC name"),
    (r"Booting nRF Connect SDK", "nRF (family)", "nRF", 0.6, "nRF Connect SDK banner"),
    # Linux / U-Boot class.
    (r"CPU:\s+(Freescale|NXP)?\s*(i\.MX\s?\w+)", lambda m: m.group(2).replace(" ", ""), "i.MX", 0.85,
     "U-Boot CPU line"),
    (r"Machine model:\s*(.+)", lambda m: m.group(1).strip()[:60], "Linux SoC", 0.7, "Linux machine model"),
    (r"^Model:\s*(.+)$", lambda m: m.group(1).strip()[:60], "Linux SoC", 0.6, "U-Boot model line"),
]

# Chip names that can show up in any text (log, product string, probe output).
CHIP_PATTERNS: list[tuple[str, str, Any]] = [
    (r"\bSTM32\s?([A-Z][0-9][0-9A-Z]{1,10})\b", "STM32", lambda m: "STM32" + m.group(1)),
    (r"\b(ESP32(?:-[SCHP][0-9]{1,2})?)\b", "ESP32", lambda m: m.group(1).upper()),
    (r"\bESP8266\b", "ESP32", "ESP8266"),
    (r"\b(nRF5[0-9]{3,4}|nRF91[0-9]{2}|nRF54[HL][0-9]{2})\b", "nRF", lambda m: "nRF" + m.group(1)[3:].upper()),
    (r"\b(ATmega[0-9]+[A-Z]*)\b", "AVR", lambda m: m.group(1)),
    (r"\b(AT)?(SAM[DEL][0-9]{2}[A-Z0-9]*)\b", "SAM", lambda m: "ATSAM" + m.group(2)[3:] if m.group(1) else m.group(2)),
    (r"\b(GD32[A-Z0-9]+)\b", "GD32", lambda m: m.group(1)),
    (r"\b(CH32V[0-9]+[A-Z0-9]*)\b", "CH32V", lambda m: m.group(1)),
    (r"\b(LPC[0-9]{3,4}[A-Z0-9]*)\b", "LPC", lambda m: m.group(1)),
    (r"\b(MIMXRT[0-9]{4}[A-Z0-9]*|i\.MX ?RT ?[0-9]{3,4})\b", "i.MX RT", lambda m: m.group(1).replace(" ", "")),
    (r"\b(EFR32[A-Z0-9]+|EFM32[A-Z0-9]+)\b", "EFR32", lambda m: m.group(1)),
    (r"\b(CC(?:13|26|32)[0-9]{2}[A-Z0-9]*)\b", "TI CC", lambda m: m.group(1)),
    (r"\b(MSP430[A-Z0-9]+|MSP432[A-Z0-9]+)\b", "MSP", lambda m: m.group(1)),
    (r"\b(TM4C[0-9]+[A-Z0-9]*)\b", "TM4C", lambda m: m.group(1)),
    (r"\b(CY8C[0-9A-Z]+|PSoC ?[0-9]+)\b", "PSoC", lambda m: m.group(1)),
    (r"\b(MK[0-9]{2}[A-Z0-9]+|MKL[0-9]{2}[A-Z0-9]+)\b", "Kinetis", lambda m: m.group(1)),
    (r"\b(BL6[0-9]{2})\b", "Bouffalo", lambda m: m.group(1)),
]


def _wordish(pattern: str) -> str:
    """Use letter/digit boundaries instead of \\b, so 'NRF52840_xxAA' still matches 'nRF52840'."""
    if pattern.startswith(r"\b"):
        pattern = r"(?<![A-Za-z0-9])" + pattern[2:]
    if pattern.endswith(r"\b"):
        pattern = pattern[:-2] + r"(?![A-Za-z0-9])"
    return pattern


CHIP_PATTERNS = [(_wordish(p), f, n) for p, f, n in CHIP_PATTERNS]

FIRMWARE_RULES: list[tuple[str, Any]] = [
    (r"ESP-IDF (v[0-9][\w.\-]*)", lambda m: f"ESP-IDF {m.group(1)}"),
    (r"Booting Zephyr OS build ([\w.\-+]+)", lambda m: f"Zephyr {m.group(1)}"),
    (r"Booting nRF Connect SDK (v?[\w.\-+]+)", lambda m: f"nRF Connect SDK {m.group(1)}"),
    (r"MicroPython (v[0-9][\w.\-]*)", lambda m: f"MicroPython {m.group(1)}"),
    (r"CircuitPython ([0-9][\w.\-]*)", lambda m: f"CircuitPython {m.group(1)}"),
    (r"U-Boot (?:SPL )?(20[0-9]{2}\.[0-9]{2}[\w.\-]*)", lambda m: f"U-Boot {m.group(1)}"),
    (r"Linux version ([0-9][\w.\-+]*)", lambda m: f"Linux {m.group(1)}"),
    (r"Mbed OS ?([0-9][\w.]*)?", lambda m: "Mbed OS" + (f" {m.group(1)}" if m.group(1) else "")),
    (r"NuttShell|NuttX", "NuttX"),
    (r"FreeRTOS", "FreeRTOS"),
    (r"RT-Thread", "RT-Thread"),
    (r"Arduino", "Arduino"),
]

# vid -> {pid or "*": (candidate, family, weight, evidence)}
USB_HINTS: dict[str, dict[str, tuple[str, str, float, str]]] = {
    "303a": {"1001": ("ESP32 (family, native USB)", "ESP32", 0.55, "Espressif USB-Serial-JTAG (ESP32-S3/C3/C6/H2)"),
             "0002": ("ESP32-S2", "ESP32", 0.5, "Espressif ESP32-S2 native USB"),
             "*": ("ESP32 (family, native USB)", "ESP32", 0.45, "Espressif USB vendor ID")},
    "2e8a": {"0003": ("RP2040", "RP2", 0.75, "RP2040 BOOTSEL mode"),
             "000f": ("RP2350", "RP2", 0.75, "RP2350 BOOTSEL mode"),
             "0005": ("RP2040", "RP2", 0.55, "MicroPython on RP2"),
             "000a": ("RP2040", "RP2", 0.45, "Pico SDK USB serial"),
             "000c": ("unknown target (Raspberry Pi Debug Probe)", "probe", 0.05, "Raspberry Pi Debug Probe"),
             "*": ("RP2040", "RP2", 0.35, "Raspberry Pi USB vendor ID")},
    "0483": {"5740": ("STM32 (family)", "STM32", 0.6, "STM32 native USB virtual COM port"),
             "df11": ("STM32 (family)", "STM32", 0.75, "STM32 DFU bootloader"),
             "*": ("STM32 (family)", "STM32", 0.45, "ST-LINK (on Nucleo/Discovery boards the target is an STM32)")},
    "1366": {"*": ("unknown target (J-Link)", "probe", 0.05, "SEGGER J-Link")},
    "1915": {"*": ("nRF (family)", "nRF", 0.5, "Nordic USB vendor ID")},
    "0d28": {"*": ("unknown target (DAPLink)", "probe", 0.05, "Arm DAPLink / CMSIS-DAP")},
    "10c4": {"ea60": ("ESP32 (family)", "ESP32", 0.2, "CP210x bridge (common on ESP32 DevKit boards)")},
    "1a86": {"55d4": ("ESP32 (family)", "ESP32", 0.2, "CH9102 bridge (common on ESP32 boards)"),
             "7523": ("ESP32 (family)", "ESP32", 0.12, "CH340 bridge (ESP32/ESP8266 boards or Arduino clones)"),
             "8010": ("CH32V (family)", "CH32V", 0.4, "WCH-Link probe")},
    "0403": {"6010": ("ESP32 (family)", "ESP32", 0.15, "FT2232 (ESP-Prog or JTAG adapter)")},
    "2341": {"0043": ("ATmega328P", "AVR", 0.6, "Arduino Uno"), "0001": ("ATmega328P", "AVR", 0.5, "Arduino Uno"),
             "0042": ("ATmega2560", "AVR", 0.6, "Arduino Mega 2560"),
             "0036": ("ATmega32U4", "AVR", 0.6, "Arduino Leonardo bootloader"),
             "8036": ("ATmega32U4", "AVR", 0.6, "Arduino Leonardo"),
             "804d": ("ATSAMD21G18", "SAM", 0.55, "Arduino Zero"),
             "805a": ("nRF52840", "nRF", 0.5, "Arduino Nano 33 BLE"),
             "*": ("Arduino (family)", "AVR", 0.3, "Arduino USB vendor ID")},
    "2a03": {"0043": ("ATmega328P", "AVR", 0.55, "Arduino Uno (arduino.org)")},
    "16c0": {"0483": ("Teensy (i.MX RT1062 or Kinetis)", "i.MX RT", 0.4, "Teensy USB serial")},
    "03eb": {"*": ("Microchip SAM/AVR (family)", "SAM", 0.3, "Atmel/Microchip EDBG or device")},
    "0451": {"bef3": ("TI CC13xx/CC26xx/MSP432/TM4C (LaunchPad)", "TI CC", 0.3, "TI XDS110 debug probe"),
             "f432": ("MSP430 (family)", "MSP", 0.45, "TI eZ-FET (MSP430 LaunchPad)")},
    "1fc9": {"*": ("NXP LPC / i.MX RT / Kinetis (family)", "LPC", 0.3, "NXP USB vendor ID (LPC-Link, MCU-Link)")},
    "04b4": {"f155": ("PSoC (family)", "PSoC", 0.4, "Infineon KitProg3"),
             "f138": ("PSoC (family)", "PSoC", 0.4, "Infineon KitProg")},
    "28e9": {"*": ("GD32 (family)", "GD32", 0.5, "GigaDevice USB vendor ID")},
    "239a": {"*": ("Adafruit board (SAMD/nRF52/RP2040/ESP32)", "Adafruit", 0.2, "Adafruit USB vendor ID")},
}

# DAPLink board IDs: the first 4 characters of the USB serial number.
DAPLINK_BOARDS = {
    "9900": ("nRF51822", "nRF", "BBC micro:bit v1"), "9901": ("nRF51822", "nRF", "BBC micro:bit v1"),
    "9903": ("nRF52833", "nRF", "BBC micro:bit v2"), "9904": ("nRF52833", "nRF", "BBC micro:bit v2"),
    "9905": ("nRF52833", "nRF", "BBC micro:bit v2"), "9906": ("nRF52833", "nRF", "BBC micro:bit v2"),
    "0200": ("MKL25Z128", "Kinetis", "FRDM-KL25Z"), "0240": ("MK64FN1M0", "Kinetis", "FRDM-K64F"),
}

# J-Link OB serial number prefixes on Nordic development kits.
NORDIC_JLINK = {"682": ("nRF52832", "nRF52 DK"), "683": ("nRF52840", "nRF52840 DK"),
                "960": ("nRF9160", "nRF9160 DK"), "1050": ("nRF5340", "nRF5340 DK")}


@dataclass
class Candidate:
    name: str
    family: str
    evidence: list[tuple[float, str]] = field(default_factory=list)

    @property
    def confidence(self) -> float:
        p = 1.0
        for w, _ in self.evidence:
            p *= 1.0 - max(0.0, min(0.99, w))
        return round(1.0 - p, 3)

    @property
    def generic(self) -> bool:
        return "(" in self.name or self.name.startswith("unknown")


def clean_log(text: str) -> str:
    text = ANSI_RE.sub("", text or "")
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _add(cands: dict[str, Candidate], name: str, family: str, weight: float, why: str) -> None:
    c = cands.setdefault(name, Candidate(name, family))
    if all(e[1] != why for e in c.evidence):
        c.evidence.append((weight, why))


def _scan_chip_names(cands: dict[str, Candidate], text: str, weight: float, where: str) -> None:
    for pattern, family, name in CHIP_PATTERNS:
        for m in re.finditer(pattern, text, re.IGNORECASE):
            n = name(m) if callable(name) else name
            if family == "STM32":
                n = n.upper()
            _add(cands, n, family, weight, f"'{m.group(0)}' in the {where}")


def _st_info(cands: dict[str, Candidate], text: str) -> None:
    m = re.search(r"dev-type:\s*(STM32\S+)", text)
    if m:
        name = m.group(1).replace("_", "/")
        _add(cands, name, "STM32", 0.97, f"st-info: dev-type {m.group(1)}")
    m = re.search(r"chipid:\s*(0x[0-9a-fA-F]+)", text)
    if m and not re.search(r"chipid:\s*0x0+\b", text):
        _add(cands, "STM32 (family)", "STM32", 0.6, f"st-info: chipid {m.group(1)}")


def analyze(device: dict[str, Any] | None, log: str = "", probe: str = "") -> dict[str, Any]:
    """Return {mcu, label, confidence, sure, firmware, candidates, evidence}."""
    device = device or {}
    cands: dict[str, Candidate] = {}
    text = clean_log(log)

    for pattern, name, family, weight, why in LOG_RULES:
        for m in re.finditer(pattern, text, re.IGNORECASE | re.MULTILINE):
            n = name(m) if callable(name) else name
            _add(cands, n, family, weight, f"{why}: '{m.group(0).strip()[:80]}'")
    _scan_chip_names(cands, text, 0.7, "log")

    if probe:
        _st_info(cands, probe)
        # The st-info dev-type line is already used above.
        _scan_chip_names(cands, re.sub(r"dev-type:.*", "", probe), 0.85, "debug probe or script output")

    vid, pid = (device.get("vid") or "").lower(), (device.get("pid") or "").lower()
    serial = device.get("serial") or ""
    hints = USB_HINTS.get(vid, {})
    hint = hints.get(pid) or hints.get("*")
    if hint:
        name, family, weight, why = hint
        _add(cands, name, family, weight, f"USB {vid}:{pid}: {why}")
    if vid == "0d28" and serial[:4] in DAPLINK_BOARDS:
        name, family, board = DAPLINK_BOARDS[serial[:4]]
        _add(cands, name, family, 0.7, f"DAPLink board ID {serial[:4]} ({board})")
    if vid == "1366":
        for prefix, (name, board) in NORDIC_JLINK.items():
            if serial.lstrip("0").startswith(prefix):
                _add(cands, name, "nRF", 0.45, f"J-Link serial {serial} looks like a {board}")
    product = " ".join(str(x) for x in (device.get("manufacturer"), device.get("product")) if x)
    if product:
        _scan_chip_names(cands, product, 0.5, "USB product name")

    # Generic "(family)" evidence supports the specific chips of the same family.
    specific = [c for c in cands.values() if not c.generic]
    for c in list(cands.values()):
        if c.generic and c.family not in ("probe",):
            for s in specific:
                if s.family == c.family:
                    for w, why in c.evidence:
                        _add(cands, s.name, s.family, w * 0.5, why)
    ranked = sorted(cands.values(), key=lambda c: (c.confidence, not c.generic), reverse=True)
    # A specific chip beats the family entry it was built from.
    if len(ranked) >= 2 and ranked[0].generic and not ranked[1].generic and ranked[1].family == ranked[0].family \
            and ranked[1].confidence >= ranked[0].confidence - 0.1:
        ranked[0], ranked[1] = ranked[1], ranked[0]
    ranked = [c for c in ranked if not (c.family == "probe" and len(ranked) > 1)]

    firmware = []
    for pattern, name in FIRMWARE_RULES:
        m = re.search(pattern, text)
        if m:
            firmware.append(name(m) if callable(name) else name)

    if not ranked or ranked[0].family == "probe":
        # Only "this is a debug probe": the target MCU is still unknown.
        why = [w for _, w in ranked[0].evidence] if ranked else []
        return {"mcu": None, "label": "unknown", "confidence": 0.0, "sure": False, "firmware": firmware,
                "candidates": [], "evidence": why + (["debug probe: the target is not known (use the probe "
                                                      "reset method or a script that prints the chip)"]
                                                     if ranked else [])}
    best = ranked[0]
    second = next((c for c in ranked[1:] if c.family != best.family or not c.generic), None)
    gap = best.confidence - (second.confidence if second else 0.0)
    sure = best.confidence >= 0.8 and not best.generic and gap >= 0.2
    if sure:
        label = best.name
    elif best.confidence >= 0.4:
        label = f"probably {best.name}"
        if second and second.confidence >= 0.3 and second.family != best.family:
            label += f" (or {second.name})"
    else:
        label = f"maybe {best.name}"
    return {
        "mcu": best.name,
        "family": best.family,
        "label": label,
        "confidence": best.confidence,
        "sure": sure,
        "firmware": firmware,
        "candidates": [{"name": c.name, "family": c.family, "confidence": c.confidence} for c in ranked[:5]],
        "evidence": [why for _, why in sorted(best.evidence, reverse=True)],
    }


# Built-in identify profiles. A profile sets the reset method, the baud rate and
# the scripts. The scripts run with bash on the PC that has the serial port, with
# these variables: SM_TTY (first port), SM_TTYS, SM_BAUD, SM_BUSID, SM_VID,
# SM_PID, SM_SERIAL, SM_RESET, SM_PHASE (start / after_reset), SM_SETUP.
# The output of the scripts is also used to find the MCU.
BUILTIN_PROFILES: dict[str, dict[str, Any]] = {
    "Auto": {"reset": "auto", "baud": "auto", "duration": 4, "script": "", "after_script": "",
             "help": "Reset method from the USB IDs, try baud rates until the text is readable."},
    "ESP32 / ESP8266": {"reset": "rts", "baud": "auto", "duration": 3, "script": "", "after_script": "",
                        "help": "EN reset through RTS, like esptool. Tries 115200 then 74880 (ESP8266 ROM)."},
    "Arduino (DTR)": {"reset": "dtr", "baud": 115200, "duration": 4, "script": "", "after_script": "",
                      "help": "DTR reset. Change the baud rate to the one of your sketch."},
    "MicroPython / CircuitPython": {
        "reset": "repl", "baud": 115200, "duration": 3, "script": "",
        "after_script": "sleep 1.5\nprintf 'import sys, os; print(sys.implementation, os.uname())\\r' > \"$SM_TTY\"\n",
        "help": "Soft reset over the REPL, then print the platform."},
    "Zephyr shell": {
        "reset": "auto", "baud": 115200, "duration": 4, "script": "",
        "after_script": "sleep 2\nprintf '\\rkernel version\\r' > \"$SM_TTY\"\n",
        "help": "After the boot, ask the Zephyr shell for the kernel version."},
    "U-Boot / Linux console": {
        "reset": "auto", "baud": 115200, "duration": 8, "script": "",
        "after_script": "sleep 5\nprintf '\\r' > \"$SM_TTY\"\n",
        "help": "Longer capture, and press Enter to get a prompt."},
    "ST-LINK (st-flash)": {"reset": "probe", "baud": 115200, "duration": 3, "script": "", "after_script": "",
                           "help": "Reset the STM32 through the ST-LINK and read the chip type with st-info."},
    "nRF (nrfjprog)": {
        "reset": "none", "baud": 115200, "duration": 3,
        "script": "nrfjprog --deviceversion ${SM_SERIAL:+-s $SM_SERIAL} || true\n"
                  "nrfjprog --reset ${SM_SERIAL:+-s $SM_SERIAL}\n",
        "after_script": "",
        "help": "Read the chip version and reset with nrfjprog (Nordic command line tools)."},
}
