"""Manager web server: serves the GUI, a REST API and the shell WebSocket proxy."""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
from pathlib import Path
from typing import Any

import aiohttp
from aiohttp import WSMsgType, web

from . import __version__
from .fleet import Fleet, FleetError

log = logging.getLogger("setup-manager")
STATIC = Path(__file__).parent / "static"


def _json(data: Any, status: int = 200) -> web.Response:
    return web.json_response(data, status=status, dumps=lambda o: json.dumps(o, default=str))


@web.middleware
async def error_middleware(request: web.Request, handler: Any) -> web.StreamResponse:
    try:
        return await handler(request)
    except FleetError as e:
        return _json({"error": e.message}, e.status)
    except web.HTTPException:
        raise
    except Exception as e:  # pragma: no cover
        log.exception("error in %s %s", request.method, request.path)
        return _json({"error": f"{type(e).__name__}: {e}"}, 500)


LOOPBACK_NAMES = {"localhost", "127.0.0.1", "::1", "[::1]"}


def _split_host(value: str) -> str:
    value = value.strip().lower()
    if value.startswith("["):
        return value.split("]")[0] + "]"
    return value.rsplit(":", 1)[0] if value.count(":") == 1 else value


def make_origin_middleware(fleet: Fleet) -> Any:
    """Block cross-site requests and DNS rebinding.

    Browsers let any web page send requests (and open WebSockets) to
    localhost. Without these checks, a page you visit could run commands on
    your setups through the manager.
    """

    @web.middleware
    async def origin_middleware(request: web.Request, handler: Any) -> web.StreamResponse:
        if not request.path.startswith("/api/"):
            return await handler(request)
        data = fleet.store.data()
        host_header = request.headers.get("Host", "")
        allowed = {h.lower() for h in data.get("allowed_hosts") or []}
        host = _split_host(host_header)
        listen = str(data.get("listen") or "127.0.0.1").lower()
        if not data.get("token") and host not in LOOPBACK_NAMES | allowed | {listen}:
            return _json({"error": f"host '{host_header}' not allowed (add it to allowed_hosts or set a token)"},
                         403)
        origin = request.headers.get("Origin")
        if origin:
            o_host = origin.split("://", 1)[-1].rstrip("/").lower()
            if o_host != host_header.lower() and _split_host(o_host) not in allowed:
                return _json({"error": "cross-origin request blocked"}, 403)
        if request.method in ("POST", "PUT", "DELETE", "PATCH") and request.can_read_body:
            if request.content_type != "application/json":
                return _json({"error": "Content-Type must be application/json"}, 415)
        return await handler(request)

    return origin_middleware


def make_auth_middleware(fleet: Fleet) -> Any:
    @web.middleware
    async def auth_middleware(request: web.Request, handler: Any) -> web.StreamResponse:
        token = fleet.store.data().get("token") or ""
        if not token or not request.path.startswith("/api/") or request.path == "/api/login":
            return await handler(request)
        given = ""
        header = request.headers.get("Authorization", "")
        if header.startswith("Bearer "):
            given = header[7:].strip()
        given = given or request.cookies.get("sm_token", "") or request.query.get("token", "")
        if not hmac.compare_digest(given.encode(), token.encode()):
            return _json({"error": "unauthorized"}, 401)
        return await handler(request)

    return auth_middleware


async def _body(request: web.Request) -> dict[str, Any]:
    if not request.can_read_body:
        return {}
    try:
        data = await request.json()
    except ValueError:
        raise FleetError("body must be JSON") from None
    return data if isinstance(data, dict) else {}


def build_app(fleet: Fleet, background: bool = True) -> web.Application:
    app = web.Application(middlewares=[error_middleware, make_origin_middleware(fleet),
                                       make_auth_middleware(fleet)])
    routes = web.RouteTableDef()

    @routes.get("/")
    async def index(request: web.Request) -> web.StreamResponse:
        return web.FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    @routes.post("/api/login")
    async def login(request: web.Request) -> web.Response:
        body = await _body(request)
        token = fleet.store.data().get("token") or ""
        if token and not hmac.compare_digest(str(body.get("token", "")).encode(), token.encode()):
            return _json({"error": "wrong token"}, 401)
        resp = _json({"ok": True})
        resp.set_cookie("sm_token", token, httponly=True, samesite="Strict", max_age=3600 * 24 * 365)
        return resp

    @routes.get("/api/overview")
    async def overview(request: web.Request) -> web.Response:
        ports = await fleet.local_ports()
        return _json({"version": __version__, "setups": fleet.summaries(), "local": {
            "ports": ports, "usbip": fleet.local.info()}, "sticky": fleet.sticky_report()})

    @routes.get("/api/setups")
    async def list_setups(request: web.Request) -> web.Response:
        return _json({"setups": fleet.summaries()})

    @routes.post("/api/setups")
    async def add_setup(request: web.Request) -> web.Response:
        return _json(await fleet.add_setup(await _body(request)))

    @routes.put("/api/setups/{sid}")
    async def update_setup(request: web.Request) -> web.Response:
        return _json(await fleet.update_setup(request.match_info["sid"], await _body(request)))

    @routes.delete("/api/setups/{sid}")
    async def delete_setup(request: web.Request) -> web.Response:
        fleet.remove_setup(request.match_info["sid"])
        return _json({"ok": True})

    @routes.post("/api/discover")
    async def discover(request: web.Request) -> web.Response:
        return _json({"found": await fleet.discover()})

    @routes.get("/api/setups/{sid}/status")
    async def status(request: web.Request) -> web.Response:
        return _json(await fleet.status(request.match_info["sid"]))

    @routes.post("/api/setups/{sid}/refresh")
    async def refresh(request: web.Request) -> web.Response:
        await fleet.refresh(fleet.setup(request.match_info["sid"])["id"])
        return _json({"ok": True})

    @routes.get("/api/setups/{sid}/usb")
    async def usb(request: web.Request) -> web.Response:
        include_hubs = request.query.get("all") in ("1", "true")
        return _json({"devices": await fleet.usb(request.match_info["sid"], include_hubs)})

    @routes.post("/api/setups/{sid}/usb/{busid}/{action}")
    async def usb_action(request: web.Request) -> web.Response:
        sid, busid, action = (request.match_info[k] for k in ("sid", "busid", "action"))
        body = await _body(request)
        force = bool(body.get("force"))
        enabled = bool(body.get("enabled", True))
        actions = {
            "bind": lambda: fleet.bind(sid, busid, force),
            "unbind": lambda: fleet.unbind(sid, busid),
            "reset": lambda: fleet.reset(sid, busid),
            "attach": lambda: fleet.attach(sid, busid, force),
            "detach": lambda: fleet.detach(sid, busid, unbind=bool(body.get("unbind", True))),
            "sticky": lambda: fleet.set_sticky(sid, busid, enabled),
            "autobind": lambda: fleet.set_auto_bind(sid, busid, enabled),
        }
        if action not in actions:
            raise FleetError(f"unknown action {action}", 404)
        return _json(await actions[action]())

    @routes.post("/api/setups/{sid}/usbipd/start")
    async def usbipd_start(request: web.Request) -> web.Response:
        return _json(await fleet.start_usbipd(request.match_info["sid"]))

    @routes.post("/api/setups/{sid}/exec")
    async def exec_one(request: web.Request) -> web.Response:
        b = await _body(request)
        return _json(await fleet.exec(request.match_info["sid"], b.get("command", ""), float(b.get("timeout") or 60),
                                      b.get("cwd"), b.get("user")))

    @routes.post("/api/exec")
    async def exec_many(request: web.Request) -> web.Response:
        b = await _body(request)
        return _json({"results": await fleet.exec_many(b.get("setups") or None, b.get("command", ""),
                                                       float(b.get("timeout") or 60))})

    @routes.get("/api/setups/{sid}/log")
    async def agent_log(request: web.Request) -> web.Response:
        return _json(await fleet.agent_log(request.match_info["sid"], int(request.query.get("n", 200))))

    @routes.get("/api/local")
    async def local(request: web.Request) -> web.Response:
        return _json({"ports": await fleet.local_ports(), "usbip": fleet.local.info()})

    @routes.post("/api/local/detach")
    async def local_detach(request: web.Request) -> web.Response:
        b = await _body(request)
        return _json(await fleet.detach_port(int(b.get("port"))))

    @routes.get("/api/usb/find")
    async def find(request: web.Request) -> web.Response:
        return _json({"devices": await fleet.find_usb(request.query.get("q", ""))})

    @routes.get("/api/setups/{sid}/shell")
    async def shell(request: web.Request) -> web.StreamResponse:
        setup = fleet.setup(request.match_info["sid"])
        client = fleet.client(setup)
        ws = web.WebSocketResponse(heartbeat=30, max_msg_size=16 * 1024 * 1024)
        await ws.prepare(request)
        url = client.shell_url(int(request.query.get("cols", 80)), int(request.query.get("rows", 24)),
                               request.query.get("user") or None)
        assert fleet.session is not None
        try:
            async with fleet.session.ws_connect(url, headers=client.headers, ssl=client.ssl, heartbeat=30,
                                                max_msg_size=16 * 1024 * 1024) as agent_ws:
                async def agent_to_browser() -> None:
                    async for msg in agent_ws:
                        if msg.type == WSMsgType.BINARY:
                            await ws.send_bytes(msg.data)
                        elif msg.type == WSMsgType.TEXT:
                            await ws.send_str(msg.data)
                        else:
                            break

                async def browser_to_agent() -> None:
                    async for msg in ws:
                        if msg.type == WSMsgType.TEXT:
                            await agent_ws.send_str(msg.data)
                        elif msg.type == WSMsgType.BINARY:
                            await agent_ws.send_bytes(msg.data)
                        else:
                            break

                tasks = [asyncio.create_task(agent_to_browser()), asyncio.create_task(browser_to_agent())]
                _, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for t in pending:
                    t.cancel()
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            if not ws.closed:
                await ws.send_str(json.dumps({"t": "error", "error": f"cannot open shell on {setup['name']}: {e}"}))
        finally:
            if not ws.closed:
                await ws.close()
        return ws

    app.add_routes(routes)
    app.router.add_static("/static", STATIC, append_version=True)

    if background:
        async def on_startup(app: web.Application) -> None:
            await fleet.start_background()

        async def on_cleanup(app: web.Application) -> None:
            await fleet.close()

        app.on_startup.append(on_startup)
        app.on_cleanup.append(on_cleanup)
    return app


def serve(fleet: Fleet, host: str, port: int) -> None:
    app = build_app(fleet)
    url_host = "localhost" if host in ("127.0.0.1", "0.0.0.0", "::") else host
    print(f"Setup Manager {__version__}: open http://{url_host}:{port}/ in your browser")
    if host not in ("127.0.0.1", "localhost", "::1") and not fleet.store.data().get("token"):
        print("WARNING: listening on a network address without a manager token. "
              "Anyone on the network gets root shells on your setups. Set \"token\" in "
              f"{fleet.store.path}.")
    web.run_app(app, host=host, port=port, print=None, access_log=None)
