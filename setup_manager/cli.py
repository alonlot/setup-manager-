"""Command line entry point: `setup-manager <command>`."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .config import ConfigStore
from .fleet import Fleet, FleetError

AGENT_DIR = Path(__file__).parent / "agent"


def _print(data: Any, as_json: bool) -> None:
    if as_json or not isinstance(data, list):
        print(json.dumps(data, indent=2, default=str))
        return
    for row in data:
        print(row)


def _run(coro_fn: Any) -> Any:
    async def go() -> Any:
        async with Fleet(STORE) as fleet:
            return await coro_fn(fleet)
    try:
        return asyncio.run(go())
    except FleetError as e:
        print(f"error: {e.message}", file=sys.stderr)
        sys.exit(1)


def cmd_list(args: argparse.Namespace) -> None:
    async def go(f: Fleet) -> Any:
        await f.refresh_all()
        return f.summaries()
    rows = _run(go)
    if args.json:
        return _print(rows, True)
    if not rows:
        print(f"no setups in {STORE.path}. Add one: setup-manager add HOST --token TOKEN "
              "(or setup-manager deploy user@host)")
    for s in rows:
        state = "online " if s["online"] else "OFFLINE"
        extra = f"usb={s.get('usb_count')} bound={s.get('usb_bound')}" if s["online"] else (s.get("error") or "")
        print(f"{state}  {s['id']:<18} {s['host']:<20} {extra}")


def cmd_usb(args: argparse.Namespace) -> None:
    devs = _run(lambda f: f.usb(args.setup, args.all))
    if args.json:
        return _print(devs, True)
    for d in devs:
        here = f" [attached here, port {d['local_port']}]" if d.get("attached_here") else ""
        tty = f" {','.join(d['ttys'])}" if d.get("ttys") else ""
        print(f"{d['busid']:<10} {d['vidpid']}  {d['state']:<9} {d['name']}{tty}{here}")


def cmd_attached(args: argparse.Namespace) -> None:
    ports = _run(lambda f: f.local_ports())
    if args.json:
        return _print(ports, True)
    for p in ports:
        print(f"port {p['port']:<3} {p.get('setup') or p.get('host')}:{p['busid']}  {p.get('vid')}:{p.get('pid')} "
              f"{p.get('name') or ''}")


def cmd_exec(args: argparse.Namespace) -> None:
    command = " ".join(args.command)
    if args.setup == "all":
        results = _run(lambda f: f.exec_many(None, command, args.timeout))
    else:
        results = [_run(lambda f: f.exec(args.setup, command, args.timeout))]
    if args.json:
        return _print(results, True)
    rc = 0
    for r in results:
        if len(results) > 1:
            print(f"===== {r['setup']} (exit {r.get('exit_code')}) =====")
        if r.get("error"):
            print(f"error: {r['error']}", file=sys.stderr)
            rc = 1
            continue
        sys.stdout.write(r["stdout"])
        sys.stderr.write(r["stderr"])
        rc = rc or (r["exit_code"] or 0)
    sys.exit(rc)


def cmd_simple(action: str) -> Any:
    def handler(args: argparse.Namespace) -> None:
        fn = {
            "attach": lambda f: f.attach(args.setup, args.busid, args.force),
            "detach": lambda f: f.detach(args.setup, args.busid, not args.keep_bound),
            "bind": lambda f: f.bind(args.setup, args.busid, args.force),
            "unbind": lambda f: f.unbind(args.setup, args.busid),
            "reset": lambda f: f.reset(args.setup, args.busid),
        }[action]
        r = _run(fn)
        print(r.get("message") if not args.json else json.dumps(r, indent=2, default=str))
    return handler


def cmd_identify(args: argparse.Namespace) -> None:
    opts: dict[str, Any] = {"reset": args.reset, "baud": args.baud, "duration": args.duration, "tty": args.tty}
    for key, path in (("script", args.script), ("after_script", args.after_script)):
        if path:
            opts[key] = sys.stdin.read() if path == "-" else Path(path).read_text()
    if args.list_profiles:
        profiles = Fleet(STORE).identify_profiles()
        for name, p in profiles.items():
            print(f"{name:<28} reset={p.get('reset')} baud={p.get('baud')}  {'' if p['builtin'] else '(yours)'}")
        return
    if not (args.setup and args.busid):
        print("error: identify needs SETUP and BUSID", file=sys.stderr)
        sys.exit(2)
    r = _run(lambda f: f.identify(args.setup, args.busid, opts, args.profile))
    if args.save_profile:
        Fleet(STORE).save_identify_profile(args.save_profile, {k: v for k, v in opts.items() if v is not None})
    if args.json:
        return _print(r, True)
    g, cap = r["guess"], r["capture"]
    print(f"MCU: {g['label']}  ({round(100 * g['confidence'])}%{', sure' if g['sure'] else ''})")
    if g.get("firmware"):
        print("Firmware: " + ", ".join(g["firmware"]))
    for e in g.get("evidence", [])[:5]:
        print(f"  - {e}")
    print(f"Reset: {cap.get('reset')}  Baud: {cap.get('baud')}  Ports: {', '.join(cap.get('ttys') or []) or 'none'}"
          f"  Ran on: {r['where']}")
    for n in cap.get("notes", []):
        print(f"  note: {n}")
    for s_ in cap.get("scripts", []):
        print(f"  {s_.get('phase')} script: exit {s_.get('exit_code')}")
    if args.log or not g.get("mcu"):
        print("----- log -----")
        print(cap.get("log", "").rstrip() or "(nothing received)")


def cmd_serial(args: argparse.Namespace) -> None:
    from .serial_client import run_serial
    logging.getLogger().setLevel(logging.WARNING)
    sys.exit(run_serial(STORE, args))


def cmd_upload(args: argparse.Namespace) -> None:
    r = _run(lambda f: f.upload(args.setup, args.file, args.remote))
    print(json.dumps(r, indent=2) if args.json else f"{r['path']}  {r['size']} bytes  sha256 {r['sha256'][:16]}…")


def cmd_download(args: argparse.Namespace) -> None:
    r = _run(lambda f: f.download(args.setup, args.remote, args.local))
    print(json.dumps(r, indent=2) if args.json else f"{r['path']}  {r['size']} bytes")


def cmd_files(args: argparse.Namespace) -> None:
    r = _run(lambda f: f.files(args.setup, args.path))
    if args.json:
        return _print(r, True)
    print(r["path"])
    for x in r["files"]:
        print(f"  {'d' if x['dir'] else '-'} {x['size']:>12}  {x['name']}")


def cmd_flash(args: argparse.Namespace) -> None:
    if args.list_profiles:
        for name, p in Fleet(STORE).flash_profiles().items():
            print(f"{name:<38} {'' if p['builtin'] else '(yours)'}")
        return
    if not (args.setup and args.busid and args.file):
        print("error: flash needs SETUP BUSID FILE", file=sys.stderr)
        sys.exit(2)
    opts: dict[str, Any] = {"timeout": args.timeout}
    if args.script:
        opts["script"] = sys.stdin.read() if args.script == "-" else Path(args.script).read_text()
    if args.env:
        opts["env"] = dict(e.split("=", 1) for e in args.env)
    if args.monitor is not None:
        opts["monitor_seconds"] = args.monitor
    profile = args.profile
    if not profile and not args.script:
        profile = None

    async def go(f: Fleet) -> Any:
        prof = profile
        if not prof and "script" not in opts:
            devs = await f.usb(args.setup, include_hubs=True)
            d = next((x for x in devs if x["busid"] == args.busid), {"busid": args.busid})
            last = f.flash_options(args.setup, d)
            prof = last.get("profile") or None
            for k in ("script", "env", "monitor_seconds"):
                if k in last and k not in opts:
                    opts[k] = last[k]
            print(f"profile: {prof}", file=sys.stderr)
        return await f.flash(args.setup, args.busid, args.file, prof, opts)
    r = _run(go)
    if args.json:
        return _print(r, True)
    sys.stdout.write(r.get("stdout") or "")
    sys.stderr.write(r.get("stderr") or "")
    print(f"\n=== flash {'OK' if r['ok'] else 'FAILED'} (exit {r.get('exit_code')}, {r.get('duration')}s, "
          f"on {r['where']}) ===")
    m = r.get("monitor")
    if m:
        print(f"--- serial after the flash ({m.get('tty')}) ---")
        print(m.get("text") or m.get("error") or "(nothing)")
    sys.exit(0 if r["ok"] else 1)


def cmd_rename(args: argparse.Namespace) -> None:
    r = _run(lambda f: f.set_nickname(args.setup, args.busid, " ".join(args.name)))
    print(r["message"])


def cmd_add(args: argparse.Namespace) -> None:
    fields = {"name": args.name or args.host, "host": args.host, "port": args.port, "token": args.token or "",
              "tls": args.tls}
    r = _run(lambda f: f.add_setup(fields))
    state = "online" if r["online"] else f"offline ({r['error']})"
    print(f"added {r['setup']['id']}: {state}")


def cmd_remove(args: argparse.Namespace) -> None:
    STORE.remove_setup(Fleet(STORE).setup(args.setup)["id"])
    print("removed")


def cmd_discover(args: argparse.Namespace) -> None:
    found = _run(lambda f: f.discover(args.timeout))
    if args.json:
        return _print(found, True)
    if not found:
        print("no agents answered (UDP broadcast, port %s)" % STORE.data().get("discovery_port"))
    for a in found:
        known = f" (known as {a['known_as']})" if a.get("known_as") else ""
        print(f"{a['host']:<16} port {a.get('port')}  {a.get('name')}  v{a.get('version')}{known}")


def cmd_app(args: argparse.Namespace) -> None:
    try:
        from .app.main import run_app
    except (ImportError, ValueError) as e:
        print(f"cannot start the desktop app: {e}\n"
              "Install GTK4, libadwaita and VTE for Python:\n"
              "  sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 gir1.2-vte-3.91\n"
              "and use a Python that sees them (see install-app.sh).", file=sys.stderr)
        sys.exit(1)
    sys.exit(run_app(STORE))


def cmd_shell(args: argparse.Namespace) -> None:
    from .shell_client import run_shell
    logging.getLogger().setLevel(logging.WARNING)
    sys.exit(run_shell(STORE, args.setup, args.user))


def cmd_daemon(args: argparse.Namespace) -> None:
    """Keep-attached and polling without the app (for example as a systemd user service)."""
    async def go() -> None:
        fleet = Fleet(STORE)
        await fleet.start_background()
        logging.info("setup-manager daemon running (keep-attached: %d devices)", len(STORE.sticky()))
        try:
            await asyncio.Event().wait()
        finally:
            await fleet.close()
    try:
        asyncio.run(go())
    except KeyboardInterrupt:
        pass


def cmd_mcp(args: argparse.Namespace) -> None:
    from .mcp_server import run
    logging.getLogger().setLevel(logging.WARNING)
    run("streamable-http" if args.http else "stdio", args.listen, args.port, STORE)


def cmd_agent_files(args: argparse.Namespace) -> None:
    print(AGENT_DIR)


def cmd_deploy(args: argparse.Namespace) -> None:
    """Copy the agent to a setup PC over SSH and run the installer there with sudo."""
    files = ["setup_agent.py", "install.sh", "uninstall.sh", "setup-agent.service", "setup-usbipd.service"]
    token = args.token or STORE.data()["agent_defaults"].get("token") or ""
    remote_dir = "/tmp/setup-agent-install"
    ssh = ["ssh"] + (["-p", str(args.ssh_port)] if args.ssh_port else [])
    scp = ["scp", "-q"] + (["-P", str(args.ssh_port)] if args.ssh_port else [])
    target = args.target
    host = target.split("@")[-1]
    install_args = []
    if args.name:
        install_args += ["--name", args.name]
    if token:
        install_args += ["--token", token]
    if args.install_deps:
        install_args.append("--install-deps")
    if args.tls:
        install_args.append("--tls")
    for rule in args.auto_bind or []:
        install_args += ["--auto-bind", rule]
    print(f">> copying agent to {target}")
    subprocess.run(ssh + [target, f"mkdir -p {remote_dir}"], check=True)
    subprocess.run(scp + [str(AGENT_DIR / f) for f in files] + [f"{target}:{remote_dir}/"], check=True)
    print(">> running installer (sudo may ask for a password)")
    remote = f"sudo bash {remote_dir}/install.sh {' '.join(shlex.quote(a) for a in install_args)}"
    # Stream the output (the sudo password prompt has no newline) and keep a copy.
    proc = subprocess.Popen(ssh + ["-t", target, remote], stdout=subprocess.PIPE)
    assert proc.stdout is not None
    chunks = []
    while True:
        chunk = os.read(proc.stdout.fileno(), 4096)
        if not chunk:
            break
        chunks.append(chunk)
        sys.stdout.write(chunk.decode(errors="replace"))
        sys.stdout.flush()
    if proc.wait() != 0:
        sys.exit(proc.returncode)
    output = b"".join(chunks).decode(errors="replace")
    agent_token = token
    for line in output.splitlines():
        if line.startswith("CONFIG_TOKEN="):
            agent_token = line.split("=", 1)[1].strip()
    if not args.no_add:
        existing = STORE.get_setup(args.name or host)
        fields = {"name": args.name or host, "host": host, "token": agent_token, "tls": args.tls}
        if existing:
            r = _run(lambda f: f.update_setup(existing["id"], fields))
        else:
            r = _run(lambda f: f.add_setup(fields))
        state = "online" if r["online"] else f"offline ({r['error']})"
        print(f">> setup '{r['setup']['id']}' saved in the manager: {state}")


STORE: ConfigStore


def main(argv: list[str] | None = None) -> None:
    global STORE
    ap = argparse.ArgumentParser(prog="setup-manager", description="Manage USB/IP setup PCs")
    ap.add_argument("--config", help="config file (default: %(default)s)", default=None)
    ap.add_argument("--json", action="store_true", help="JSON output")
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("app", help="open the desktop app (default command)")
    p.set_defaults(fn=cmd_app)

    p = sub.add_parser("shell", help="interactive shell on a setup, in this terminal")
    p.add_argument("setup")
    p.add_argument("--user", help="log in as this user (default: the setup's run_as user)")
    p.set_defaults(fn=cmd_shell)

    p = sub.add_parser("daemon", help="keep-attached and polling without the app")
    p.set_defaults(fn=cmd_daemon)

    p = sub.add_parser("mcp", help="run the MCP server for AI agents (stdio by default)")
    p.add_argument("--http", action="store_true", help="streamable HTTP instead of stdio")
    p.add_argument("--listen", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8801)
    p.set_defaults(fn=cmd_mcp)

    p = sub.add_parser("list", help="list setups")
    p.set_defaults(fn=cmd_list)

    p = sub.add_parser("usb", help="list USB devices of a setup")
    p.add_argument("setup")
    p.add_argument("--all", action="store_true", help="include hubs")
    p.set_defaults(fn=cmd_usb)

    p = sub.add_parser("attached", help="list devices attached to this PC")
    p.set_defaults(fn=cmd_attached)

    for action, helptext in (("attach", "bind on the setup and attach here"),
                             ("detach", "detach here and unbind on the setup"),
                             ("bind", "usbip bind on the setup"), ("unbind", "usbip unbind on the setup"),
                             ("reset", "USB reset of the device on the setup")):
        p = sub.add_parser(action, help=helptext)
        p.add_argument("setup")
        p.add_argument("busid")
        if action in ("attach", "bind"):
            p.add_argument("--force", action="store_true")
        if action == "detach":
            p.add_argument("--keep-bound", action="store_true", help="do not unbind on the setup")
        p.set_defaults(fn=cmd_simple(action))

    p = sub.add_parser("exec", help="run a command on a setup ('all' for every setup)")
    p.add_argument("setup")
    p.add_argument("command", nargs=argparse.REMAINDER)
    p.add_argument("--timeout", type=float, default=60)
    p.set_defaults(fn=cmd_exec)

    p = sub.add_parser("identify", help="reset the MCU, read its boot log and guess which MCU it is")
    p.add_argument("setup", nargs="?")
    p.add_argument("busid", nargs="?")
    p.add_argument("--profile", help="identify profile (see --list-profiles)")
    p.add_argument("--reset", help="auto, rts, dtr, rts+dtr, repl, probe, usb or none")
    p.add_argument("--baud", help="'auto' or a number, e.g. 115200")
    p.add_argument("--duration", type=float, help="seconds to capture after the reset")
    p.add_argument("--tty", help="only this port of the device, e.g. ttyUSB1")
    p.add_argument("--script", help="bash script file to run at the start (before the reset), '-' = stdin")
    p.add_argument("--after-script", help="bash script file to run after the reset")
    p.add_argument("--save-profile", metavar="NAME", help="save these options as a profile")
    p.add_argument("--list-profiles", action="store_true")
    p.add_argument("--log", action="store_true", help="print the captured log")
    p.set_defaults(fn=cmd_identify)

    p = sub.add_parser("serial", help="serial terminal of a device (Ctrl-] exits), or wait for a pattern")
    p.add_argument("setup")
    p.add_argument("busid", nargs="?")
    p.add_argument("--tty", help="a port of the device (e.g. ttyUSB1), or a port name without busid")
    p.add_argument("--baud", type=int, default=None, help="default 115200, or the rate the port has now")
    p.add_argument("--eol", choices=["cr", "lf", "crlf"], default="cr", help="what Enter sends (default cr)")
    p.add_argument("--history", action="store_true", help="first print what is already in the buffer")
    p.add_argument("--read-only", action="store_true")
    p.add_argument("--wait-for", metavar="REGEX", help="exit 0 when the output matches, 1 on timeout")
    p.add_argument("--timeout", type=float, default=0, help="seconds (with --wait-for, or to stop reading)")
    p.set_defaults(fn=cmd_serial)

    p = sub.add_parser("upload", help="copy a file to a setup PC")
    p.add_argument("setup")
    p.add_argument("file")
    p.add_argument("remote", nargs="?", help="path on the setup (relative = in the agent's files folder)")
    p.set_defaults(fn=cmd_upload)

    p = sub.add_parser("download", help="copy a file from a setup PC")
    p.add_argument("setup")
    p.add_argument("remote")
    p.add_argument("local", nargs="?")
    p.set_defaults(fn=cmd_download)

    p = sub.add_parser("files", help="list a folder on a setup PC")
    p.add_argument("setup")
    p.add_argument("path", nargs="?", default=".")
    p.set_defaults(fn=cmd_files)

    p = sub.add_parser("flash", help="flash a firmware file to a board")
    p.add_argument("setup", nargs="?")
    p.add_argument("busid", nargs="?")
    p.add_argument("file", nargs="?")
    p.add_argument("--profile", help="flash profile (see --list-profiles). Default: the last one for the device")
    p.add_argument("--script", help="bash script file instead of the profile script ('-' = stdin)")
    p.add_argument("--env", action="append", metavar="NAME=VALUE", help="e.g. SM_TARGET=target/stm32h7x.cfg")
    p.add_argument("--monitor", type=float, help="seconds of serial output to show after the flash")
    p.add_argument("--timeout", type=float, default=300)
    p.add_argument("--list-profiles", action="store_true")
    p.set_defaults(fn=cmd_flash)

    p = sub.add_parser("rename", help="give a device a nickname (no name = remove it)")
    p.add_argument("setup")
    p.add_argument("busid")
    p.add_argument("name", nargs="*")
    p.set_defaults(fn=cmd_rename)

    p = sub.add_parser("add", help="add a setup")
    p.add_argument("host")
    p.add_argument("--name")
    p.add_argument("--port", type=int)
    p.add_argument("--token")
    p.add_argument("--tls", action="store_true")
    p.set_defaults(fn=cmd_add)

    p = sub.add_parser("remove", help="remove a setup")
    p.add_argument("setup")
    p.set_defaults(fn=cmd_remove)

    p = sub.add_parser("discover", help="find agents on the local network (UDP broadcast)")
    p.add_argument("--timeout", type=float, default=1.5)
    p.set_defaults(fn=cmd_discover)

    p = sub.add_parser("deploy", help="install the agent on a setup PC over SSH")
    p.add_argument("target", help="user@host")
    p.add_argument("--name")
    p.add_argument("--token", help="agent token (default: agent_defaults.token, or a new random one)")
    p.add_argument("--ssh-port", type=int)
    p.add_argument("--install-deps", action="store_true", help="install usbip with the package manager")
    p.add_argument("--tls", action="store_true", help="enable TLS with a self-signed certificate")
    p.add_argument("--auto-bind", action="append", help="auto-bind rule, e.g. 0483:374b (repeatable)")
    p.add_argument("--no-add", action="store_true", help="do not add the setup to the manager config")
    p.set_defaults(fn=cmd_deploy)

    p = sub.add_parser("agent-files", help="print the folder with the agent files")
    p.set_defaults(fn=cmd_agent_files)

    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        stream=sys.stderr)
    STORE = ConfigStore(args.config or os.environ.get("SETUP_MANAGER_CONFIG"))
    if not getattr(args, "fn", None):
        args = ap.parse_args((argv if argv is not None else sys.argv[1:]) + ["app"])
    if args.cmd == "exec" and not args.command:
        ap.error("exec needs a command")
    if args.cmd == "exec" and args.command[:1] == ["--"]:
        args.command = args.command[1:]
    args.fn(args)


if __name__ == "__main__":
    main()
