"""The "Flash firmware" dialog: select a file and a profile, flash, show the tool output and the boot log."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

from gi.repository import Adw, Gio, GLib, Gtk

from .identify import _get, _text_editor
from .widgets import clear, pill

if TYPE_CHECKING:
    from .main import MainWindow

HELP = ("The script runs with bash on the PC that has the device. $SM_FILE is the firmware, $SM_FILE_EXT its "
        "extension, $SM_TTY the serial port, $SM_SERIAL the USB serial number. Add your own variables below "
        "(NAME=value, one per line).")


def _env_text(env: dict[str, str] | None) -> str:
    return "\n".join(f"{k}={v}" for k, v in (env or {}).items())


def _parse_env(text: str) -> dict[str, str]:
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


class FlashDialog(Adw.Dialog):
    def __init__(self, win: "MainWindow", sid: str, d: dict[str, Any]):
        super().__init__(title="Flash firmware", content_width=780, content_height=780)
        self.win, self.sid, self.d = win, sid, d
        self.fleet = win.fleet
        self.loading = False
        opts = self.fleet.flash_options(sid, d)
        self.file: str | None = opts.get("file") if opts.get("file") and os.path.isfile(opts["file"]) else None

        view = Adw.ToolbarView()
        hb = Adw.HeaderBar()
        hb.set_title_widget(Adw.WindowTitle(title="Flash firmware",
                                            subtitle=f"{d.get('nickname') or d['name']}  ·  bus {d['busid']}"))
        self.go_btn = Gtk.Button(label="Flash", css_classes=["suggested-action"])
        self.go_btn.connect("clicked", lambda *_: self.run())
        hb.pack_end(self.go_btn)
        view.add_top_bar(hb)
        page = Adw.PreferencesPage()
        view.set_content(page)
        self.set_child(view)

        self.result_group = Adw.PreferencesGroup(title="Result", visible=False)
        self.result_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.result_group.add(self.result_box)
        page.add(self.result_group)

        g = Adw.PreferencesGroup(title="Firmware")
        self.file_row = Adw.ActionRow(title="File", subtitle=self.file or "no file selected", use_markup=False,
                                      subtitle_selectable=True)
        pick = Gtk.Button(label="Select…", valign=Gtk.Align.CENTER)
        pick.connect("clicked", lambda *_: self._pick())
        self.file_row.add_suffix(pick)
        g.add(self.file_row)
        self.profiles = self.fleet.flash_profiles()
        self.names = list(self.profiles)
        self.profile_row = Adw.ComboRow(title="Profile", model=Gtk.StringList.new(self.names))
        self.profile_row.connect("notify::selected", lambda *_: self._profile_selected())
        g.add(self.profile_row)
        self.monitor_row = Adw.SpinRow.new_with_range(0, 120, 1)
        self.monitor_row.set_title("Show the serial output after the flash (seconds)")
        g.add(self.monitor_row)
        self.timeout_row = Adw.SpinRow.new_with_range(10, 3600, 10)
        self.timeout_row.set_title("Timeout (seconds)")
        g.add(self.timeout_row)
        page.add(g)

        g = Adw.PreferencesGroup(title="Script", description=HELP)
        w, self.script_tv = _text_editor("", 120)
        g.add(w)
        g.add(Gtk.Label(label="Variables", xalign=0, css_classes=["heading"], margin_top=12))
        w, self.env_tv = _text_editor("", 50)
        g.add(w)
        btns = Gtk.Box(spacing=8, margin_top=12)
        save = Gtk.Button(label="Save as profile…")
        save.connect("clicked", lambda *_: self._save_profile())
        self.delete_btn = Gtk.Button(label="Delete profile", css_classes=["destructive-action"])
        self.delete_btn.connect("clicked", lambda *_: self._delete_profile())
        btns.append(save)
        btns.append(self.delete_btn)
        g.add(btns)
        page.add(g)

        self.loading = True
        name = opts.get("profile")
        self.profile_row.set_selected(self.names.index(name) if name in self.names else 0)
        self.loading = False
        self._fill(opts)
        self._update_profile_ui()

    # -- options ---------------------------------------------------------------------
    def _fill(self, o: dict[str, Any]) -> None:
        self.script_tv.get_buffer().set_text(o.get("script") or "")
        self.env_tv.get_buffer().set_text(_env_text(o.get("env")))
        self.monitor_row.set_value(float(o.get("monitor_seconds") if o.get("monitor_seconds") is not None else 3))
        self.timeout_row.set_value(float(o.get("timeout") or 300))

    def _profile_selected(self) -> None:
        self._update_profile_ui()
        if not self.loading:
            self._fill(self.profiles[self.names[self.profile_row.get_selected()]])

    def _update_profile_ui(self) -> None:
        p = self.profiles.get(self.names[self.profile_row.get_selected()], {})
        self.profile_row.set_subtitle(p.get("help") or "")
        self.delete_btn.set_sensitive(not p.get("builtin", True))

    def options(self) -> dict[str, Any]:
        return {"script": _get(self.script_tv), "env": _parse_env(_get(self.env_tv)),
                "monitor_seconds": self.monitor_row.get_value(), "timeout": self.timeout_row.get_value()}

    def _pick(self) -> None:
        dlg = Gtk.FileDialog(title="Select the firmware file")
        flt = Gtk.FileFilter(name="Firmware (bin, hex, elf, uf2)")
        for ext in ("bin", "hex", "elf", "uf2", "ihex", "srec", "s19", "axf", "out", "zip"):
            flt.add_suffix(ext)
        store = Gio.ListStore.new(Gtk.FileFilter)
        store.append(flt)
        dlg.set_filters(store)
        if self.file:
            dlg.set_initial_file(Gio.File.new_for_path(self.file))

        def done(d: Gtk.FileDialog, res: Gio.AsyncResult) -> None:
            try:
                f = d.open_finish(res)
            except GLib.Error:
                return
            self.file = f.get_path()
            self.file_row.set_subtitle(self.file)

        dlg.open(self.win, None, done)

    def _save_profile(self) -> None:
        entry = Gtk.Entry(placeholder_text="Profile name", activates_default=True)
        cur = self.names[self.profile_row.get_selected()]
        if not self.profiles.get(cur, {}).get("builtin"):
            entry.set_text(cur)
        dlg = Adw.AlertDialog(heading="Save as profile", body="Save the script, the variables and the times.")
        dlg.set_extra_child(entry)
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("save", "Save")
        dlg.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)
        dlg.set_default_response("save")

        def response(_d: Any, r: str) -> None:
            if r != "save":
                return
            try:
                self.fleet.save_flash_profile(entry.get_text(), self.options())
            except Exception as e:  # noqa: BLE001
                self.win.toast(getattr(e, "message", str(e)))
                return
            self._reload(entry.get_text().strip())

        dlg.connect("response", response)
        dlg.present(self)
        GLib.idle_add(lambda: (entry.grab_focus(), False)[1])  # type at once

    def _delete_profile(self) -> None:
        name = self.names[self.profile_row.get_selected()]
        self.fleet.save_flash_profile(name, None)
        self._reload(None)

    def _reload(self, select: str | None) -> None:
        self.profiles = self.fleet.flash_profiles()
        self.names = list(self.profiles)
        self.loading = True
        self.profile_row.set_model(Gtk.StringList.new(self.names))
        if select in self.names:
            self.profile_row.set_selected(self.names.index(select))
        self.loading = False
        self._update_profile_ui()

    # -- run -------------------------------------------------------------------------
    def run(self) -> None:
        if not self.file and "SM_FILE" in _get(self.script_tv):
            self.win.toast("Select the firmware file first")
            return
        self.go_btn.set_sensitive(False)
        self.go_btn.set_child(Gtk.Spinner(spinning=True))
        self.result_group.set_visible(True)
        clear(self.result_box)
        self.result_box.append(Gtk.Label(label="Uploading and flashing…", xalign=0, css_classes=["dim-label"]))
        profile = self.names[self.profile_row.get_selected()]
        opts = self.options()

        def done(r: dict[str, Any]) -> None:
            self._idle()
            self._show(r)
            self.win.after_identify(self.sid)  # refresh the rows

        def fail(e: Exception) -> None:
            self._idle()
            clear(self.result_box)
            self.result_box.append(Gtk.Label(label=getattr(e, "message", str(e)), wrap=True, xalign=0,
                                             css_classes=["error"]))

        self.win.bridge.run(self.fleet.flash(self.sid, self.d["busid"], self.file, profile, opts), done, fail)

    def _idle(self) -> None:
        self.go_btn.set_sensitive(True)
        self.go_btn.set_label("Flash")

    def _show(self, r: dict[str, Any]) -> None:
        clear(self.result_box)
        top = Gtk.Box(spacing=12)
        top.append(Gtk.Label(label="Flashed" if r["ok"] else "Flash failed", css_classes=["title-2"]))
        if r.get("timed_out"):
            top.append(pill("timeout", "red"))
        else:
            top.append(pill(f"exit {r.get('exit_code')}", "green" if r["ok"] else "red"))
        top.append(Gtk.Label(label=f"{r.get('duration')} s on {r['where']}", css_classes=["dim-label"]))
        self.result_box.append(top)
        out = ((r.get("stdout") or "") + (("\n" + r["stderr"]) if r.get("stderr") else "")).strip()
        self.result_box.append(Gtk.Label(label="Tool output", xalign=0, css_classes=["heading"]))
        w, tv = _text_editor(out or "(no output)", 140)
        tv.set_editable(False)
        self.result_box.append(w)
        m = r.get("monitor")
        if m:
            self.result_box.append(Gtk.Label(label=f"Serial output after the flash ({m.get('tty') or '-'})",
                                             xalign=0, css_classes=["heading"]))
            w, tv = _text_editor(m.get("text") or m.get("error") or "(nothing received)", 120)
            tv.set_editable(False)
            self.result_box.append(w)
