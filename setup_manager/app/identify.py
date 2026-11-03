"""The "Identify MCU" dialog: reset the board, read its boot log, show the MCU guess."""

from __future__ import annotations

import re
import time
from typing import TYPE_CHECKING, Any

from gi.repository import Adw, GLib, Gtk, Pango

from ..agent.setup_agent import AUTO_BAUDS, RESET_METHODS
from .widgets import clear, copy_text, pill

if TYPE_CHECKING:
    from .main import MainWindow

BAUDS = ["auto"] + [str(b) for b in sorted(set(AUTO_BAUDS + [38400, 19200, 1000000, 1500000, 2000000]))]
ENV_HELP = ("The scripts run with bash on the PC that has the serial port. Variables: $SM_TTY (the port), "
            "$SM_TTYS, $SM_BAUD, $SM_BUSID, $SM_VID, $SM_PID, $SM_SERIAL, $SM_PHASE. Example: "
            "printf 'version\\r' > \"$SM_TTY\". The script output also counts for the guess.")


def mcu_pill(win: "MainWindow", sid: str, d: dict[str, Any]) -> Gtk.Widget | None:
    """The small label next to a device: the MCU, with '?' when the guess is not sure."""
    r = d.get("mcu")
    if not r or not r.get("mcu"):
        return None
    sure = r.get("sure")
    short = re.sub(r"\s*\(.*\)$", "", r["mcu"]) or r["mcu"]  # "STM32 (family)" -> "STM32"
    text = short if sure else f"{short}?"
    color = "green" if sure else "amber" if (r.get("confidence") or 0) >= 0.4 else "grey"
    tip = [r.get("label") or text, f"confidence {round(100 * (r.get('confidence') or 0))}%"]
    if r.get("firmware"):
        tip.append("firmware: " + ", ".join(r["firmware"]))
    tip += [f"• {e}" for e in (r.get("evidence") or [])[:4]]
    if r.get("at"):
        tip.append("identified " + time.strftime("%Y-%m-%d %H:%M", time.localtime(r["at"])) + ". Click to run again.")
    b = Gtk.Button(child=Gtk.Label(label=text, ellipsize=Pango.EllipsizeMode.END, max_width_chars=22),
                   css_classes=["pill", color, "flat"], valign=Gtk.Align.CENTER, tooltip_text="\n".join(tip))
    b.connect("clicked", lambda *_: IdentifyDialog(win, sid, d).present(win))
    return b


def _text_editor(text: str, height: int = 90) -> tuple[Gtk.Widget, Gtk.TextView]:
    tv = Gtk.TextView(monospace=True, top_margin=8, bottom_margin=8, left_margin=10, right_margin=10,
                      wrap_mode=Gtk.WrapMode.WORD_CHAR)
    tv.get_buffer().set_text(text or "")
    sw = Gtk.ScrolledWindow(child=tv, min_content_height=height, max_content_height=260,
                            propagate_natural_height=True, css_classes=["card"])
    return sw, tv


def _get(tv: Gtk.TextView) -> str:
    buf = tv.get_buffer()
    return buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False)


class IdentifyDialog(Adw.Dialog):
    def __init__(self, win: "MainWindow", sid: str, d: dict[str, Any]):
        super().__init__(title="Identify MCU", content_width=780, content_height=760)
        self.win, self.sid, self.d = win, sid, d
        self.fleet = win.fleet
        self.loading = False
        opts = self.fleet.identify_options(sid, d["busid"], d)

        view = Adw.ToolbarView()
        hb = Adw.HeaderBar()
        hb.set_title_widget(Adw.WindowTitle(title="Identify MCU", subtitle=f"{d['name']}  ·  bus {d['busid']}"))
        self.run_btn = Gtk.Button(label="Reset and read", css_classes=["suggested-action"])
        self.run_btn.connect("clicked", lambda *_: self.run())
        hb.pack_end(self.run_btn)
        view.add_top_bar(hb)

        page = Adw.PreferencesPage()
        view.set_content(page)
        self.set_child(view)

        # Result.
        self.result_group = Adw.PreferencesGroup(title="Result")
        self.result_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.result_group.add(self.result_box)
        page.add(self.result_group)
        if d.get("mcu"):
            self._show_stored(d["mcu"])
        else:
            self.result_group.set_visible(False)

        # Options.
        g = Adw.PreferencesGroup(title="How", description="The app resets the board, reads the serial port and "
                                                          "matches the text against known boot messages.")
        self.profiles = self.fleet.identify_profiles()
        self.profile_names = list(self.profiles)
        self.profile_row = Adw.ComboRow(title="Profile", model=Gtk.StringList.new(self.profile_names))
        self.profile_row.connect("notify::selected", lambda *_: self._profile_selected())
        g.add(self.profile_row)
        self.methods = list(RESET_METHODS)
        self.reset_row = Adw.ComboRow(title="Reset", model=Gtk.StringList.new(
            [f"{m}  ·  {RESET_METHODS[m]}" for m in self.methods]))
        g.add(self.reset_row)
        self.baud_row = Adw.ComboRow(title="Baud rate", subtitle="auto: tries rates until the text is readable",
                                     model=Gtk.StringList.new(BAUDS))
        g.add(self.baud_row)
        self.duration_row = Adw.SpinRow.new_with_range(0.5, 120, 0.5)
        self.duration_row.set_title("Capture time after the reset (seconds)")
        g.add(self.duration_row)
        self.ports = ["all ports"] + list(d.get("ttys") or [])
        self.port_row = Adw.ComboRow(title="Serial port", model=Gtk.StringList.new(self.ports),
                                     subtitle="" if d.get("ttys") else "this device has no serial port")
        g.add(self.port_row)
        page.add(g)

        # Scripts.
        g = Adw.PreferencesGroup(title="Scripts", description=ENV_HELP)
        g.add(Gtk.Label(label="At the start (before the reset)", xalign=0, css_classes=["heading"], margin_top=6))
        w, self.script_tv = _text_editor("")
        g.add(w)
        g.add(Gtk.Label(label="After the reset (runs while the port is read)", xalign=0, css_classes=["heading"],
                        margin_top=12))
        w, self.after_tv = _text_editor("")
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

        self._fill(opts)
        name = opts.get("profile")
        self.loading = True
        self.profile_row.set_selected(self.profile_names.index(name) if name in self.profile_names else 0)
        self.loading = False
        self._update_delete()

    # -- options -----------------------------------------------------------------
    def _fill(self, opts: dict[str, Any]) -> None:
        self.loading = True
        reset = opts.get("reset") or "auto"
        self.reset_row.set_selected(self.methods.index(reset) if reset in self.methods else 0)
        baud = str(opts.get("baud") or "auto")
        self.baud_row.set_selected(BAUDS.index(baud) if baud in BAUDS else 0)
        self.duration_row.set_value(float(opts.get("duration") or 4))
        tty = opts.get("tty")
        self.port_row.set_selected(self.ports.index(tty) if tty in self.ports else 0)
        self.script_tv.get_buffer().set_text(opts.get("script") or "")
        self.after_tv.get_buffer().set_text(opts.get("after_script") or "")
        self.loading = False

    def _profile_selected(self) -> None:
        self._update_delete()
        if self.loading:
            return
        p = self.profiles.get(self.profile_names[self.profile_row.get_selected()])
        if p:
            self._fill(p)
            self.profile_row.set_subtitle(p.get("help") or "")

    def _update_delete(self) -> None:
        name = self.profile_names[self.profile_row.get_selected()]
        self.delete_btn.set_sensitive(not self.profiles.get(name, {}).get("builtin", True))
        self.profile_row.set_subtitle(self.profiles.get(name, {}).get("help") or "")

    def options(self) -> dict[str, Any]:
        tty = self.ports[self.port_row.get_selected()]
        return {
            "reset": self.methods[self.reset_row.get_selected()],
            "baud": BAUDS[self.baud_row.get_selected()],
            "duration": self.duration_row.get_value(),
            "script": _get(self.script_tv),
            "after_script": _get(self.after_tv),
            "tty": None if tty == "all ports" else tty,
        }

    def _reload_profiles(self, select: str | None) -> None:
        self.profiles = self.fleet.identify_profiles()
        self.profile_names = list(self.profiles)
        self.loading = True
        self.profile_row.set_model(Gtk.StringList.new(self.profile_names))
        if select in self.profile_names:
            self.profile_row.set_selected(self.profile_names.index(select))
        self.loading = False
        self._update_delete()

    def _save_profile(self) -> None:
        current = self.profile_names[self.profile_row.get_selected()]
        entry = Gtk.Entry(text="" if self.profiles.get(current, {}).get("builtin") else current,
                          placeholder_text="Profile name", activates_default=True)
        dlg = Adw.AlertDialog(heading="Save as profile", body="Save the reset method, baud rate, time and scripts.")
        dlg.set_extra_child(entry)
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("save", "Save")
        dlg.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)
        dlg.set_default_response("save")

        def response(_d: Any, r: str) -> None:
            if r != "save":
                return
            name = entry.get_text().strip()
            try:
                opts = self.options()
                opts.pop("tty", None)
                self.fleet.save_identify_profile(name, opts)
            except Exception as e:  # noqa: BLE001
                self.win.toast(getattr(e, "message", str(e)))
                return
            self._reload_profiles(name)
            self.win.toast(f"Profile '{name}' saved")

        dlg.connect("response", response)
        dlg.present(self)
        GLib.idle_add(lambda: (entry.grab_focus(), False)[1])  # type at once

    def _delete_profile(self) -> None:
        name = self.profile_names[self.profile_row.get_selected()]
        self.fleet.save_identify_profile(name, None)
        self._reload_profiles("Auto")
        self.win.toast(f"Profile '{name}' deleted")

    # -- run ---------------------------------------------------------------------
    def run(self) -> None:
        self.run_btn.set_sensitive(False)
        self.run_btn.set_child(Gtk.Spinner(spinning=True))
        self.result_group.set_visible(True)
        clear(self.result_box)
        self.result_box.append(Gtk.Label(label="Resetting the board and reading the serial port…", xalign=0,
                                         css_classes=["dim-label"]))
        opts = self.options()
        profile = self.profile_names[self.profile_row.get_selected()]
        opts["profile"] = profile

        def done(r: dict[str, Any]) -> None:
            self._idle()
            self._show_result(r)
            self.win.after_identify(self.sid)

        def fail(e: Exception) -> None:
            self._idle()
            clear(self.result_box)
            self.result_box.append(Gtk.Label(label=getattr(e, "message", str(e)), xalign=0, wrap=True,
                                             css_classes=["error"]))

        self.win.bridge.run(self.fleet.identify(self.sid, self.d["busid"], opts), done, fail)

    def _idle(self) -> None:
        self.run_btn.set_sensitive(True)
        self.run_btn.set_label("Reset and read")

    def _headline(self, label: str, mcu: str | None, sure: bool, confidence: float,
                  firmware: list[str]) -> None:
        top = Gtk.Box(spacing=12)
        top.append(Gtk.Label(label=mcu or "Unknown", css_classes=["title-1"], selectable=True))
        if mcu:
            top.append(pill("sure" if sure else "not sure", "green" if sure else "amber"))
            top.append(Gtk.Label(label=f"{round(100 * confidence)}%", css_classes=["dim-label"]))
        self.result_box.append(top)
        if label and label != mcu:
            self.result_box.append(Gtk.Label(label=label, xalign=0, wrap=True, css_classes=["dim-label"]))
        if firmware:
            self.result_box.append(Gtk.Label(label="Firmware: " + ", ".join(firmware), xalign=0, wrap=True))

    def _evidence(self, evidence: list[str]) -> None:
        if not evidence:
            return
        lst = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, css_classes=["boxed-list"])
        for e in evidence[:8]:
            lst.append(Adw.ActionRow(title=e, use_markup=False, title_lines=3))
        self.result_box.append(lst)

    def _show_stored(self, r: dict[str, Any]) -> None:
        clear(self.result_box)
        self._headline(r.get("label") or "", r.get("mcu"), bool(r.get("sure")), r.get("confidence") or 0,
                       r.get("firmware") or [])
        self._evidence(r.get("evidence") or [])
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(r.get("at") or 0))
        self.result_box.append(Gtk.Label(label=f"Last run {when} (reset {r.get('reset')}, {r.get('baud')} baud). "
                                               "Click “Reset and read” to run again.", xalign=0, wrap=True,
                                         css_classes=["dim-label", "caption"]))

    def _show_result(self, r: dict[str, Any]) -> None:
        g, cap = r["guess"], r["capture"]
        clear(self.result_box)
        self._headline(g["label"], g.get("mcu"), g["sure"], g["confidence"], g.get("firmware") or [])
        if len(g.get("candidates") or []) > 1:
            others = ", ".join(f"{c['name']} {round(100 * c['confidence'])}%" for c in g["candidates"][1:4])
            self.result_box.append(Gtk.Label(label=f"Other candidates: {others}", xalign=0, wrap=True,
                                             css_classes=["dim-label"]))
        self._evidence(g.get("evidence") or [])
        attempts = ", ".join(f"{a['baud']} baud: {a['bytes']} bytes, {round(100 * a['readable'])}% readable"
                             for a in cap.get("attempts", []))
        facts = [f"Ran on {r['where']}", f"reset {cap.get('reset')}", attempts]
        facts += cap.get("notes") or []
        self.result_box.append(Gtk.Label(label="  ·  ".join(x for x in facts if x), xalign=0, wrap=True,
                                         css_classes=["dim-label", "caption"]))
        for s in cap.get("scripts") or []:
            ok = s.get("exit_code") == 0 and not s.get("timed_out")
            out = ((s.get("stdout") or "") + (s.get("stderr") or "")).strip()
            exp = Adw.ExpanderRow(title=f"{s.get('phase', 'script').replace('_', ' ')} script: "
                                        f"{'timeout' if s.get('timed_out') else 'exit ' + str(s.get('exit_code'))}",
                                  subtitle=out.splitlines()[-1][:120] if out else "(no output)", use_markup=False)
            exp.add_prefix(Gtk.Image(icon_name="emblem-ok-symbolic" if ok else "dialog-warning-symbolic"))
            w, tv = _text_editor(out or "(no output)", 60)
            tv.set_editable(False)
            exp.add_row(w)
            lst = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, css_classes=["boxed-list"])
            lst.append(exp)
            self.result_box.append(lst)
        if cap.get("probe"):
            self.result_box.append(Gtk.Label(label=f"Debug probe: {cap['probe']}", xalign=0, wrap=True,
                                             css_classes=["caption"], selectable=True))
        head = Gtk.Box(spacing=8, margin_top=6)
        head.append(Gtk.Label(label=f"Serial log ({', '.join(cap.get('ttys') or []) or 'no port'}, "
                                    f"{cap.get('baud')} baud)", xalign=0, hexpand=True, css_classes=["heading"]))
        log = cap.get("log") or ""
        b = Gtk.Button(icon_name="edit-copy-symbolic", tooltip_text="Copy the log", css_classes=["flat"])
        b.connect("clicked", lambda *_: (copy_text(self.result_box, log), self.win.toast("Log copied")))
        head.append(b)
        self.result_box.append(head)
        w, tv = _text_editor(log.strip() or "(nothing received: try another reset method, baud rate, or a script)",
                             200)
        tv.set_editable(False)
        self.result_box.append(w)
