"""Setup Manager desktop app: application, main window and sidebar."""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any, Awaitable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Vte", "3.91")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from .. import __version__  # noqa: E402
from ..config import ConfigStore  # noqa: E402
from ..fleet import Fleet  # noqa: E402
from . import APP_ID  # noqa: E402
from .bridge import AsyncBridge  # noqa: E402
from .dialogs import confirm, discover_dialog, preferences_dialog, setup_dialog  # noqa: E402
from .pages import OverviewPage, Page, RunManyPage, SearchPage, SetupPage  # noqa: E402
from .widgets import copy_text, dot  # noqa: E402

log = logging.getLogger("setup-manager")
HERE = Path(__file__).resolve().parent


class SidebarRow(Gtk.ListBoxRow):
    def __init__(self, key: str, title: str, icon: str | None = None):
        super().__init__()
        self.key = key
        box = Gtk.Box(spacing=10, margin_top=6, margin_bottom=6, margin_start=6, margin_end=6)
        self.prefix = Gtk.Box()
        if icon:
            self.prefix.append(Gtk.Image(icon_name=icon))
        box.append(self.prefix)
        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
        self.title = Gtk.Label(label=title, xalign=0, ellipsize=3)
        self.subtitle = Gtk.Label(xalign=0, ellipsize=3, css_classes=["dim-label", "caption"], visible=False)
        texts.append(self.title)
        texts.append(self.subtitle)
        box.append(texts)
        self.set_child(box)

    def set_status(self, online: bool | None, subtitle: str) -> None:
        child = self.prefix.get_first_child()
        if child:
            self.prefix.remove(child)
        self.prefix.append(dot(online))
        self.subtitle.set_label(subtitle)
        self.subtitle.set_visible(bool(subtitle))


class MainWindow(Adw.ApplicationWindow):
    def __init__(self, app: "SetupManagerApp"):
        super().__init__(application=app, title="Setup Manager", default_width=1240, default_height=800,
                         width_request=360, height_request=320)
        self.app = app
        self.store: ConfigStore = app.store
        self.fleet: Fleet = app.fleet
        self.bridge: AsyncBridge = app.bridge
        self.busy: set[str] = set()
        self.snapshot: dict[str, Any] = {"setups": [], "ports": [], "sticky": [], "usbip": {}}
        self._snap_key = ""
        self.pages: dict[str, Page] = {}
        self.current: Page | None = None
        self.setup_rows: dict[str, SidebarRow] = {}

        self.toasts = Adw.ToastOverlay()
        self.split = Adw.NavigationSplitView(min_sidebar_width=240, max_sidebar_width=320)
        self.toasts.set_child(self.split)
        self.set_content(self.toasts)

        bp = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 720sp"))
        bp.add_setter(self.split, "collapsed", True)
        self.add_breakpoint(bp)

        # Sidebar.
        sb = Adw.ToolbarView()
        hb = Adw.HeaderBar()
        hb.set_title_widget(Adw.WindowTitle(title="Setup Manager"))
        add = Gtk.Button(icon_name="list-add-symbolic", tooltip_text="Add setup (Ctrl+N)")
        add.set_action_name("app.add-setup")
        hb.pack_start(add)
        menu = Gio.Menu()
        menu.append("Scan network…", "app.discover")
        menu.append("Refresh all", "app.refresh")
        section = Gio.Menu()
        section.append("Preferences", "app.preferences")
        section.append("About Setup Manager", "app.about")
        menu.append_section(None, section)
        hb.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu, primary=True,
                                   tooltip_text="Main menu"))
        sb.add_top_bar(hb)

        self.sidebar = Gtk.ListBox(css_classes=["navigation-sidebar"])
        self.sidebar.connect("row-selected", self._row_selected)
        for key, title, icon in (("overview", "Overview", "view-grid-symbolic"),
                                 ("search", "Find device", "system-search-symbolic"),
                                 ("run", "Run on many", "utilities-terminal-symbolic")):
            self.sidebar.append(SidebarRow(key, title, icon))
        self.setups_header = Gtk.ListBoxRow(selectable=False, activatable=False)
        self.setups_label = Gtk.Label(label="SETUPS", xalign=0, css_classes=["dim-label", "caption-heading"],
                                      margin_top=12, margin_start=8)
        self.setups_header.set_child(self.setups_label)
        self.sidebar.append(self.setups_header)
        sb.set_content(Gtk.ScrolledWindow(child=self.sidebar, vexpand=True,
                                          hscrollbar_policy=Gtk.PolicyType.NEVER))
        self.split.set_sidebar(Adw.NavigationPage(title="Setups", child=sb))

        self.content = Adw.NavigationPage(title="Overview", tag="content")
        self.split.set_content(self.content)

        self.sidebar.select_row(self.sidebar.get_row_at_index(0))
        GLib.timeout_add(3000, self._poll)
        self.refresh_now()

    # -- navigation ------------------------------------------------------------
    def _page(self, key: str) -> Page:
        if key not in self.pages:
            if key == "overview":
                self.pages[key] = OverviewPage(self)
            elif key == "search":
                self.pages[key] = SearchPage(self)
            elif key == "run":
                self.pages[key] = RunManyPage(self)
            else:
                self.pages[key] = SetupPage(self, key[len("setup:"):])
            self.pages[key].on_snapshot(self.snapshot)
        return self.pages[key]

    def _row_selected(self, _lb: Gtk.ListBox, row: SidebarRow | None) -> None:
        if row is None or not isinstance(row, SidebarRow):
            return
        page = self._page(row.key)
        if self.current is page:
            self.split.set_show_content(True)
            return
        if self.current:
            self.current.on_hidden()
        self.current = page
        self.content.set_child(page.widget)
        self.content.set_title(page.title)
        page.on_snapshot(self.snapshot)
        page.on_shown()
        self.split.set_show_content(True)

    def select_setup(self, sid: str) -> None:
        row = self.setup_rows.get(sid)
        if row:
            self.sidebar.select_row(row)

    def select_key(self, key: str) -> None:
        if key.startswith("setup:"):
            return self.select_setup(key[6:])
        row = self.sidebar.get_first_child()
        while row is not None:
            if isinstance(row, SidebarRow) and row.key == key:
                self.sidebar.select_row(row)
                return
            row = row.get_next_sibling()

    # -- data ------------------------------------------------------------------
    def _poll(self) -> bool:
        self.refresh_now()
        return True

    def refresh_now(self, select: str | None = None) -> None:
        def done(snap: dict[str, Any]) -> None:
            self._apply_snapshot(snap)
            if select == "overview":
                self.select_key("overview")
            elif select:
                self.select_setup(select)

        self.bridge.run(self.fleet.snapshot(), done, lambda e: log.warning("snapshot failed: %s", e))

    def _apply_snapshot(self, snap: dict[str, Any]) -> None:
        self.snapshot = snap
        setups = snap["setups"]
        ids = [s["id"] for s in setups]
        # Remove deleted setups.
        for sid in list(self.setup_rows):
            if sid not in ids:
                row = self.setup_rows.pop(sid)
                if self.sidebar.get_selected_row() is row:
                    self.select_key("overview")
                self.sidebar.remove(row)
                page = self.pages.pop(f"setup:{sid}", None)
                if page:
                    page.on_hidden()
        for s in setups:
            row = self.setup_rows.get(s["id"])
            if row is None:
                row = SidebarRow(f"setup:{s['id']}", s["name"])
                self.setup_rows[s["id"]] = row
                self.sidebar.append(row)
            row.title.set_label(s["name"])
            sub = s["host"] + (f"  ·  {s.get('usb_count') or 0} USB" if s.get("online") else
                               "  ·  offline" if s.get("online") is False else "")
            row.set_status(s.get("online"), sub)
        online = sum(1 for s in setups if s.get("online"))
        self.setups_label.set_label(f"SETUPS  ·  {online}/{len(setups)} ONLINE" if setups else "SETUPS")
        for page in list(self.pages.values()):
            page.on_snapshot(snap)
        if self.current:
            self.content.set_title(self.current.title)

    def toast(self, text: str, timeout: int = 4) -> None:
        t = Adw.Toast(title=GLib.markup_escape_text(text), timeout=timeout)
        self.toasts.add_toast(t)

    # -- actions -----------------------------------------------------------------
    def _redraw_busy(self) -> None:
        for page in self.pages.values():
            if isinstance(page, SetupPage):
                page.draw_usb()
            elif isinstance(page, SearchPage) and page is self.current:
                page.draw()
            elif isinstance(page, OverviewPage):
                page.on_snapshot(self.snapshot)

    def run_action(self, key: str, coro: Awaitable[Any], ok: str | None = None) -> None:
        if key in self.busy:
            return
        self.busy.add(key)
        self._redraw_busy()

        def finish() -> None:
            self.busy.discard(key)
            self.refresh_now()
            for page in self.pages.values():
                if isinstance(page, SetupPage):
                    page._last_usb = ""
                    if page is self.current:
                        page.load_usb()
                elif isinstance(page, SearchPage) and page is self.current:
                    page.search()
            self._redraw_busy()

        def done(r: Any) -> None:
            msg = ok or (r.get("message") if isinstance(r, dict) else None) or "Done"
            self.toast(msg)
            finish()

        def fail(e: Exception) -> None:
            self.toast(getattr(e, "message", str(e)), timeout=8)
            finish()

        self.bridge.run(coro, done, fail)

    def usb_action(self, sid: str, busid: str, action: str, d: dict[str, Any]) -> None:
        f = self.fleet
        key = f"{sid}/{busid}"
        name = f"{d.get('nickname') or d.get('name') or busid} ({busid})"
        if action == "attach":
            if (d.get("net") or d.get("is_hub")) and not d.get("bound"):
                confirm(self, "Attach this device?", f"{name} is a network adapter or a hub. Sharing it can "
                        "disconnect the setup PC from the network.", "Attach anyway",
                        lambda: self.run_action(key, f.attach(sid, busid, True), f"Attached {name} to this PC"))
            else:
                self.run_action(key, f.attach(sid, busid), f"Attached {name} to this PC")
        elif action == "take":
            who = d.get("used_by_text") or "another client"
            confirm(self, "Take over the device?", f"{name} is in use by {who}. Taking it "
                    "disconnects that client.", "Take over",
                    lambda: self.run_action(key, f.attach(sid, busid, True), f"Attached {name} to this PC"))
        elif action == "detach":
            self.run_action(key, f.detach(sid, busid, True), f"Detached {name}")
        elif action == "detach-keep":
            self.run_action(key, f.detach(sid, busid, False), f"Detached {name}, still shared")
        elif action == "bind":
            async def bind() -> Any:
                return await f.bind(sid, busid)

            def bind_fail_force(msg: str) -> None:
                confirm(self, "Share anyway?", msg, "Share anyway",
                        lambda: self.run_action(key, f.bind(sid, busid, True), f"Shared {name}"))

            if d.get("net") or d.get("is_hub"):
                bind_fail_force(f"{name} is a network adapter or a hub. Sharing it can disconnect the setup PC.")
            else:
                self.run_action(key, bind(), f"Shared {name}")
        elif action == "unbind":
            if d.get("state") == "in_use" and not d.get("attached_here"):
                confirm(self, "Stop sharing?", f"{name} is in use by {d.get('used_by_text') or 'another client'}. "
                        "It loses the device.",
                        "Stop sharing", lambda: self.run_action(key, f.unbind(sid, busid), f"Stopped sharing {name}"))
            else:
                self.run_action(key, f.unbind(sid, busid), f"Stopped sharing {name}")
        elif action == "reset":
            self.run_action(key, f.reset(sid, busid))
        elif action == "sticky":
            self.run_action(key, f.set_sticky(sid, busid, not d.get("sticky")))
        elif action == "sticky-off":
            self.run_action(key, f.set_sticky(sid, busid, False))
        elif action == "autobind":
            self.run_action(key, f.set_auto_bind(sid, busid, not d.get("auto_bind")))
        elif action == "serial":
            page = self._page(f"setup:{sid}")
            self.select_setup(sid)
            if isinstance(page, SetupPage):
                page.open_serial(busid)
        elif action == "flash":
            from .flash import FlashDialog
            FlashDialog(self, sid, d).present(self)
        elif action == "rename":
            self._rename(sid, busid, d)
        elif action == "identify":
            from .identify import IdentifyDialog
            IdentifyDialog(self, sid, d).present(self)
        elif action == "copy-busid":
            copy_text(self, busid)
            self.toast("Bus ID copied")
        elif action == "copy-attach":
            s = self.store.get_setup(sid)
            copy_text(self, f"sudo usbip attach -r {s['host'] if s else sid} -b {busid}")
            self.toast("Command copied")

    def _rename(self, sid: str, busid: str, d: dict[str, Any]) -> None:
        entry = Gtk.Entry(text=d.get("nickname") or "", placeholder_text=d["name"], activates_default=True)
        dlg = Adw.AlertDialog(heading="Rename device", body=f"{d['name']} (bus {busid}, {d['vidpid']}). The name is "
                              "stored on the setup PC, so everybody sees it. Empty = no name.")
        dlg.set_extra_child(entry)
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("save", "Save")
        dlg.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)
        dlg.set_default_response("save")

        def response(_d: Any, r: str) -> None:
            if r == "save":
                self.run_action(f"{sid}/{busid}", self.fleet.set_nickname(sid, busid, entry.get_text().strip()))
        dlg.connect("response", response)
        dlg.present(self)
        GLib.idle_add(lambda: (entry.grab_focus(), False)[1])  # type at once

    def after_identify(self, sid: str) -> None:
        """Show the new MCU label next to the device."""
        for page in self.pages.values():
            if isinstance(page, SetupPage) and page.sid == sid:
                page._last_usb = ""
                page.load_usb()
            elif isinstance(page, SearchPage) and page is self.current:
                page.search()

    def detach_port(self, port: int) -> None:
        self.run_action(f"port/{port}", self.fleet.detach_port(port))


class SetupManagerApp(Adw.Application):
    def __init__(self, store: ConfigStore):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.store = store
        self.fleet = Fleet(store)
        self.bridge = AsyncBridge()
        GLib.set_application_name("Setup Manager")

    def do_startup(self) -> None:
        Adw.Application.do_startup(self)
        css = Gtk.CssProvider()
        css.load_from_path(str(HERE / "style.css"))
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), css,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        icon_dir = HERE / "data"
        Gtk.IconTheme.get_for_display(Gdk.Display.get_default()).add_search_path(str(icon_dir))
        self.bridge.start()
        self.bridge.call(self.fleet.start_background())
        for name, cb, accels in (
            ("add-setup", lambda *_: setup_dialog(self.props.active_window), ["<Control>n"]),
            ("discover", lambda *_: discover_dialog(self.props.active_window), []),
            ("refresh", lambda *_: self._refresh_all(), ["<Control>r", "F5"]),
            ("preferences", lambda *_: preferences_dialog(self.props.active_window), ["<Control>comma"]),
            ("about", lambda *_: self._about(), []),
            ("quit", lambda *_: self.quit(), ["<Control>q"]),
        ):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", cb)
            self.add_action(action)
            if accels:
                self.set_accels_for_action(f"app.{name}", accels)

    def do_activate(self) -> None:
        win = self.props.active_window or MainWindow(self)
        win.present()

    def do_shutdown(self) -> None:
        try:
            self.bridge.call(self.fleet.close(), timeout=5)
        except Exception:  # noqa: BLE001
            pass
        self.bridge.stop()
        Adw.Application.do_shutdown(self)

    def _refresh_all(self) -> None:
        win = self.props.active_window
        self.bridge.run(self.fleet.refresh_all(), lambda _r: win.refresh_now() if win else None)

    def _about(self) -> None:
        about = Adw.AboutDialog(application_name="Setup Manager", application_icon=APP_ID, version=__version__,
                                developer_name="Setup Manager contributors", license_type=Gtk.License.MIT_X11,
                                comments="Manage Linux setup PCs: status, USB/IP devices and shells.",
                                website="https://github.com/alonlot/setup-manager-")
        about.present(self.props.active_window)


def run_app(store: ConfigStore) -> int:
    logging.getLogger().setLevel(logging.INFO)
    app = SetupManagerApp(store)
    return app.run([sys.argv[0]])


if __name__ == "__main__":
    sys.exit(run_app(ConfigStore()))
