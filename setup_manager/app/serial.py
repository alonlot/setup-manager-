"""Serial monitor tab: a live view of a device's serial port, with input, reset, timestamps and save."""

from __future__ import annotations

import codecs
import time
from typing import TYPE_CHECKING, Any

from gi.repository import Gio, GLib, Gtk, Pango, Vte

from .widgets import _rgba

if TYPE_CHECKING:
    from .main import MainWindow

BAUDS = ["115200", "9600", "19200", "38400", "57600", "74880", "230400", "460800", "921600", "1000000",
         "1500000", "2000000", "3000000"]
EOLS = [("CR", b"\r"), ("LF", b"\n"), ("CR LF", b"\r\n")]
MAX_SAVED = 16 * 1024 * 1024


class SerialMonitor(Gtk.Box):
    def __init__(self, win: "MainWindow", sid: str):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.win, self.sid = win, sid
        self.ports: list[dict[str, Any]] = []
        self.port: dict[str, Any] | None = None
        self.is_open = False
        self.visible = False
        self.polling = False
        self.offset = 0
        self.seen_events: set[tuple] = set()
        self.saved = bytearray()
        self._reset_decoder()

        bar = Gtk.Box(spacing=6, margin_top=8, margin_bottom=8, margin_start=12, margin_end=12)
        self.port_dd = Gtk.DropDown.new_from_strings(["(no serial port)"])
        self.port_dd.set_tooltip_text("Serial port")
        self.port_dd.set_size_request(280, -1)
        self.port_dd.connect("notify::selected", lambda *_: self._port_changed())
        bar.append(self.port_dd)
        self.baud_dd = Gtk.DropDown.new_from_strings(BAUDS)
        self.baud_dd.set_tooltip_text("Baud rate")
        self.baud_dd.connect("notify::selected", lambda *_: self._baud_changed())
        bar.append(self.baud_dd)
        self.open_btn = Gtk.Button(label="Open", css_classes=["suggested-action"])
        self.open_btn.connect("clicked", lambda *_: self.close() if self.is_open else self.open())
        bar.append(self.open_btn)
        reset = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Reset the board (method: auto)")
        reset.connect("clicked", lambda *_: self.reset())
        bar.append(reset)
        clear = Gtk.Button(icon_name="edit-clear-all-symbolic", tooltip_text="Clear the view")
        clear.connect("clicked", lambda *_: self.term.reset(True, True))
        bar.append(clear)
        save = Gtk.Button(icon_name="document-save-symbolic", tooltip_text="Save the log to a file")
        save.connect("clicked", lambda *_: self.save())
        bar.append(save)
        self.ts = Gtk.CheckButton(label="Timestamps")
        bar.append(self.ts)
        self.eol_dd = Gtk.DropDown.new_from_strings([e[0] for e in EOLS])
        self.eol_dd.set_tooltip_text("What Enter sends")
        bar.append(self.eol_dd)
        self.status = Gtk.Label(xalign=1, hexpand=True, ellipsize=Pango.EllipsizeMode.END,
                                css_classes=["dim-label", "caption"])
        bar.append(self.status)
        self.append(bar)

        self.term = Vte.Terminal(vexpand=True, hexpand=True)
        self.term.set_scrollback_lines(100000)
        self.term.set_font(Pango.FontDescription.from_string("Monospace 10.5"))
        self.term.set_colors(_rgba("#deddda"), _rgba("#16181d"), None)
        self.term.connect("commit", self._typed)
        self.append(self.term)

        send = Gtk.Box(spacing=6, margin_top=6, margin_bottom=8, margin_start=12, margin_end=12)
        self.line = Gtk.Entry(placeholder_text="Type a line and press Enter (or type in the window above)",
                              hexpand=True, css_classes=["monospace"])
        self.line.connect("activate", lambda *_: self._send_line())
        send.append(self.line)
        b = Gtk.Button(label="Send")
        b.connect("clicked", lambda *_: self._send_line())
        send.append(b)
        self.append(send)
        self._status("Select a port and click Open. Devices that are shared over USB/IP are not here.")

    # -- helpers -------------------------------------------------------------------
    def _reset_decoder(self) -> None:
        self.decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self.line_start = True

    def _status(self, text: str) -> None:
        self.status.set_label(text)
        self.status.set_tooltip_text(text)

    def _note(self, text: str) -> None:
        self.term.feed(f"\r\n\x1b[90m[{time.strftime('%H:%M:%S')} {text}]\x1b[0m\r\n".encode())
        self.line_start = True

    def _baud(self) -> int:
        return int(BAUDS[self.baud_dd.get_selected()])

    def _eol(self) -> bytes:
        return EOLS[self.eol_dd.get_selected()][1]

    # -- ports -----------------------------------------------------------------------
    def refresh_ports(self, select_busid: str | None = None, then_open: bool = False) -> None:
        def done(ports: list[dict[str, Any]]) -> None:
            self.ports = ports
            labels = [f"{p['tty']}  ·  {p['name']}{'  (this PC)' if p['where'] == 'here' else ''}" for p in ports]
            cur = self.port
            self.port_dd.set_model(Gtk.StringList.new(labels or ["(no serial port)"]))
            idx = 0
            for i, p in enumerate(ports):
                if (select_busid and p["busid"] == select_busid) or (
                        not select_busid and cur and p["busid"] == cur["busid"] and p["tty"] == cur["tty"]):
                    idx = i
                    break
            if ports:
                self.port_dd.set_selected(idx)
            self.port = ports[idx] if ports else None
            if then_open and self.port and not self.is_open:
                self.open()

        self.win.bridge.run(self.win.fleet.serial_ports(self.sid), done, lambda e: self._status(str(e)))

    def _port_changed(self) -> None:
        i = self.port_dd.get_selected()
        new = self.ports[i] if 0 <= i < len(self.ports) else None
        if new and self.port and new["busid"] == self.port["busid"] and new["tty"] == self.port["tty"]:
            return
        was_open = self.is_open
        self.is_open = False
        self.port = new
        self.term.reset(True, True)
        self.saved.clear()
        self._set_open_button()
        if was_open and new:
            self.open()

    def _baud_changed(self) -> None:
        if self.is_open:
            self.open()  # the same port again, with the new rate

    # -- open / read / close -------------------------------------------------------
    def open(self) -> None:
        if not self.port:
            self.win.toast("No serial port on this setup (or the devices are shared over USB/IP)")
            return
        p = self.port

        def done(h: dict[str, Any]) -> None:
            first = not self.is_open
            self.is_open = True
            self._set_open_button()
            if first:
                self.offset = 0  # show what the port already recorded
                self.seen_events.clear()
                self._reset_decoder()
                self._note(f"{p['tty']} at {h['baud']} baud ({'this PC' if h['where'] == 'here' else self.sid})")
            self._poll()

        self.win.bridge.run(self.win.fleet.serial_open(self.sid, p["busid"], p["tty"], self._baud()), done,
                            lambda e: self.win.toast(getattr(e, "message", str(e))))

    def close(self) -> None:
        p = self.port
        self.is_open = False
        self._set_open_button()
        if p:
            self.win.bridge.run(self.win.fleet.serial_close(self.sid, p["busid"], p["tty"]),
                                lambda _r: self._note("closed"), lambda e: None)

    def _set_open_button(self) -> None:
        self.open_btn.set_label("Close" if self.is_open else "Open")
        for c in ("suggested-action", "destructive-action"):
            self.open_btn.remove_css_class(c)
        self.open_btn.add_css_class("destructive-action" if self.is_open else "suggested-action")

    def set_visible_now(self, visible: bool) -> None:
        """Read only while the tab is on the screen. The agent records in the meantime."""
        self.visible = visible
        if visible:
            if not self.ports:
                self.refresh_ports()
            self._poll()

    def _poll(self) -> None:
        if self.polling or not (self.is_open and self.visible and self.port):
            return
        self.polling = True
        p = self.port

        def done(r: dict[str, Any]) -> None:
            self.polling = False
            if p is not self.port or not self.is_open:
                return
            self._show(r)
            GLib.idle_add(lambda: (self._poll(), False)[1])

        def fail(e: Exception) -> None:
            self.polling = False
            self.is_open = False
            self._set_open_button()
            self._note(f"stopped: {getattr(e, 'message', str(e))}")

        self.win.bridge.run(self.win.fleet.serial_read(self.sid, p["busid"], p["tty"], self.offset, wait=1.0),
                            done, fail)

    def _show(self, r: dict[str, Any]) -> None:
        if r.get("lost") and self.offset:
            self._note("some data was lost (the buffer is full)")
        self.offset = r["offset"]
        for e in r.get("events") or []:
            key = (e["offset"], e["time"], e["text"])
            if key not in self.seen_events:
                self.seen_events.add(key)
                self._note(e["text"])
        data = r.get("data") or b""
        if data:
            if len(self.saved) < MAX_SAVED:
                self.saved.extend(data[: MAX_SAVED - len(self.saved)])
            self.term.feed(self._format(data))
        state = "connected" if r.get("connected") else "paused" if r.get("paused") else "waiting for the port"
        self._status(f"{self.port['tty']}  ·  {r.get('baud')} baud  ·  {state}  ·  {r['offset']} bytes")

    def _format(self, data: bytes) -> bytes:
        text = self.decoder.decode(data)
        text = text.replace("\r\n", "\n").replace("\n", "\r\n")  # a lone LF also starts a new line
        if self.ts.get_active():
            out = []
            for ch in text:
                if self.line_start and ch not in "\r\n":
                    out.append(time.strftime("\x1b[90m%H:%M:%S") + f".{int(time.time() * 1000) % 1000:03d}\x1b[0m ")
                    self.line_start = False
                out.append(ch)
                if ch == "\n":
                    self.line_start = True
            text = "".join(out)
        return text.encode()

    # -- input ---------------------------------------------------------------------
    def _write(self, data: bytes) -> None:
        if not (self.is_open and self.port):
            self.win.toast("Open the port first")
            return
        p = self.port
        self.win.bridge.run(self.win.fleet.serial_write(self.sid, p["busid"], p["tty"], data), None,
                            lambda e: self.win.toast(getattr(e, "message", str(e))))

    def _typed(self, _term: Vte.Terminal, text: str, _size: int) -> None:
        data = text.encode()
        eol = self._eol()
        if eol != b"\r":
            data = data.replace(b"\r", eol)
        self._write(data)

    def _send_line(self) -> None:
        text = self.line.get_text()
        self._write(text.encode() + self._eol())
        self.line.set_text("")

    def reset(self) -> None:
        if not (self.is_open and self.port):
            self.win.toast("Open the port first")
            return
        p = self.port
        self.win.bridge.run(self.win.fleet.serial_reset(self.sid, p["busid"], p["tty"], "auto"),
                            lambda r: self._note("reset: " + "; ".join(r.get("notes") or [])),
                            lambda e: self.win.toast(getattr(e, "message", str(e))))

    def save(self) -> None:
        if not self.saved:
            self.win.toast("Nothing to save yet")
            return
        dlg = Gtk.FileDialog(title="Save the serial log",
                             initial_name=f"{self.sid}-{self.port['tty'] if self.port else 'serial'}-"
                                          f"{time.strftime('%Y%m%d-%H%M%S')}.log")

        def done(d: Gtk.FileDialog, res: Gio.AsyncResult) -> None:
            try:
                f = d.save_finish(res)
            except GLib.Error:
                return  # cancelled
            with open(f.get_path(), "wb") as out:
                out.write(bytes(self.saved))
            self.win.toast(f"Saved {len(self.saved)} bytes to {f.get_path()}")

        dlg.save(self.win, None, done)
