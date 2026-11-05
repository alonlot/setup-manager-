# Changelog

## 0.5.0

### Before you update

- Update with `./update-lab.sh` (or tell an AI agent "apply the new fix", see `CLAUDE.md`). It updates
  this PC and every setup PC.
- Every setup PC gets a **new token**. The new tokens are saved in your config.
- **USB/IP mode "open" is the new default.** The agent does not touch port 3240: `usbip bind` and
  `usbip attach` by hand work as without Setup Manager. Any PC that can reach port 3240 can attach a
  shared device without the token. For the old protection, deploy with `--usbip-filter` or
  `--tunnel-only`. The update keeps a mode that you choose.
- USB/IP of the manager is **plain** (not encrypted) by default. The token still goes only over TLS.
  Turn on **Preferences > Encrypt USB/IP** for the encrypted tunnel.
- Run `./install-app.sh` once in a terminal after the update: it writes the new sudo rule for usbip.
- A firewall between your PC and the setup PCs must let 3240/tcp through (or turn on Encrypt USB/IP).

### New

- USB/IP modes of the agent: `open` (default), `manual` (`--usbip-filter`: a device that Setup Manager
  shares needs the token, a device shared by hand works for everybody) and `tunnel` (`--tunnel-only`).
- Plain USB/IP for the manager: it asks the agent with the token over TLS (`POST /api/usb/allow`).
- The device list says who shared a device (`shared_by`): Setup Manager or "by hand".
- `./update-lab.sh --yes` runs to the end without a terminal (AI agents, scripts). The sudo password of
  the setup PCs can come from `SETUP_MANAGER_SUDO_PASSWORD`. `CLAUDE.md` has the steps for AI agents.
- `install-app.sh --no-sudo`: only the app, no step that needs sudo.
- `setup-manager deploy --usbip-filter / --tunnel-only / --open-usbip`.
- The SSH port of `deploy --ssh-port` is saved per setup and used by the rollout.

### Fixed

- Ubuntu: `usbip bind` failed after a deploy or a kernel update ("usbip not found for kernel"). The
  agent and the app use the real usbip of `/usr/lib/linux-tools*/`, and the installers install the
  tools of the running kernel.
- Security: a setup with transport "tls" and no fingerprint (after `trust --forget`) was never verified
  and never pinned again.
- The agent dropped the `SM_*` variables and the working folder of flash and identify scripts when it
  runs commands as another user (the normal installation).
- The agent became unreachable after a short `accept()` error; a serial port could read the pipes of a
  flash tool; identify changed the baud rate of an open serial monitor; several 500 errors on bad input.
- A failed `usbip port` run erased the records of encrypted attachments ("in use by this PC").
- Rollout: `--only` removed the shared token of the other setups; "keep attached" was turned off.
- Lab library: a lost reservation locked the job out of its own board; `reset()` changed the baud
  rate of an open console; the pytest fixture leaked reservations and missed the boot banner after a
  flash; split UTF-8 characters in serial output.
- App: the serial monitor opened the wrong port after a refresh; closed terminal tabs stayed alive; the
  identify dialog forgot its profile; "Run on many" ignored the user.
- CLI: errors are one line and an exit code, not a traceback; `exec SETUP --timeout N` works.
- A manual `usbip attach` no longer hangs for two minutes when the port is blocked (it is refused).

## 0.4.0

- TLS with pinned certificates for the agent API, and an encrypted USB/IP tunnel.
- `update-lab.sh` and `setup-manager rollout`.
