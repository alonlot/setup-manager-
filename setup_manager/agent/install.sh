#!/usr/bin/env bash
# Install (or upgrade) the Setup Manager agent on this Setup PC.
#
#   sudo ./install.sh [--name NAME] [--token TOKEN] [--port 8765] [--run-as USER]
#                     [--auto-bind RULE]... [--protect RULE]... [--tls] [--install-deps]
#
# An existing /etc/setup-agent/config.json is kept. Options given on the
# command line replace the values in it.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PREFIX=/opt/setup-agent
CONF_DIR=/etc/setup-agent
CONF="$CONF_DIR/config.json"

NAME="" TOKEN="" PORT="" RUN_AS="${SUDO_USER:-}" TLS=0 DEPS=0
AUTO_BIND=() PROTECT=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --name) NAME="$2"; shift 2;;
    --token) TOKEN="$2"; shift 2;;
    --port) PORT="$2"; shift 2;;
    --run-as) RUN_AS="$2"; shift 2;;
    --auto-bind) AUTO_BIND+=("$2"); shift 2;;
    --protect) PROTECT+=("$2"); shift 2;;
    --tls) TLS=1; shift;;
    --install-deps) DEPS=1; shift;;
    -h|--help) sed -n '2,9p' "$0"; exit 0;;
    *) echo "unknown option: $1" >&2; exit 1;;
  esac
done

if [[ $EUID -ne 0 ]]; then echo "run as root (sudo $0 ...)" >&2; exit 1; fi
command -v python3 >/dev/null || { echo "python3 is required" >&2; exit 1; }
[[ "$RUN_AS" == "root" ]] && RUN_AS=""

if [[ $DEPS -eq 1 ]] && ! command -v usbip >/dev/null; then
  echo ">> installing usbip"
  if command -v apt-get >/dev/null; then
    if grep -qi ubuntu /etc/os-release; then
      apt-get install -y "linux-tools-$(uname -r)" linux-tools-generic hwdata || apt-get install -y linux-tools-generic hwdata
    else
      apt-get install -y usbip hwdata
    fi
  elif command -v dnf >/dev/null; then dnf install -y usbip hwdata
  elif command -v pacman >/dev/null; then pacman -S --noconfirm usbip hwdata
  elif command -v zypper >/dev/null; then zypper install -y usbip
  else echo "!! unknown package manager, install usbip yourself"; fi
fi

echo ">> installing agent to $PREFIX"
install -d -m 755 "$PREFIX"
install -m 755 "$HERE/setup_agent.py" "$PREFIX/setup_agent.py"
install -d -m 700 "$CONF_DIR"

# Modules at boot.
printf 'usbip-core\nusbip-host\n' > /etc/modules-load.d/setup-agent.conf
modprobe usbip-host 2>/dev/null || echo "!! could not load usbip-host now (is usbip installed for this kernel?)"

if [[ $TLS -eq 1 && ! -f "$CONF_DIR/cert.pem" ]]; then
  echo ">> creating a self-signed TLS certificate"
  openssl req -x509 -newkey rsa:2048 -nodes -days 3650 -subj "/CN=$(hostname)" \
    -keyout "$CONF_DIR/key.pem" -out "$CONF_DIR/cert.pem" 2>/dev/null
  chmod 600 "$CONF_DIR/key.pem"
fi

AUTO_JSON="$(printf '%s\n' "${AUTO_BIND[@]+"${AUTO_BIND[@]}"}")" \
PROTECT_JSON="$(printf '%s\n' "${PROTECT[@]+"${PROTECT[@]}"}")" \
NAME="$NAME" TOKEN="$TOKEN" PORT="$PORT" RUN_AS="$RUN_AS" TLS="$TLS" CONF="$CONF" CONF_DIR="$CONF_DIR" \
python3 - <<'PY'
import json, os, secrets, socket
conf = os.environ["CONF"]
cfg = {}
if os.path.exists(conf):
    with open(conf) as f:
        cfg = json.load(f)
env = os.environ
if env["NAME"]: cfg["name"] = env["NAME"]
cfg.setdefault("name", socket.gethostname())
if env["TOKEN"]: cfg["token"] = env["TOKEN"]
if not cfg.get("token"): cfg["token"] = secrets.token_urlsafe(32)
if env["PORT"]: cfg["port"] = int(env["PORT"])
cfg.setdefault("port", 8765)
if env["RUN_AS"]: cfg["run_as"] = env["RUN_AS"]
cfg.setdefault("run_as", "")
auto = [x for x in env["AUTO_JSON"].splitlines() if x.strip()]
prot = [x for x in env["PROTECT_JSON"].splitlines() if x.strip()]
cfg["auto_bind"] = sorted(set(cfg.get("auto_bind", [])) | set(auto))
cfg["protected"] = sorted(set(cfg.get("protected", [])) | set(prot))
if env["TLS"] == "1":
    cfg["tls_cert"] = os.path.join(env["CONF_DIR"], "cert.pem")
    cfg["tls_key"] = os.path.join(env["CONF_DIR"], "key.pem")
tmp = conf + ".tmp"
with open(tmp, "w") as f:
    json.dump(cfg, f, indent=2)
os.chmod(tmp, 0o600)
os.replace(tmp, conf)
print("CONFIG_NAME=" + cfg["name"])
print("CONFIG_PORT=%d" % cfg["port"])
print("CONFIG_TOKEN=" + cfg["token"])
PY

echo ">> installing systemd units"
install -m 644 "$HERE/setup-agent.service" /etc/systemd/system/setup-agent.service
install -m 644 "$HERE/setup-usbipd.service" /etc/systemd/system/setup-usbipd.service
systemctl daemon-reload
if command -v usbipd >/dev/null; then
  # Do not run two usbipd daemons when the distro unit is already active.
  if ! systemctl is-active --quiet usbipd.service 2>/dev/null; then
    systemctl enable --now setup-usbipd.service
  fi
else
  echo "!! usbipd not found. Install usbip (or use --install-deps), then: systemctl enable --now setup-usbipd"
fi
systemctl enable setup-agent.service
systemctl restart setup-agent.service

if command -v ufw >/dev/null && ufw status 2>/dev/null | grep -q "Status: active"; then
  PORT_NOW="$(python3 -c "import json;print(json.load(open('$CONF')).get('port',8765))")"
  ufw allow "$PORT_NOW/tcp" >/dev/null; ufw allow "$PORT_NOW/udp" >/dev/null; ufw allow 3240/tcp >/dev/null
  echo ">> opened ufw ports $PORT_NOW/tcp, $PORT_NOW/udp, 3240/tcp"
fi

sleep 1
systemctl --no-pager --lines=0 status setup-agent.service || true
echo
echo "Agent installed. Add this PC to the manager with the host/IP of this PC, and the token above (CONFIG_TOKEN)."
