# Setup Manager

Setup Manager is an Ubuntu desktop app that controls a group of Linux "setup
PCs" that you use for embedded development. Each setup PC shares its USB
devices (debug probes, serial adapters, boards) over USB/IP. With Setup
Manager, you can:

- See the status of all setup PCs (online, CPU, memory, disk, IPs, usbipd).
- See the USB devices of each PC, with their tty names and USB/IP state.
- Attach a device to your PC with one click (usbip bind + usbip attach).
- Keep a device attached. The app attaches it again after a board reset.
- Open real terminal tabs on a setup PC, or run one command on many PCs.
- Reset a board, read its boot log and see which MCU it probably is, next to the device.
- Let AI agents do all of this through an MCP server.

![USB devices](docs/usb.png)

## Parts

```
 your Ubuntu PC                                   setup PC (one agent on each)
 +-----------------------------+   HTTP + WS     +-------------------------------+
 | Setup Manager app (GTK 4)   | --------------> | setup_agent.py  (systemd)     |
 | setup-manager mcp    (MCP)  |   token auth    |   status, USB list, bind,     |
 | setup-manager <cli>         |   port 8765     |   exec, PTY shell             |
 |                             |                 |                               |
 | usbip attach / detach       | <-------------- | usbipd (USB/IP server)        |
 +-----------------------------+   USB/IP 3240   +-------------------------------+
```

| Part | Runs on | What it is |
|---|---|---|
| Agent | each setup PC | `setup_manager/agent/setup_agent.py`. One file. Python 3.8+ standard library only. Runs as root (USB bind needs root) from systemd and starts at boot. |
| App | your Ubuntu PC | A native GTK 4 + libadwaita app. The terminal tabs use VTE (the terminal of GNOME Terminal). The local `usbip attach` runs here. |
| CLI and MCP | your Ubuntu PC | The `setup-manager` command. Same logic as the app. |

The app, the CLI and the MCP server use the same config file:
`~/.config/setup-manager/config.json`.

## 1. Install the app (your Ubuntu PC)

Requirements: Ubuntu 24.04 or newer (libadwaita 1.5 or newer).

```bash
git clone https://github.com/alonlot/setup-manager- setup-manager
cd setup-manager
./install-app.sh
```

Run it as your normal user (not with sudo). It asks for your sudo password. The script:

- Installs the system packages: GTK 4, libadwaita, VTE, PyGObject, and the usbip client (`linux-tools-generic`).
- Makes a Python venv in `~/.local/share/setup-manager/venv` that uses the system GTK bindings.
- Installs the command `~/.local/bin/setup-manager`.
- Adds **Setup Manager** to the app menu (with an icon).
- Adds a sudo rule (`/etc/sudoers.d/setup-manager`). The rule lets you run `usbip` and `modprobe vhci-hcd` without a password, because `usbip attach` needs root.
- Loads the `vhci-hcd` kernel module now and at boot.

Run `./install-app.sh` again to upgrade. Use `./install-app.sh --uninstall` to remove it (your config is kept).

## 2. Install the agent (each setup PC)

### From your PC, over SSH (recommended)

```bash
setup-manager deploy dev@192.168.1.20 --name bench-1 --install-deps
```

This copies the agent to the PC, runs the installer with sudo, and adds the
setup to the app with its token. To use one token for all setups, set the
default agent token in **Preferences** first, or give `--token`.
Repeat for each setup PC.

### By hand

Copy the folder `setup_manager/agent/` to the setup PC (`setup-manager agent-files` prints its path), then:

```bash
sudo ./install.sh --name bench-1 --install-deps
```

The installer:

- Copies the agent to `/opt/setup-agent/` and makes `/etc/setup-agent/config.json`.
- Makes a random token if you do not give one, and prints it as `CONFIG_TOKEN=...`.
- Installs and starts two systemd services: `setup-agent` and `setup-usbipd`. They start at boot.
- Loads `usbip-host` at boot.
- Sets the shell user to the user that ran sudo (`run_as`). Shells and commands run as this user, not root.
- Opens the ports in ufw when ufw is active (8765/tcp, 8765/udp, 3240/tcp).

Options: `--token`, `--port`, `--run-as USER`, `--auto-bind RULE`, `--protect RULE`, `--tls`, `--install-deps`.
Run `install.sh` again to upgrade. It keeps the existing config. To remove: `sudo ./uninstall.sh [--purge]`.

Check the agent on the setup PC: `systemctl status setup-agent` and `journalctl -u setup-agent -f`.

## 3. Use the app

Open **Setup Manager** from the app menu, or run `setup-manager`.
Add setups with **+** (host and token) or with **Scan network** in the main menu
(UDP broadcast, same subnet only).

| Place | What you can do |
|---|---|
| Overview | All setups with CPU, RAM and USB counts. The devices attached to your PC, with Detach. The keep-attached list. |
| Setup > USB | One row per device: state, name, vid:pid, serial, tty names, drivers. **Attach** shares the device and attaches it to your PC. **Detach** detaches it and stops sharing. The **⋮** menu: share only, stop sharing, detach and keep shared, keep attached, auto-share when plugged, USB reset, copy bus ID, copy the usbip command. |
| Setup > Terminal | Real terminal tabs (VTE) on the setup PC. **+** opens a new tab. A tab stays open when you go to other pages. Ctrl+Shift+C / Ctrl+Shift+V copy and paste. Ctrl +/- zooms. |
| Setup > Run | Run one command with a timeout and an optional user. Quick commands (right-click a quick command to remove it). |
| Setup > Info / Log | System details, and the recent log lines of the agent. |
| Find device | Find a device on all setups by name, vid:pid, serial or tty. |
| Run on many | Run one command on many setups at the same time. |
| Preferences (Ctrl+,) | Default agent token and port, refresh interval, the local usbip command and sudo. |

Keyboard: Ctrl+N add setup, Ctrl+R or F5 refresh, Ctrl+Q quit.

![Overview](docs/overview.png)

Device states:

| State | Meaning |
|---|---|
| Not shared | The setup PC uses the device. |
| Shared | Bound to usbip-host. No client has it. |
| Attached here | Attached to this PC. |
| In use elsewhere | Another client has it. **Take over** takes it (asks first). |

### Keep attached and auto-share

Debug probes and boards often re-enumerate (after flashing, after a reset).
Then the USB/IP connection drops.

- **Keep attached** (your PC): every 4 seconds, the app checks the device and attaches it again when it is missing. It follows the device by serial number when it moves to a different USB port. This works while the app is open. To keep it without the app, run `setup-manager daemon` (for example as a systemd user service). Do not run the app and the daemon at the same time. A manual Detach turns keep-attached off.
- **Auto-share when plugged** (setup PC): the agent binds matching devices by itself when they are plugged in. The rules are in the agent config (`auto_bind`).

Use the two together for a hands-off setup.

### Safety rules in the agent

- The agent does not bind a device that has a network interface (a USB Ethernet adapter can be the only link to the PC), a hub, or a device in `protected`. The app asks before it forces this.
- Rule format for `auto_bind` and `protected`: `1-1.2` (busid), `0483:374b` (vid:pid), `0483:*`, `0483:374b:SERIAL`.

![Terminal](docs/terminal.png)

## Identify the MCU

Setup Manager can find which MCU is on a board. Open the **⋮** menu of a
device and select **Identify MCU…**, then click **Reset and read**. The app:

1. Opens the serial port(s) of the device and starts to record.
2. Runs your **start script**, if you have one.
3. Resets the MCU.
4. Runs your **after-reset script**, if you have one, while it records.
5. Records for the capture time (4 seconds by default). With baud rate "auto", it tries
   115200, 74880, 9600 and 57600 until the text is readable.
6. Compares the log, the USB IDs, the debug probe output and the script output with known signatures.

The result shows next to the device:

| Label | Meaning |
|---|---|
| `ESP32-S3` (green) | Sure. A strong signature, clearly ahead of the other candidates. |
| `STM32?` (amber) | Probably. Hover over the label to see the evidence. |
| `ESP32?` (grey) | A weak guess, for example only from a USB bridge chip. |

Click the label to see the full result and the log, or to run it again.

![Identify MCU](docs/identify.png)

### Reset methods

| Method | What it does | For |
|---|---|---|
| `auto` | Selects a method from the USB IDs | Start with this |
| `rts` | Pulses RTS (the EN pin), like esptool | ESP32, ESP8266, ESP32 native USB |
| `dtr` | Pulses DTR | Arduino Uno, Mega, Nano |
| `rts+dtr` | RTS, then DTR | Unknown CH340/CP210x/FTDI boards |
| `repl` | Sends Ctrl-C, Ctrl-D (soft reset) | MicroPython, CircuitPython |
| `probe` | `st-flash reset` or `pyocd reset`, and reads the chip type with `st-info` | ST-LINK, CMSIS-DAP, DAPLink |
| `usb` | USB port reset of the device | Native USB devices |
| `none` | No reset. Your scripts do it. | J-Link, nrfjprog, relays, special boards |

### It recognizes

Espressif ROM and ESP-IDF logs (all ESP32 types, ESP8266), RP2040/RP2350 and
MicroPython/CircuitPython banners, Zephyr and nRF Connect SDK boot lines and
board names, U-Boot and Linux SoC lines, `st-info` output, `nrfjprog`
output, chip names in any text (STM32, nRF, ATmega, SAMD, GD32, CH32V, LPC,
i.MX RT, EFR32, CC13xx/CC26xx, MSP430, PSoC, Kinetis and more), and the USB
IDs of common boards and probes. It also shows the firmware when it finds it
(ESP-IDF, Zephyr, MicroPython, U-Boot, Linux, FreeRTOS, Arduino and more).
To add a signature, edit `setup_manager/mcu_detect.py`. The agents do not need an update for this.

### Scripts: make the right logs appear

Some boards print nothing at boot, or the MCU name is only in a command
output. Use the scripts to fix this:

- **Start script**: runs before the reset. For example: put the board in a mode, power it on with a relay, or read the chip with a tool.
- **After-reset script**: runs after the reset, while the app records. For example: wait for the prompt and send a command.

The scripts run with bash on the PC that has the serial port, with these variables:

| Variable | Value |
|---|---|
| `SM_TTY` | The first serial port, for example `/dev/ttyACM0` |
| `SM_TTYS` | All serial ports of the device |
| `SM_BAUD` | The baud rate of this try |
| `SM_BUSID`, `SM_VID`, `SM_PID`, `SM_SERIAL` | The USB device |
| `SM_PHASE` | `start` or `after_reset` |
| `SM_SETUP` | The name of the setup |

The output of the scripts also counts for the guess. A script that prints
`NRF52840_xxAA` (for example `nrfjprog --deviceversion`) is enough.
Examples:

```bash
# after reset: ask a Zephyr shell
sleep 2; printf '\rkernel version\r' > "$SM_TTY"

# after reset: ask your own firmware
sleep 1; printf 'version\r' > "$SM_TTY"

# start: J-Link without a serial port, print the core and reset
printf 'connect\nr\ng\nexit\n' > /tmp/jl.txt
JLinkExe -USB "$SM_SERIAL" -if SWD -speed 4000 -device CORTEX-M4 -autoconnect 1 -CommandFile /tmp/jl.txt
```

Do not read from `$SM_TTY` in a script: the app reads the port, and a script that also reads it takes the bytes.

### Profiles

A profile stores the reset method, the baud rate, the capture time and the
two scripts. There are built-in profiles (Auto, ESP32 / ESP8266, Arduino,
MicroPython, Zephyr shell, U-Boot / Linux, ST-LINK, nRF with nrfjprog). Click
**Save as profile…** to save your own. The app remembers the last options for
each device.

### Where it runs

| Device state | What happens |
|---|---|
| Not shared | The agent on the setup PC does it. |
| Attached here | This PC does it, with the local serial port. |
| Shared, no client | The app stops sharing, does it on the setup PC, then shares it again. |
| In use elsewhere | Refused. Identify it on the other PC, or take it over. |

The installers add the shell user to the `dialout` group, so that the scripts can open the serial ports.

### Limits

- The result is a guess from text. A board that prints nothing and has a generic USB bridge gives only a weak guess. Then use a script.
- `rts` and `dtr` reset only work when the board connects these lines to reset (most ESP32 and Arduino boards do).
- `probe` needs `st-flash`/`st-info` (package `stlink-tools`) or `pyocd` on the PC that runs it.

## 4. Connect AI agents (MCP)

The MCP server uses the same config and the same logic as the app. Devices
attach to the PC where the MCP server runs.

Claude Code:

```bash
claude mcp add setup-manager -- ~/.local/bin/setup-manager mcp
```

Claude Desktop, Cursor and other clients (stdio):

```json
{
  "mcpServers": {
    "setup-manager": {
      "command": "/home/you/.local/bin/setup-manager",
      "args": ["mcp"]
    }
  }
}
```

For clients that use HTTP: `setup-manager mcp --http --port 8801` (streamable HTTP at `http://127.0.0.1:8801/mcp`). It has no auth. Keep it on 127.0.0.1.

Tools:

| Tool | What it does |
|---|---|
| `list_setups` | All setups with online state and USB counts |
| `setup_status` | Full status of one setup |
| `list_usb` | USB devices of a setup and their state |
| `find_usb` | Search devices on all setups (`"stlink"`, `"0483:374b"`, `"ttyACM"`) |
| `attach_usb` / `detach_usb` | Bind + attach here / detach + unbind |
| `bind_usb` / `unbind_usb` / `reset_usb` | Single actions on the setup PC |
| `keep_attached` | Turn keep-attached on or off |
| `list_attached` | Devices attached to this PC |
| `run_command` / `run_command_many` | Shell commands on one or many setups |
| `identify_mcu` | Reset the MCU, read its log and guess the MCU (with profiles and scripts) |
| `identify_profiles` / `save_identify_profile` | List and save identify profiles |
| `agent_log` | Recent agent log lines |

Example requests to an agent: *"Find the ST-Link on the lab setups, attach it,
flash build/fw.elf with openocd, then detach it."* or *"Identify the MCU on
every USB serial device of bench-2."*

## CLI

```bash
setup-manager                            # open the app
setup-manager list                       # setups and state
setup-manager usb bench-1                # USB devices
setup-manager attach bench-1 1-1.2       # bind + attach here
setup-manager detach bench-1 1-1.2       # detach + unbind (--keep-bound to keep it shared)
setup-manager attached                   # devices attached to this PC
setup-manager shell bench-1              # interactive shell in this terminal (like ssh)
setup-manager exec bench-1 'dmesg | tail -n 20'
setup-manager exec all -- uptime         # on every setup
setup-manager identify bench-1 1-2       # reset, read the log, guess the MCU
setup-manager identify bench-1 1-2 --profile "Zephyr shell" --log
setup-manager identify bench-1 1-2 --reset none --script start.sh --after-script ask.sh
setup-manager identify --list-profiles
setup-manager discover                   # find agents with UDP broadcast
setup-manager add 192.168.1.21 --name bench-2 --token ...
setup-manager daemon                     # keep-attached without the app
setup-manager --json usb bench-1         # JSON output for scripts
```

A setup can be given by id, name or host.

## Configuration

App (`~/.config/setup-manager/config.json`). You can change most values in the app. All keys are optional:

```json
{
  "agent_defaults": { "port": 8765, "token": "one-token-for-all-agents" },
  "local_usbip": { "command": null, "sudo": null },
  "poll_interval": 5,
  "discovery_targets": ["255.255.255.255"],
  "setups": [
    { "id": "bench-1", "name": "bench-1", "host": "192.168.1.20", "port": 8765, "token": "...",
      "tls": false, "verify_tls": false, "usbip_port": 3240, "notes": "STM32H7 board" }
  ]
}
```

Agent (`/etc/setup-agent/config.json`): see `setup_manager/agent/config.example.json`.
Important keys: `token`, `port`, `run_as`, `auto_bind`, `protected`, `tls_cert`/`tls_key`, `discovery`.
Restart after a change: `sudo systemctl restart setup-agent`.

## Security

Read this before you use it on a shared network.

- The agent gives a **root-level** API (USB control and shell) to everyone who has its token. Keep the token secret. Each agent can have its own token. The config file of the app has mode 600.
- Without TLS, the token and the shell traffic go over the network as plain text. Use `--tls` on the agent (self-signed certificate) and turn on TLS for the setup in the app. Or use the agents only on a lab network that you trust.
- USB/IP (port 3240) has no authentication and no encryption. This is a limit of USB/IP. Anyone who can reach port 3240 can attach a shared device. Share devices only on a trusted network, or limit port 3240 with a firewall to your PC's IP.
- The MCP server can do everything the app can do. Give it only to agents that you trust with your lab.

## Troubleshooting

| Problem | What to do |
|---|---|
| The app does not start: "cannot start the desktop app" | Run `./install-app.sh` again. It installs GTK 4, libadwaita and VTE for the system Python. |
| Setup is offline | On the setup PC: `systemctl status setup-agent`. Check the firewall (8765/tcp). Check host and port in the app. |
| "agent rejected the token" | The token in the app does not match `/etc/setup-agent/config.json`. |
| "usbipd is not running" | Click **Start usbipd** in the banner, or `sudo systemctl restart setup-usbipd` on the setup PC. |
| usbip not found on the setup PC | Ubuntu: `sudo apt install linux-tools-generic linux-tools-$(uname -r)`. Debian: `sudo apt install usbip`. |
| Attach fails with "password is required" | Run `./install-app.sh` again (it adds the sudo rule). |
| Attach fails with "open vhci_driver" | `sudo modprobe vhci-hcd` on your PC. |
| Attach times out | Port 3240/tcp on the setup PC is blocked. |
| Scan network finds nothing | Discovery uses UDP broadcast and works only in one subnet. Add the setups by IP. |

## Development

```bash
python3 -m venv --system-site-packages .venv   # system Python, for the GTK bindings
.venv/bin/pip install -e '.[test]'
.venv/bin/pytest
```

The tests run a real agent process against a fake sysfs tree with fake `usbip`
programs (`tests/fakes/`). They test the agent API, the manager logic, the
shell client in a real pseudo-terminal and the MCP server, without USB
hardware or root. The app smoke test runs only when a display is available.
The MCP server works with the `mcp` package 1.x and 2.x.
