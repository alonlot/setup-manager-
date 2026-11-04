"""Encrypted USB/IP: a local port on 127.0.0.1 that forwards to the agent's TLS port.

`usbip attach -r 127.0.0.1 --tcp-port <local port>` connects to this helper. The
helper connects to the agent with TLS (pinned certificate), sends the token, and
relays the bytes. After the attach, the kernel keeps using the socket, so the
helper runs as its own process (it stays when the app closes) and exits when the
device is detached.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import ssl
import sys
from pathlib import Path
from typing import Any

from .agent_client import fingerprint_of


class TunnelError(Exception):
    pass


def state_file() -> Path:
    base = os.environ.get("SETUP_MANAGER_STATE") or os.path.join(
        os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"), "setup-manager")
    return Path(base) / "tunnels.json"


def load_tunnels() -> dict[str, dict[str, Any]]:
    try:
        return json.loads(state_file().read_text())
    except (OSError, ValueError):
        return {}


def save_tunnels(t: dict[str, dict[str, Any]]) -> None:
    path = state_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(t, indent=1))
    os.replace(tmp, path)


async def open_tunnel(host: str, port: int, fingerprint: str, token: str, leases: list[str] | None = None,
                      verify: bool = False, timeout: float = 20) -> tuple[int, Any]:
    """Start a helper process. Returns (local port, process). Errors of the helper are on process.stderr."""
    env = dict(os.environ, SM_TUNNEL_HOST=host, SM_TUNNEL_PORT=str(port), SM_TUNNEL_FP=fingerprint or "",
               SM_TUNNEL_TOKEN=token or "", SM_TUNNEL_LEASES=",".join(leases or []),
               SM_TUNNEL_VERIFY="1" if verify else "")
    pkg_parent = str(Path(__file__).resolve().parent.parent)
    env["PYTHONPATH"] = pkg_parent + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    proc = await asyncio.create_subprocess_exec(sys.executable, "-m", "setup_manager.tunnel", env=env,
                                                stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.PIPE, start_new_session=True)
    try:
        line = await asyncio.wait_for(proc.stdout.readline(), timeout)  # type: ignore[union-attr]
    except asyncio.TimeoutError:
        proc.kill()
        raise TunnelError(f"the tunnel to {host}:{port} did not start") from None
    if not line.startswith(b"PORT "):
        err = (await proc.stderr.read()).decode(errors="replace").strip()  # type: ignore[union-attr]
        raise TunnelError(err or line.decode(errors="replace").strip() or "the tunnel did not start")
    return int(line.split()[1]), proc


async def tunnel_error(proc: Any, wait: float = 2.0) -> str:
    """The error text of a helper that failed (after a failed usbip attach)."""
    try:
        data = await asyncio.wait_for(proc.stderr.read(), wait)
        return data.decode(errors="replace").strip()
    except (asyncio.TimeoutError, AttributeError):
        return ""


def _fail(msg: str) -> int:
    sys.stderr.write(msg + "\n")
    sys.stderr.flush()
    return 1


def main() -> int:
    from .agent.setup_agent import relay

    host, port = os.environ["SM_TUNNEL_HOST"], int(os.environ["SM_TUNNEL_PORT"])
    pin, token = os.environ.get("SM_TUNNEL_FP", ""), os.environ.get("SM_TUNNEL_TOKEN", "")
    leases = os.environ.get("SM_TUNNEL_LEASES", "")
    verify = bool(os.environ.get("SM_TUNNEL_VERIFY"))
    for k in ("SM_TUNNEL_TOKEN", "SM_TUNNEL_LEASES"):
        os.environ.pop(k, None)

    # 1. TLS to the agent, with the pinned certificate.
    ctx = ssl.create_default_context()
    if not verify:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    try:
        raw = socket.create_connection((host, port), timeout=15)
        tls = ctx.wrap_socket(raw, server_hostname=host if verify else None)
    except (OSError, ssl.SSLError) as e:
        return _fail(f"cannot open the encrypted tunnel to {host}:{port}: {e}")
    if pin and fingerprint_of(tls.getpeercert(binary_form=True)) != pin:
        return _fail(f"the TLS certificate of {host} changed! The tunnel was not opened (this can be an attack)")
    tls.sendall(f"SMTUNNEL {token} {leases}\n".encode())

    # 2. The local port for usbip.
    lst = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    lst.bind(("127.0.0.1", 0))
    lst.listen(1)
    lst.settimeout(30)
    sys.stdout.write(f"PORT {lst.getsockname()[1]}\n")
    sys.stdout.flush()
    try:
        conn, _ = lst.accept()
    except OSError:
        return _fail("usbip did not connect to the tunnel")
    lst.close()  # one connection only

    # 3. The first USB/IP request goes to the agent, which answers OK (or ERR) before the relay.
    conn.settimeout(15)
    first = b""
    try:
        while len(first) < 8:
            chunk = conn.recv(4096)
            if not chunk:
                return _fail("usbip closed the connection")
            first += chunk
        if first[2:4] == b"\x80\x03":  # OP_REQ_IMPORT: the agent needs the busid (40 bytes)
            while len(first) < 40:
                chunk = conn.recv(4096)
                if not chunk:
                    return _fail("usbip closed the connection")
                first += chunk
        tls.sendall(first)
        tls.settimeout(15)
        answer = b""
        while not answer.endswith(b"\n") and len(answer) < 512:
            chunk = tls.recv(1)
            if not chunk:
                break
            answer += chunk
    except (OSError, ssl.SSLError) as e:
        return _fail(f"tunnel error: {e}")
    if not answer.startswith(b"OK"):
        conn.close()
        return _fail(answer.decode(errors="replace").strip().removeprefix("ERR ") or "the agent closed the tunnel")

    # 4. From now on only relay. Errors no longer go to the parent (it does not read anymore).
    sys.stderr = open(os.devnull, "w")
    os.close(2)
    relay(tls, conn)
    return 0


if __name__ == "__main__":
    sys.exit(main())
