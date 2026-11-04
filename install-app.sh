#!/usr/bin/env bash
# Install the Setup Manager desktop app for the current user (Ubuntu 24.04 or newer).
#
#   ./install-app.sh            install or upgrade
#   ./install-app.sh --no-apt   do not install system packages
#   ./install-app.sh --uninstall
#
# It installs: system packages (GTK 4, libadwaita, VTE, usbip client), a Python
# venv in ~/.local/share/setup-manager/venv, the command ~/.local/bin/setup-manager,
# a launcher in the app menu, and a sudo rule so that the app can run usbip.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ME="$(id -un)"
APP_ID=io.github.alonlot.SetupManager
DATA="${XDG_DATA_HOME:-$HOME/.local/share}"
VENV="$DATA/setup-manager/venv"
BIN="$HOME/.local/bin"
APT=1

for arg in "$@"; do
  case "$arg" in
    --no-apt) APT=0;;
    --uninstall)
      rm -rf "$DATA/setup-manager/venv" "$BIN/setup-manager" "$DATA/applications/$APP_ID.desktop" \
             "$DATA/icons/hicolor/scalable/apps/$APP_ID.svg"
      sudo rm -f /etc/sudoers.d/setup-manager
      echo "Setup Manager removed (your config in ~/.config/setup-manager is kept)."
      exit 0;;
    -h|--help) sed -n '2,10p' "$0"; exit 0;;
    *) echo "unknown option: $arg" >&2; exit 1;;
  esac
done

if [[ $EUID -eq 0 ]]; then
  echo "Run this as your normal user (not with sudo). It asks for sudo when it needs it." >&2
  exit 1
fi

if [[ $APT -eq 1 ]]; then
  echo ">> installing system packages (sudo)"
  pkgs=(python3-venv python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 gir1.2-vte-3.91 hwdata)
  if grep -qi ubuntu /etc/os-release; then
    # /usr/bin/usbip is a wrapper that needs the tools of the running kernel.
    if apt-cache show "linux-tools-$(uname -r)" >/dev/null 2>&1; then
      pkgs+=("linux-tools-$(uname -r)")
    elif REL="$(. /etc/os-release; echo "${VERSION_ID:-}")" && \
         dpkg-query -W -f='${Status}' "linux-generic-hwe-$REL" 2>/dev/null | grep -q "ok installed"; then
      pkgs+=("linux-tools-generic-hwe-$REL")
    else
      pkgs+=(linux-tools-generic)
    fi
  else
    pkgs+=(usbip)
  fi
  sudo apt-get install -y "${pkgs[@]}"
fi

# The venv must use the system Python that has the GTK bindings (python3-gi).
PY=""
for cand in /usr/bin/python3 /usr/bin/python3.13 /usr/bin/python3.12 /usr/bin/python3.11 /usr/bin/python3.10; do
  if [[ -x "$cand" ]] && "$cand" -c "import gi; gi.require_version('Gtk','4.0'); gi.require_version('Adw','1'); gi.require_version('Vte','3.91'); from gi.repository import Gtk, Adw, Vte" 2>/dev/null; then
    PY="$cand"; break
  fi
done
if [[ -z "$PY" ]]; then
  echo "!! no system Python can load GTK 4 + libadwaita + VTE. Install:" >&2
  echo "   sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 gir1.2-vte-3.91" >&2
  echo "   (libadwaita 1.5 or newer is needed: Ubuntu 24.04 or newer)" >&2
  exit 1
fi
"$PY" - <<'PY'
import gi, sys
gi.require_version("Adw", "1")
from gi.repository import Adw
if (Adw.get_major_version(), Adw.get_minor_version()) < (1, 5):
    sys.exit("!! libadwaita %d.%d is too old, 1.5 or newer is needed (Ubuntu 24.04+)"
             % (Adw.get_major_version(), Adw.get_minor_version()))
PY

echo ">> creating the venv with $PY"
mkdir -p "$(dirname "$VENV")" "$BIN"
"$PY" -m venv --system-site-packages "$VENV"
"$VENV/bin/pip" install -q --upgrade pip
"$VENV/bin/pip" install -q --upgrade "$HERE"
ln -sf "$VENV/bin/setup-manager" "$BIN/setup-manager"

echo ">> installing the launcher and the icon"
install -Dm644 "$HERE/setup_manager/app/data/$APP_ID.svg" "$DATA/icons/hicolor/scalable/apps/$APP_ID.svg"
mkdir -p "$DATA/applications"
sed "s|@EXEC@|$BIN/setup-manager|" "$HERE/setup_manager/app/data/$APP_ID.desktop.in" > "$DATA/applications/$APP_ID.desktop"
command -v update-desktop-database >/dev/null && update-desktop-database "$DATA/applications" || true
command -v gtk-update-icon-cache >/dev/null && gtk-update-icon-cache -q -t "$DATA/icons/hicolor" || true

# Ubuntu: /usr/bin/usbip is a wrapper that fails ("usbip not found for kernel ...") without
# linux-tools-$(uname -r); the app then runs the real binary in /usr/lib/linux-tools*/.
USBIP="$(command -v usbip || true)"
REAL=""
for f in "/usr/lib/linux-tools/$(uname -r)/usbip" /usr/lib/linux-tools/*/usbip /usr/lib/linux-tools-*/usbip; do
  if [[ -x "$f" ]]; then REAL="$f"; break; fi
done
if [[ -n "$USBIP" ]] && ! usbip version >/dev/null 2>&1; then
  if [[ -n "$REAL" ]]; then
    echo ">> 'usbip' does not run for kernel $(uname -r), the app uses $REAL"
  else
    echo "!! usbip does not work for this kernel: sudo apt install linux-tools-$(uname -r)"
  fi
fi
MODPROBE="$(command -v modprobe || echo /usr/sbin/modprobe)"
if [[ -n "$USBIP" || -n "$REAL" ]]; then
  echo ">> allowing '$ME' to run usbip and modprobe without a password (sudo)"
  CMDS="/usr/lib/linux-tools/*/usbip, /usr/lib/linux-tools-*/usbip"
  [[ -n "$USBIP" ]] && CMDS="$USBIP, $CMDS"
  RULE="$ME ALL=(root) NOPASSWD: $CMDS, $MODPROBE vhci-hcd"
  echo "$RULE" | sudo tee /etc/sudoers.d/setup-manager >/dev/null
  sudo chmod 440 /etc/sudoers.d/setup-manager
  sudo visudo -cf /etc/sudoers.d/setup-manager >/dev/null || { sudo rm -f /etc/sudoers.d/setup-manager; echo "!! sudo rule rejected"; }
  echo vhci-hcd | sudo tee /etc/modules-load.d/setup-manager.conf >/dev/null
  sudo modprobe vhci-hcd || echo "!! could not load vhci-hcd now"
else
  echo "!! usbip not found. Attach will not work until you install it (Ubuntu: sudo apt install linux-tools-$(uname -r))."
fi

if getent group dialout >/dev/null && ! id -nG "$ME" | grep -qw dialout; then
  echo ">> adding $ME to the dialout group (serial ports of attached devices; log out and in once)"
  sudo usermod -aG dialout "$ME"
fi

case ":$PATH:" in *":$BIN:"*) ;; *) echo "!! add $BIN to your PATH to use the setup-manager command";; esac
echo
echo "Done. Open 'Setup Manager' from the app menu, or run: setup-manager"
