"""Reusable widgets: USB device rows, the command runner and the terminal tabs."""

from __future__ import annotations

import os
import signal
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Vte", "3.91")
from gi.repository import Adw, Gdk, GLib, Gtk, Pango, Vte  # noqa: E402

if TYPE_CHECKING:
    from .main import MainWindow


# --------------------------------------------------------------------------- small helpers

def fmt_bytes(n: float | None) -> str:
    if n is None:
        return "–"
    units = ["B", "KB", "MB", "GB", "TB"]
    i = 0
    while n >= 1024 and i < len(units) - 1:
        n /= 1024
        i += 1
    return f"{n:.0f} {units[i]}" if n >= 10 or i == 0 else f"{n:.1f} {units[i]}"


def fmt_uptime(s: float | None) -> str:
    if s is None:
        return "–"
    d, h, m = int(s // 86400), int(s % 86400 // 3600), int(s % 3600 // 60)
    return f"{d}d {h}h" if d else f"{h}h {m}m" if h else f"{m}m"


def pill(text: str, color: str) -> Gtk.Label:
    lbl = Gtk.Label(label=text, valign=Gtk.Align.CENTER, css_classes=["sm-pill", color])
    return lbl


def dot(online: bool | None) -> Gtk.Label:
    color = "green" if online else "red" if online is False else "grey"
    return Gtk.Label(label="●", css_classes=["dot", color], valign=Gtk.Align.CENTER)


def icon_button(icon: str, tooltip: str, cb: Callable[..., Any], *classes: str) -> Gtk.Button:
    b = Gtk.Button(icon_name=icon, tooltip_text=tooltip, valign=Gtk.Align.CENTER, css_classes=list(classes))
    b.connect("clicked", lambda *_: cb())
    return b


def clear(box: Gtk.Widget) -> None:
    child = box.get_first_child()
    while child is not None:
        nxt = child.get_next_sibling()
        if isinstance(box, Gtk.ListBox):
            box.remove(child)
        else:
            box.remove(child)  # type: ignore[attr-defined]
        child = nxt


def text_view(text: str, error: bool = False) -> Gtk.Widget:
    tv = Gtk.TextView(editable=False, cursor_visible=False, monospace=True, wrap_mode=Gtk.WrapMode.WORD_CHAR,
                      top_margin=8, bottom_margin=8, left_margin=12, right_margin=12,
                      css_classes=["output"] + (["err"] if error else []))
    tv.get_buffer().set_text(text)
    sw = Gtk.ScrolledWindow(child=tv, propagate_natural_height=True, max_content_height=380,
                            hscrollbar_policy=Gtk.PolicyType.NEVER)
    return sw


def copy_text(widget: Gtk.Widget, text: str) -> None:
    widget.get_clipboard().set(text)


# --------------------------------------------------------------------------- USB device rows

def device_state(d: dict[str, Any]) -> tuple[str, str]:
    if d.get("attached_here"):
        return "purple", "Attached here"
    if d["state"] == "in_use":
        return "amber", "In use elsewhere"
    if d["state"] == "bound":
        return "blue", "Shared"
    if d["state"] == "error":
        return "red", "USB/IP error"
    return "grey", "Not shared"


def device_row(win: "MainWindow", sid: str, d: dict[str, Any], show_setup: bool = False,
               on_menu: Callable[[bool], None] | None = None) -> Adw.ActionRow:
    """One USB device: state, name, details, the main button and a menu with all actions."""
    busy = f"{sid}/{d['busid']}" in win.busy
    parts = []
    if d["state"] == "in_use" and not d.get("attached_here"):
        parts.append(f"in use by {d.get('used_by_text') or 'another client'}")
    if d.get("nickname"):
        parts.append(d["name"])
    parts.append(d["vidpid"])
    if d.get("serial"):
        parts.append(f"SN {d['serial']}")
    parts.append(f"bus {d['busid']}")
    parts += [f"/dev/{t}" for t in d.get("ttys", [])]
    parts += [f"net {n}" for n in d.get("net", [])]
    drivers = sorted({i["driver"] for i in d.get("interfaces", []) if i.get("driver") not in (None, "usbip-host")})
    parts += drivers
    title = d.get("nickname") or d["name"]
    if show_setup:
        title = f"{d.get('setup_name') or sid}  ·  {title}"
    row = Adw.ActionRow(title=title, subtitle="  ·  ".join(parts), use_markup=False)
    row.set_subtitle_lines(2)
    color, label = device_state(d)
    st = pill(label, color)
    st.set_tooltip_text(d.get("state_detail") or "")
    st.set_size_request(118, -1)
    row.add_prefix(st)

    from .identify import mcu_pill  # here, to avoid an import cycle
    mp = mcu_pill(win, sid, d)
    if mp is not None:
        row.add_suffix(mp)

    for flag, cls, icon, tip in (("sticky", "sticky", "view-pin-symbolic", "Keep attached"),
                                 ("auto_bind", "auto", "emblem-synchronizing-symbolic", "Auto-share when plugged"),
                                 ("protected", "lock", "changes-prevent-symbolic", "Protected on the setup PC")):
        if d.get(flag):
            row.add_suffix(Gtk.Image(icon_name=icon, tooltip_text=tip, css_classes=["flag-on", cls]))

    busid = d["busid"]
    if d.get("attached_here"):
        primary = Gtk.Button(label="Detach", tooltip_text="Detach from this PC and stop sharing")
        primary.connect("clicked", lambda *_: win.usb_action(sid, busid, "detach", d))
    elif d["state"] == "in_use":
        primary = Gtk.Button(label="Take over", tooltip_text="Another client uses it. Take it to this PC.")
        primary.connect("clicked", lambda *_: win.usb_action(sid, busid, "take", d))
    else:
        primary = Gtk.Button(label="Attach", css_classes=["suggested-action"],
                             tooltip_text="Share on the setup and attach to this PC")
        primary.connect("clicked", lambda *_: win.usb_action(sid, busid, "attach", d))
        if d.get("protected") and not d.get("bound"):
            primary.set_sensitive(False)
            primary.set_tooltip_text("Protected on the setup PC")
    primary.set_valign(Gtk.Align.CENTER)
    primary.set_size_request(96, -1)
    if busy:
        primary.set_sensitive(False)
        primary.set_child(Gtk.Spinner(spinning=True))
    row.add_suffix(primary)

    # The "more" menu.
    pop = Gtk.Popover(has_arrow=True, position=Gtk.PositionType.BOTTOM)
    menu = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, margin_top=4, margin_bottom=4,
                   margin_start=4, margin_end=4)
    pop.set_child(menu)

    def item(text: str, action: str, icon: str | None = None, checked: bool | None = None) -> None:
        box = Gtk.Box(spacing=10)
        box.append(Gtk.Image(icon_name=icon or "", pixel_size=16))
        box.append(Gtk.Label(label=text, xalign=0, hexpand=True))
        if checked:
            box.append(Gtk.Image(icon_name="object-select-symbolic"))
        b = Gtk.Button(child=box, css_classes=["flat", "menu-item"])

        def go(*_: Any) -> None:
            pop.popdown()
            win.usb_action(sid, busid, action, d)
        b.connect("clicked", go)
        menu.append(b)

    if d.get("bound"):
        item("Stop sharing (usbip unbind)", "unbind", "media-eject-symbolic")
    else:
        item("Share only (usbip bind)", "bind", "send-to-symbolic")
    if d.get("attached_here"):
        item("Detach, keep shared", "detach-keep", "edit-undo-symbolic")
    menu.append(Gtk.Separator())
    item("Keep attached here", "sticky", "view-pin-symbolic", bool(d.get("sticky")))
    item("Auto-share when plugged", "autobind", "emblem-synchronizing-symbolic", bool(d.get("auto_bind")))
    menu.append(Gtk.Separator())
    item("Serial monitor", "serial", "network-transmit-receive-symbolic")
    item("Flash firmware…", "flash", "document-send-symbolic")
    item("Identify MCU…", "identify", "dialog-question-symbolic")
    item("Rename…", "rename", "document-edit-symbolic")
    item("USB reset (re-plug)", "reset", "view-refresh-symbolic")
    item("Copy bus ID", "copy-busid", "edit-copy-symbolic")
    item("Copy usbip attach command", "copy-attach", "edit-copy-symbolic")

    more = Gtk.MenuButton(icon_name="view-more-symbolic", popover=pop, valign=Gtk.Align.CENTER,
                          css_classes=["flat"], tooltip_text="More actions")
    if on_menu:
        pop.connect("show", lambda *_: on_menu(True))
        pop.connect("closed", lambda *_: on_menu(False))
    row.add_suffix(more)
    return row


# --------------------------------------------------------------------------- command runner

class RunPanel(Gtk.Box):
    """Command entry + quick commands + results. targets() gives the setup ids to run on."""

    def __init__(self, win: "MainWindow", targets: Callable[[], list[str]]):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.win = win
        self.targets = targets
        self.results: list[dict[str, Any]] = []

        form = Gtk.Box(spacing=8)
        self.entry = Gtk.Entry(placeholder_text="Command, e.g. ls -l /dev/ttyACM*", hexpand=True)
        self.entry.add_css_class("monospace")
        self.entry.connect("activate", lambda *_: self.run())
        self.timeout = Gtk.SpinButton.new_with_range(1, 3600, 1)
        self.timeout.set_value(60)
        self.timeout.set_tooltip_text("Timeout in seconds")
        self.user = Gtk.Entry(placeholder_text="user (default)", width_chars=12,
                              tooltip_text="Run as this user. Empty = the setup default user.")
        self.button = Gtk.Button(label="Run", css_classes=["suggested-action"])
        self.button.connect("clicked", lambda *_: self.run())
        for w in (self.entry, self.timeout, self.user, self.button):
            form.append(w)
        self.append(form)

        self.snippets = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, column_spacing=6, row_spacing=6,
                                    max_children_per_line=20)
        self.append(self.snippets)
        self._draw_snippets()

        self.list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.append(self.list)

    def _draw_snippets(self) -> None:
        self.snippets.remove_all()
        for cmd in self.win.store.data().get("snippets") or []:
            b = Gtk.Button(label=cmd, css_classes=["pill", "chip"], tooltip_text="Click to use. Right-click to remove.")
            b.connect("clicked", lambda _b, c=cmd: (self.entry.set_text(c), self.entry.grab_focus()))
            click = Gtk.GestureClick(button=3)
            click.connect("pressed", lambda *_a, c=cmd: self._remove_snippet(c))
            b.add_controller(click)
            self.snippets.append(b)
        save = Gtk.Button(label="+ Save current", css_classes=["flat"])
        save.connect("clicked", lambda *_: self._save_snippet())
        self.snippets.append(save)

    def _save_snippet(self) -> None:
        cmd = self.entry.get_text().strip()
        if not cmd:
            return
        data = self.win.store.data()
        if cmd not in data.setdefault("snippets", []):
            data["snippets"].append(cmd)
            self.win.store.save()
        self._draw_snippets()

    def _remove_snippet(self, cmd: str) -> None:
        data = self.win.store.data()
        data["snippets"] = [c for c in data.get("snippets", []) if c != cmd]
        self.win.store.save()
        self._draw_snippets()

    def run(self) -> None:
        command = self.entry.get_text().strip()
        targets = self.targets()
        if not command:
            return
        if not targets:
            self.win.toast("Select at least one setup")
            return
        timeout = self.timeout.get_value()
        user = self.user.get_text().strip() or None
        self.button.set_sensitive(False)
        self.button.set_child(Gtk.Spinner(spinning=True))
        fleet = self.win.fleet

        async def go() -> list[dict[str, Any]]:
            if len(targets) == 1:
                try:
                    return [await fleet.exec(targets[0], command, timeout, None, user)]
                except Exception as e:  # noqa: BLE001
                    return [{"setup": targets[0], "command": command, "error": getattr(e, "message", str(e))}]
            return await fleet.exec_many(targets, command, timeout, user=user)

        def done(results: list[dict[str, Any]]) -> None:
            self.button.set_sensitive(True)
            self.button.set_label("Run")
            for r in results:
                r.setdefault("command", command)
            self.results = (results + self.results)[:30]
            self.draw()

        self.win.bridge.run(go(), done, lambda e: (done([]), self.win.toast(str(e))))

    def draw(self) -> None:
        clear(self.list)
        for r in self.results:
            card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, css_classes=["card", "result-card"])
            head = Gtk.Box(spacing=10, css_classes=["result-head"])
            if r.get("error"):
                head.append(pill("error", "red"))
            elif r.get("timed_out"):
                head.append(pill("timeout", "amber"))
            elif r.get("exit_code") == 0:
                head.append(pill("exit 0", "green"))
            else:
                head.append(pill(f"exit {r.get('exit_code')}", "red"))
            s = self.win.store.get_setup(r["setup"])
            head.append(Gtk.Label(label=s["name"] if s else r["setup"], css_classes=["heading"]))
            cmd = Gtk.Label(label=f"$ {r['command']}", xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.END,
                            css_classes=["monospace", "dim-label"])
            head.append(cmd)
            if r.get("duration") is not None:
                head.append(Gtk.Label(label=f"{r['duration']}s", css_classes=["dim-label"]))
            text = r.get("stdout") or r.get("error") or ""
            head.append(icon_button("edit-copy-symbolic", "Copy output", lambda t=text: copy_text(self, t), "flat"))
            card.append(head)
            card.append(Gtk.Separator())
            if r.get("error"):
                card.append(text_view(r["error"], error=True))
            if r.get("stdout"):
                card.append(text_view(r["stdout"]))
            if r.get("stderr"):
                card.append(text_view(r["stderr"], error=True))
            if not (r.get("error") or r.get("stdout") or r.get("stderr")):
                card.append(Gtk.Label(label="(no output)", css_classes=["dim-label"], margin_top=8, margin_bottom=8))
            self.list.append(card)


# --------------------------------------------------------------------------- terminal tabs

def shell_argv(config_path: Path, sid: str) -> tuple[list[str], list[str]]:
    """The command that a terminal tab runs, and its environment."""
    pkg_parent = str(Path(__file__).resolve().parent.parent.parent)
    env = dict(os.environ)
    env["PYTHONPATH"] = pkg_parent + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    env["TERM"] = "xterm-256color"
    argv = [sys.executable, "-m", "setup_manager", "--config", str(config_path), "shell", sid]
    return argv, [f"{k}={v}" for k, v in env.items()]


def _rgba(spec: str) -> Gdk.RGBA:
    c = Gdk.RGBA()
    c.parse(spec)
    return c


class TerminalTab(Gtk.Box):
    """A VTE terminal running `setup-manager shell <setup>`, with a reconnect bar."""

    def __init__(self, win: "MainWindow", sid: str):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.win = win
        self.sid = sid
        self.page: Adw.TabPage | None = None
        self.running = False
        self.pid: int | None = None

        self.bar = Gtk.Revealer(transition_type=Gtk.RevealerTransitionType.SLIDE_DOWN)
        bar_box = Gtk.Box(spacing=10, css_classes=["term-bar"])
        self.bar_label = Gtk.Label(label="Disconnected", xalign=0, hexpand=True)
        bar_box.append(self.bar_label)
        rb = Gtk.Button(label="Reconnect", css_classes=["suggested-action"])
        rb.connect("clicked", lambda *_: self.start())
        bar_box.append(rb)
        self.bar.set_child(bar_box)
        self.append(self.bar)

        self.term = Vte.Terminal(vexpand=True, hexpand=True)
        self.term.set_scrollback_lines(20000)
        self.term.set_mouse_autohide(True)
        self.term.set_font(Pango.FontDescription.from_string("Monospace 11"))
        self.term.set_enable_fallback_scrolling(True) if hasattr(self.term, "set_enable_fallback_scrolling") else None
        self.term.connect("child-exited", self._exited)
        self.term.connect("window-title-changed", self._title_changed)
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._key)
        self.term.add_controller(keys)
        self._apply_colors()
        # The style manager is a global singleton: close() must disconnect this, else it keeps the tab alive.
        self._style_handler: int | None = Adw.StyleManager.get_default().connect(
            "notify::dark", lambda *_: self._apply_colors())
        self.append(self.term)
        self.start()

    def _apply_colors(self) -> None:
        if Adw.StyleManager.get_default().get_dark():
            fg, bg = _rgba("#deddda"), _rgba("#1e1e1e")
        else:
            fg, bg = _rgba("#241f31"), _rgba("#fafafa")
        palette = [_rgba(c) for c in (
            "#241f31", "#c01c28", "#2ec27e", "#f5c211", "#1e78e4", "#9841bb", "#0ab9dc", "#c0bfbc",
            "#5e5c64", "#ed333b", "#57e389", "#f8e45c", "#51a1ff", "#c061cb", "#4fd2fd", "#f6f5f4")]
        self.term.set_colors(fg, bg, palette)

    def start(self) -> None:
        """Start (or restart) the shell client. Not named connect(): that is GObject.Object.connect."""
        if self.running:
            return
        argv, env = shell_argv(self.win.store.path, self.sid)
        self.term.reset(True, True)
        self.bar.set_reveal_child(False)
        self.running = True
        self.term.spawn_async(Vte.PtyFlags.DEFAULT, os.path.expanduser("~"), argv, env, GLib.SpawnFlags.DEFAULT,
                              None, None, -1, None, self._spawned)
        self.term.grab_focus()

    def _spawned(self, term: Vte.Terminal, pid: int, error: GLib.Error | None, *args: Any) -> None:
        if error is not None:
            self.running = False
            self.bar_label.set_label(f"Cannot start the shell client: {error.message}")
            self.bar.set_reveal_child(True)
            return
        self.pid = pid

    def close(self) -> None:
        """The tab goes away: end the shell client and drop the global signal handler."""
        if self._style_handler is not None:
            Adw.StyleManager.get_default().disconnect(self._style_handler)
            self._style_handler = None
        pid, self.pid = self.pid, None
        if pid:
            try:
                os.kill(pid, signal.SIGHUP)
            except ProcessLookupError:
                pass

    def _exited(self, term: Vte.Terminal, status: int) -> None:
        self.running = False
        self.pid = None
        self.bar_label.set_label("Disconnected from the setup.")
        self.bar.set_reveal_child(True)
        if self.page:
            self.page.set_indicator_icon(None)

    def _title_changed(self, term: Vte.Terminal) -> None:
        title = term.get_window_title() if hasattr(term, "get_window_title") else None
        if self.page and title:
            self.page.set_title(title)

    def _key(self, ctrl: Gtk.EventControllerKey, keyval: int, keycode: int, state: Gdk.ModifierType) -> bool:
        ctrl_shift = Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.SHIFT_MASK
        if state & ctrl_shift == ctrl_shift:
            name = Gdk.keyval_name(keyval) or ""
            if name in ("C", "c"):
                self.term.copy_clipboard_format(Vte.Format.TEXT)
                return True
            if name in ("V", "v"):
                self.term.paste_clipboard()
                return True
        if state & Gdk.ModifierType.CONTROL_MASK:
            name = Gdk.keyval_name(keyval) or ""
            if name in ("plus", "equal", "KP_Add"):
                self.term.set_font_scale(min(3.0, self.term.get_font_scale() + 0.1))
                return True
            if name in ("minus", "KP_Subtract"):
                self.term.set_font_scale(max(0.5, self.term.get_font_scale() - 0.1))
                return True
            if name == "0":
                self.term.set_font_scale(1.0)
                return True
        return False
