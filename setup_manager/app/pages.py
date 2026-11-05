"""Content pages: overview, one setup (USB / terminal / run / info / log), search, run on many."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from gi.repository import Adw, GLib, Gtk, Pango

from .widgets import (RunPanel, TerminalTab, clear, device_row, dot, fmt_bytes, fmt_uptime, icon_button, pill,
                      text_view)

if TYPE_CHECKING:
    from .main import MainWindow


def _scrolled(child: Gtk.Widget, clamp: int = 1100) -> Gtk.ScrolledWindow:
    c = Adw.Clamp(maximum_size=clamp, child=child, tightening_threshold=clamp)
    return Gtk.ScrolledWindow(child=c, vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)


def _page_box() -> Gtk.Box:
    return Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18, margin_top=18, margin_bottom=24,
                   margin_start=18, margin_end=18)


class Page:
    title = ""
    widget: Gtk.Widget

    def on_snapshot(self, snap: dict[str, Any]) -> None:
        pass

    def on_shown(self) -> None:
        pass

    def on_hidden(self) -> None:
        pass

    def on_removed(self) -> None:
        """The page goes away for good (its setup was deleted)."""
        self.on_hidden()


# --------------------------------------------------------------------------- overview

class OverviewPage(Page):
    title = "Overview"

    def __init__(self, win: "MainWindow"):
        self.win = win
        self._last = ""
        view = Adw.ToolbarView()
        hb = Adw.HeaderBar()
        hb.set_title_widget(Adw.WindowTitle(title="Overview"))
        view.add_top_bar(hb)
        self.banner = Adw.Banner(revealed=False)
        view.add_top_bar(self.banner)
        self.box = _page_box()
        self.stack = Gtk.Stack()
        self.stack.add_named(_scrolled(self.box), "content")
        empty = Adw.StatusPage(icon_name="computer-symbolic", title="No setups yet",
                               description="Install the agent on each setup PC, then add it here.\n\n"
                                           "From this PC, over SSH:\n"
                                           "<tt>setup-manager deploy user@setup-pc --name bench-1</tt>")
        btns = Gtk.Box(spacing=12, halign=Gtk.Align.CENTER)
        add = Gtk.Button(label="Add setup…", css_classes=["suggested-action", "pill"])
        add.set_action_name("app.add-setup")
        scan = Gtk.Button(label="Scan network…", css_classes=["pill"])
        scan.set_action_name("app.discover")
        btns.append(add)
        btns.append(scan)
        empty.set_child(btns)
        self.stack.add_named(empty, "empty")
        view.set_content(self.stack)
        self.widget = view

    def on_snapshot(self, snap: dict[str, Any]) -> None:
        key = json.dumps([snap["setups"], snap["ports"], snap["sticky"], sorted(self.win.busy),
                          (snap.get("usbip") or {}).get("found"), (snap.get("usbip") or {}).get("error")], default=str)
        if key == self._last:
            return
        self._last = key
        usbip = snap.get("usbip") or {}
        if usbip and (not usbip.get("found") or usbip.get("error")):
            self.banner.set_title(f"usbip client problem on this PC: "
                                  f"{usbip.get('error') or str(usbip.get('command')) + ' not found'}. Attach will not work.")
            self.banner.set_revealed(True)
        else:
            self.banner.set_revealed(False)
        setups = snap["setups"]
        self.stack.set_visible_child_name("content" if setups else "empty")
        clear(self.box)

        online = sum(1 for s in setups if s.get("online"))
        g = Adw.PreferencesGroup(title="Setups", description=f"{online} of {len(setups)} online")
        for s in setups:
            if s.get("online"):
                mem = s.get("memory") or {}
                parts = [s["host"], s.get("os") or ""]
                if s.get("cpu_percent") is not None:
                    parts.append(f"CPU {s['cpu_percent']}%")
                if mem:
                    parts.append(f"RAM {fmt_bytes(mem.get('used'))}/{fmt_bytes(mem.get('total'))}")
                parts.append(f"up {fmt_uptime(s.get('uptime'))}")
                parts.append(f"{s.get('usb_count', 0)} USB, {s.get('usb_bound', 0)} shared, "
                             f"{s.get('usb_in_use', 0)} in use")
                sub = "  ·  ".join(p for p in parts if p)
            else:
                sub = f"{s['host']}  ·  {s.get('error') or 'connecting…'}"
            row = Adw.ActionRow(title=s["name"], subtitle=sub, use_markup=False, activatable=True)
            row.set_subtitle_lines(2)
            row.add_prefix(dot(s.get("online")))
            if s.get("online") and s.get("encrypted") is False:
                row.add_suffix(pill("not encrypted", "red"))
            if s.get("online") and s.get("usbip") and not s["usbip"].get("usbipd_running"):
                row.add_suffix(pill("usbipd off", "amber"))
            row.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
            row.connect("activated", lambda _r, sid=s["id"]: self.win.select_setup(sid))
            g.add(row)
        self.box.append(g)

        g = Adw.PreferencesGroup(title="Attached to this PC")
        if not snap["ports"]:
            g.add(Adw.ActionRow(title="No USB/IP devices are attached to this PC.", css_classes=["dim-label"]))
        for p in snap["ports"]:
            src = p.get("setup_name") or p.get("host")
            row = Adw.ActionRow(title=p.get("name") or f"{p.get('vid')}:{p.get('pid')}", use_markup=False,
                                subtitle=f"from {src}  ·  bus {p.get('busid')}  ·  port {p['port']}  ·  "
                                         f"{p.get('vid')}:{p.get('pid')}  ·  {p.get('speed')}")
            row.add_prefix(Gtk.Image(icon_name="media-removable-symbolic"))
            key_ = f"{p.get('setup')}/{p.get('busid')}" if p.get("setup") else f"port/{p['port']}"
            b = Gtk.Button(label="Detach", valign=Gtk.Align.CENTER, sensitive=key_ not in self.win.busy)
            if p.get("setup"):
                b.connect("clicked", lambda _b, p=p: self.win.usb_action(p["setup"], p["busid"], "detach", {}))
            else:
                b.connect("clicked", lambda _b, p=p: self.win.detach_port(p["port"]))
            row.add_suffix(b)
            g.add(row)
        self.box.append(g)

        if snap["sticky"]:
            g = Adw.PreferencesGroup(title="Keep attached",
                                     description="Attached again when the connection drops (board reset, "
                                                 "re-enumeration). Runs while this app is open.")
            for x in snap["sticky"]:
                setup = self.win.store.get_setup(x["setup"])
                row = Adw.ActionRow(title=x.get("name") or x.get("vidpid") or x["busid"], use_markup=False,
                                    subtitle=f"{setup['name'] if setup else x['setup']}  ·  bus {x['busid']}"
                                             + (f"  ·  {x['error']}" if x.get("error") else ""))
                row.add_prefix(pill("attached" if x.get("ok") else "retrying" if x.get("ok") is False else "waiting",
                                    "green" if x.get("ok") else "red" if x.get("ok") is False else "grey"))
                b = Gtk.Button(label="Turn off", valign=Gtk.Align.CENTER)
                b.connect("clicked", lambda _b, x=x: self.win.usb_action(x["setup"], x["busid"], "sticky-off", {}))
                row.add_suffix(b)
                g.add(row)
            self.box.append(g)


# --------------------------------------------------------------------------- one setup

class SetupPage(Page):
    def __init__(self, win: "MainWindow", sid: str):
        self.win = win
        self.sid = sid
        self.devices: list[dict[str, Any]] | None = None
        self._last_usb = ""
        self._menu_open = False
        self._timer: int | None = None
        self.show_hubs = False

        view = Adw.ToolbarView()
        hb = Adw.HeaderBar()
        self.stack = Adw.ViewStack()
        switcher = Adw.ViewSwitcher(stack=self.stack, policy=Adw.ViewSwitcherPolicy.WIDE)
        hb.set_title_widget(switcher)
        hb.pack_end(self._setup_menu())
        hb.pack_end(icon_button("view-refresh-symbolic", "Refresh", self.refresh))
        view.add_top_bar(hb)
        self.banner = Adw.Banner(revealed=False)
        self.banner.connect("button-clicked", lambda *_: self._banner_action())
        view.add_top_bar(self.banner)
        bottom = Adw.ViewSwitcherBar(stack=self.stack)
        view.add_bottom_bar(bottom)
        self.switcher_bar = bottom

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.summary = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, margin_top=14, margin_start=20,
                               margin_end=20, margin_bottom=6)
        outer.append(self.summary)
        outer.append(self.stack)
        view.set_content(outer)
        self.widget = view

        self.stack.add_titled_with_icon(self._usb_tab(), "usb", "USB", "media-removable-symbolic")
        self.stack.add_titled_with_icon(self._terminal_tab(), "terminal", "Terminal", "utilities-terminal-symbolic")
        from .serial import SerialMonitor
        self.serial = SerialMonitor(win, sid)
        self.stack.add_titled_with_icon(self.serial, "serial", "Serial", "network-transmit-receive-symbolic")
        run_box = _page_box()
        run_box.append(RunPanel(win, lambda: [self.sid]))
        self.stack.add_titled_with_icon(_scrolled(run_box), "run", "Run", "system-run-symbolic")
        self.info_box = _page_box()
        self.stack.add_titled_with_icon(_scrolled(self.info_box), "info", "Info", "dialog-information-symbolic")
        self.stack.add_titled_with_icon(self._log_tab(), "log", "Log", "document-open-recent-symbolic")
        self.stack.connect("notify::visible-child-name", lambda *_: self._tab_changed())

    @property
    def title(self) -> str:  # type: ignore[override]
        s = self.win.store.get_setup(self.sid)
        return s["name"] if s else self.sid

    # -- header --------------------------------------------------------------
    def _setup_menu(self) -> Gtk.MenuButton:
        pop = Gtk.Popover()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, margin_top=4, margin_bottom=4,
                      margin_start=4, margin_end=4)
        for label, cb in (("Edit setup…", self._edit), ("Start usbipd on the setup", self._start_usbipd),
                          ("Remove setup…", self._remove)):
            b = Gtk.Button(child=Gtk.Label(label=label, xalign=0), css_classes=["flat", "menu-item"])
            b.connect("clicked", lambda _b, cb=cb: (pop.popdown(), cb()))
            box.append(b)
        pop.set_child(box)
        return Gtk.MenuButton(icon_name="view-more-symbolic", popover=pop, tooltip_text="Setup actions")

    def _edit(self) -> None:
        from .dialogs import setup_dialog
        s = next((x for x in self.win.snapshot.get("setups", []) if x["id"] == self.sid), None)
        if s:
            setup_dialog(self.win, existing=s)

    def _remove(self) -> None:
        from .dialogs import confirm
        s = self.win.store.get_setup(self.sid)
        if not s:
            return

        def go() -> None:
            self.win.fleet.remove_setup(self.sid)
            self.win.toast(f"Removed {s['name']}")
            self.win.refresh_now(select="overview")
        confirm(self.win, f"Remove {s['name']}?", f"Remove {s['name']} ({s['host']}) from the manager? "
                "The agent on the PC keeps running.", "Remove", go)

    def _start_usbipd(self) -> None:
        self.win.run_action(f"usbipd/{self.sid}", self.win.fleet.start_usbipd(self.sid), "usbipd started")

    def _banner_action(self) -> None:
        self._start_usbipd()

    def _draw_summary(self, s: dict[str, Any]) -> None:
        clear(self.summary)
        row = Gtk.Box(spacing=10)
        row.append(dot(s.get("online")))
        row.append(Gtk.Label(label=s["name"], css_classes=["summary-title"]))
        if s.get("online"):
            row.append(pill("online", "green"))
        elif s.get("online") is False:
            row.append(pill("offline", "red"))
        self.summary.append(row)
        parts = [f"{s['host']}:{s.get('agent_port')}"]
        for val in (s.get("os"), f"up {fmt_uptime(s['uptime'])}" if s.get("uptime") is not None else None,
                    f"CPU {s['cpu_percent']}%" if s.get("cpu_percent") is not None else None,
                    "load " + " ".join(f"{x:.2f}" for x in s["load"]) if s.get("load") else None,
                    f"agent v{s['version']}" if s.get("version") else None):
            if val:
                parts.append(val)
        self.summary.append(Gtk.Label(label="  ·  ".join(parts), xalign=0, css_classes=["dim-label"], wrap=True,
                                      selectable=True))
        if s.get("notes"):
            self.summary.append(Gtk.Label(label=s["notes"], xalign=0, wrap=True))

    def on_snapshot(self, snap: dict[str, Any]) -> None:
        s = next((x for x in snap["setups"] if x["id"] == self.sid), None)
        if not s:
            return
        self._draw_summary(s)
        usbip = s.get("usbip") or {}
        if s.get("online") is False:
            self.banner.set_title(f"Cannot reach the agent: {s.get('error') or ''}")
            self.banner.set_button_label(None)
            self.banner.set_revealed(True)
        elif s.get("online") and s.get("encrypted") is False:
            self.banner.set_title(f"NOT encrypted: TLS is off on this agent (older than 0.4, or installed with "
                                  f"--plain). Fix: setup-manager deploy USER@{s['host']}")
            self.banner.set_button_label(None)
            self.banner.set_revealed(True)
        elif s.get("online") and not usbip.get("usbip_installed", True):
            self.banner.set_title("usbip does not work on this setup: "
                                  + (usbip.get("usbip_hint") or "re-run install.sh with --install-deps") + ".")
            self.banner.set_button_label(None)
            self.banner.set_revealed(True)
        elif s.get("online") and not usbip.get("usbipd_running", True):
            self.banner.set_title("usbipd is not running on this setup. Clients cannot attach devices.")
            self.banner.set_button_label("Start usbipd")
            self.banner.set_revealed(True)
        else:
            self.banner.set_revealed(False)

    # -- lifecycle -------------------------------------------------------------
    def on_shown(self) -> None:
        self._tab_changed()
        if self._timer is None:
            self._timer = GLib.timeout_add(2500, self._tick)

    def on_hidden(self) -> None:
        if self._timer is not None:
            GLib.source_remove(self._timer)
            self._timer = None
        self.serial.set_visible_now(False)

    def on_removed(self) -> None:
        self.on_hidden()
        if self.serial.is_open:
            self.serial.close()
        for i in range(self.tabs.get_n_pages()):
            self.tabs.get_nth_page(i).get_child().close()

    def open_serial(self, busid: str) -> None:
        """Show the Serial tab with the port of this device, and open it."""
        self.stack.set_visible_child_name("serial")
        self.serial.refresh_ports(select_busid=busid, then_open=True)

    def _tick(self) -> bool:
        name = self.stack.get_visible_child_name()
        if name == "usb":
            self.load_usb()
        return True

    def _tab_changed(self) -> None:
        name = self.stack.get_visible_child_name()
        self.serial.set_visible_now(name == "serial" and self.win.current is self)
        if name == "serial":
            self.serial.refresh_ports()
            self.serial.term.grab_focus()
        if name == "usb":
            self.load_usb()
        elif name == "terminal":
            if self.tabs.get_n_pages() == 0:
                self.new_terminal()
            page = self.tabs.get_selected_page()
            if page:
                page.get_child().term.grab_focus()
        elif name == "info":
            self.load_info()
        elif name == "log":
            self.load_log()

    def refresh(self) -> None:
        self.win.bridge.run(self.win.fleet.refresh(self.sid), lambda _r: self.win.refresh_now())
        self._last_usb = ""
        self._tab_changed()

    # -- USB tab ---------------------------------------------------------------
    def _usb_tab(self) -> Gtk.Widget:
        box = _page_box()
        top = Gtk.Box(spacing=12)
        self.filter = Gtk.SearchEntry(placeholder_text="Filter devices", width_chars=28)
        self.filter.connect("search-changed", lambda *_: self.draw_usb(force=True))
        top.append(self.filter)
        hubs = Gtk.CheckButton(label="Show hubs")
        hubs.connect("toggled", lambda b: (setattr(self, "show_hubs", b.get_active()), self.load_usb()))
        top.append(hubs)
        top.append(Gtk.Label(label="Attach = share on the setup + attach to this PC.", hexpand=True, xalign=1,
                             css_classes=["dim-label", "caption"], ellipsize=Pango.EllipsizeMode.END))
        box.append(top)
        self.usb_stack = Gtk.Stack()
        self.usb_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, css_classes=["boxed-list"],
                                    valign=Gtk.Align.START)
        self.usb_stack.add_named(self.usb_list, "list")
        self.usb_empty = Adw.StatusPage(icon_name="media-removable-symbolic", title="No USB devices",
                                        description="Plug a device into this setup PC. It shows here in a few "
                                                    "seconds.")
        self.usb_stack.add_named(self.usb_empty, "empty")
        self.usb_stack.add_named(Gtk.Spinner(spinning=True, halign=Gtk.Align.CENTER, height_request=48), "loading")
        self.usb_stack.set_visible_child_name("loading")
        box.append(self.usb_stack)
        return _scrolled(box)

    def load_usb(self) -> None:
        def done(devs: list[dict[str, Any]]) -> None:
            self.devices = devs
            self.draw_usb()

        def fail(e: Exception) -> None:
            self.devices = None
            self.usb_empty.set_title("Cannot read the USB devices")
            self.usb_empty.set_description(GLib.markup_escape_text(getattr(e, "message", str(e))))
            self.usb_stack.set_visible_child_name("empty")
            self._last_usb = ""

        self.win.bridge.run(self.win.fleet.usb(self.sid, self.show_hubs), done, fail)

    def _set_menu_open(self, is_open: bool) -> None:
        self._menu_open = is_open
        if not is_open:
            GLib.idle_add(lambda: (self.draw_usb(), False)[1])

    def draw_usb(self, force: bool = False) -> None:
        if self.devices is None or (self._menu_open and not force):
            return
        q = self.filter.get_text().strip().lower()
        devs = [d for d in self.devices if not q or q in " ".join(
            str(x or "") for x in (d["name"], d["vidpid"], d.get("serial"), d["busid"], *d.get("ttys", []))).lower()]
        key = json.dumps([devs, sorted(self.win.busy)], default=str)
        if key == self._last_usb and not force:
            return
        self._last_usb = key
        clear(self.usb_list)
        if not devs:
            self.usb_empty.set_title("No matching devices" if q else "No USB devices")
            self.usb_empty.set_description("Change the filter." if q else
                                           "Plug a device into this setup PC. It shows here in a few seconds.")
            self.usb_stack.set_visible_child_name("empty")
            return
        for d in devs:
            self.usb_list.append(device_row(self.win, self.sid, d, on_menu=self._set_menu_open))
        self.usb_stack.set_visible_child_name("list")

    # -- terminal tab ----------------------------------------------------------
    def _terminal_tab(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.tabs = Adw.TabView(vexpand=True)
        self.tabs.connect("close-page", self._close_tab)
        bar = Adw.TabBar(view=self.tabs, autohide=False)
        add = Gtk.Button(icon_name="tab-new-symbolic", tooltip_text="New terminal tab", css_classes=["flat"])
        add.connect("clicked", lambda *_: self.new_terminal())
        bar.set_end_action_widget(add)
        hint = Gtk.Label(label="Ctrl+Shift+C / Ctrl+Shift+V to copy / paste  ·  Ctrl +/- to zoom",
                         css_classes=["dim-label", "caption"], margin_top=4, margin_bottom=4)
        box.append(bar)
        box.append(self.tabs)
        box.append(hint)
        return box

    def new_terminal(self) -> None:
        tab = TerminalTab(self.win, self.sid)
        page = self.tabs.append(tab)
        n = self.tabs.get_n_pages()
        page.set_title(self.title if n == 1 else f"{self.title} ({n})")
        page.set_icon(Gio_icon("utilities-terminal-symbolic"))
        tab.page = page
        self.tabs.set_selected_page(page)
        tab.term.grab_focus()

    def _close_tab(self, view: Adw.TabView, page: Adw.TabPage) -> bool:
        page.get_child().close()
        view.close_page_finish(page, True)
        return True  # Gdk.EVENT_STOP: we called close_page_finish ourselves

    # -- info / log tabs -------------------------------------------------------
    def load_info(self) -> None:
        def done(st: dict[str, Any]) -> None:
            clear(self.info_box)
            mem, disk = st["memory"], st["disk"]

            def group(title: str, rows: list[tuple[str, Any]]) -> Adw.PreferencesGroup:
                g = Adw.PreferencesGroup(title=title)
                for k, v in rows:
                    if isinstance(v, Gtk.Widget):
                        r = Adw.ActionRow(title=k, use_markup=False)
                        r.add_suffix(v)
                    else:
                        # "property" style: small key on top, the value below it.
                        r = Adw.ActionRow(title=k, subtitle=str(v), use_markup=False, subtitle_selectable=True,
                                          css_classes=["property"])
                    g.add(r)
                return g

            def level(frac: float, text: str) -> Gtk.Widget:
                b = Gtk.Box(spacing=10, valign=Gtk.Align.CENTER)
                lb = Gtk.LevelBar(min_value=0, max_value=1, value=max(0.0, min(1.0, frac)), width_request=220,
                                  valign=Gtk.Align.CENTER)
                b.append(lb)
                b.append(Gtk.Label(label=text, css_classes=["dim-label"], width_chars=16, xalign=1))
                return b

            self.info_box.append(group("System", [
                ("Hostname", st["hostname"]), ("OS", st["os"]), ("Kernel", f"{st['kernel']} ({st['arch']})"),
                ("Uptime", fmt_uptime(st.get("uptime"))), ("CPUs", st.get("cpu_count")),
                ("Load", " / ".join(f"{x:.2f}" for x in st["load"]) if st.get("load") else "–"),
                ("Agent", f"v{st['version']}, runs as {st['agent_user']}, up {fmt_uptime(st.get('agent_uptime'))}"),
                ("Shell user", st.get("run_as")),
            ]))
            self.info_box.append(group("Resources", [
                ("CPU", level((st.get("cpu_percent") or 0) / 100, f"{st.get('cpu_percent') or 0}%")),
                ("Memory", level(mem["used"] / max(1, mem["total"]),
                                 f"{fmt_bytes(mem['used'])} / {fmt_bytes(mem['total'])}")),
                ("Disk /", level(disk["used"] / max(1, disk["total"]), f"{fmt_bytes(disk['free'])} free")),
            ] + [(t["zone"], f"{t['celsius']:.1f} °C") for t in st.get("temperatures", [])]))
            self.info_box.append(group("Network", [
                (i.get("iface") or "ip", f"{i['addr']}/{i['prefix']}" if i.get("prefix") else i["addr"])
                for i in st.get("ips", [])] or [("Addresses", "none")]))
            u = st["usbip"]
            self.info_box.append(group("USB/IP", [
                ("usbip installed", ("yes" if u["usbip_installed"] else "NO")
                 + (f" ({u['usbip_hint']})" if u.get("usbip_hint") else "")),
                ("usbipd running", "yes" if u["usbipd_running"] else "NO"),
                ("usbip-host module", "loaded" if u["module_loaded"] else "not loaded"),
                ("Devices", f"{st['usb_count']} ({st['usb_bound']} shared, {st['usb_in_use']} in use)"),
            ]))

        def fail(e: Exception) -> None:
            clear(self.info_box)
            self.info_box.append(Adw.StatusPage(icon_name="network-offline-symbolic", title="No data",
                                                description=GLib.markup_escape_text(str(e))))

        self.win.bridge.run(self.win.fleet.status(self.sid), done, fail)

    def _log_tab(self) -> Gtk.Widget:
        box = _page_box()
        top = Gtk.Box(spacing=8)
        top.append(Gtk.Button(label="Refresh", halign=Gtk.Align.START))
        top.get_first_child().connect("clicked", lambda *_: self.load_log())
        box.append(top)
        self.log_view = Gtk.TextView(editable=False, monospace=True, wrap_mode=Gtk.WrapMode.WORD_CHAR,
                                     top_margin=10, bottom_margin=10, left_margin=12, right_margin=12,
                                     css_classes=["card"], vexpand=True)
        box.append(self.log_view)
        return _scrolled(box)

    def load_log(self) -> None:
        def done(r: dict[str, Any]) -> None:
            lines = [x["msg"] for x in r.get("lines", [])]
            self.log_view.get_buffer().set_text("\n".join(lines) if lines else "(empty)")

        self.win.bridge.run(self.win.fleet.agent_log(self.sid, 500), done,
                            lambda e: self.log_view.get_buffer().set_text(str(e)))


def Gio_icon(name: str) -> Any:
    from gi.repository import Gio
    return Gio.ThemedIcon.new(name)


# --------------------------------------------------------------------------- search

class SearchPage(Page):
    title = "Find device"

    def __init__(self, win: "MainWindow"):
        self.win = win
        self.results: list[dict[str, Any]] = []
        self._menu_open = False
        self._timer: int | None = None
        view = Adw.ToolbarView()
        hb = Adw.HeaderBar()
        self.entry = Gtk.SearchEntry(placeholder_text="Name, vid:pid, serial or tty on all setups", width_chars=40)
        self.entry.connect("search-changed", lambda *_: self.search())
        hb.set_title_widget(self.entry)
        view.add_top_bar(hb)
        box = _page_box()
        self.list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, css_classes=["boxed-list"],
                                valign=Gtk.Align.START)
        self.status = Adw.StatusPage(icon_name="system-search-symbolic", title="Find a USB device",
                                     description="Search all online setups, for example “st-link”, “0483”, "
                                                 "“ttyACM” or a serial number.")
        self.stack = Gtk.Stack()
        self.stack.add_named(self.list, "list")
        self.stack.add_named(self.status, "status")
        self.stack.set_visible_child_name("status")
        box.append(self.stack)
        view.set_content(_scrolled(box))
        self.widget = view

    def on_shown(self) -> None:
        # After the sidebar click has finished, else the sidebar keeps the focus.
        GLib.idle_add(lambda: (self.entry.grab_focus(), False)[1])
        self.search()

    def search(self) -> None:
        q = self.entry.get_text().strip()

        def done(devs: list[dict[str, Any]]) -> None:
            if q != self.entry.get_text().strip():
                return
            self.results = devs
            self.draw()

        self.win.bridge.run(self.win.fleet.find_usb(q), done, lambda e: self.win.toast(str(e)))

    def _menu(self, is_open: bool) -> None:
        self._menu_open = is_open

    def draw(self) -> None:
        clear(self.list)
        if not self.results:
            self.status.set_title("Nothing found" if self.entry.get_text().strip() else "No devices")
            self.stack.set_visible_child_name("status")
            return
        for d in self.results:
            self.list.append(device_row(self.win, d["setup"], d, show_setup=True, on_menu=self._menu))
        self.stack.set_visible_child_name("list")


# --------------------------------------------------------------------------- run on many

class RunManyPage(Page):
    title = "Run on many"

    def __init__(self, win: "MainWindow"):
        self.win = win
        self.checks: dict[str, Gtk.CheckButton] = {}
        view = Adw.ToolbarView()
        hb = Adw.HeaderBar()
        hb.set_title_widget(Adw.WindowTitle(title="Run on many setups",
                                            subtitle="One command on the selected setups at the same time"))
        view.add_top_bar(hb)
        box = _page_box()
        self.picks = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, max_children_per_line=8,
                                 column_spacing=6, row_spacing=6)
        box.append(self.picks)
        box.append(RunPanel(win, lambda: [sid for sid, c in self.checks.items() if c.get_active()]))
        view.set_content(_scrolled(box))
        self.widget = view

    def on_snapshot(self, snap: dict[str, Any]) -> None:
        ids = [s["id"] for s in snap["setups"]]
        if ids == list(self.checks):
            return
        old = {k: c.get_active() for k, c in self.checks.items()}
        self.picks.remove_all()
        self.checks = {}
        for s in snap["setups"]:
            c = Gtk.CheckButton(label=s["name"], active=old.get(s["id"], bool(s.get("online"))))
            self.checks[s["id"]] = c
            self.picks.append(c)


__all__ = ["OverviewPage", "SetupPage", "SearchPage", "RunManyPage", "text_view"]
