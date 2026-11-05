"""Dialogs: add/edit setup, network scan, preferences, confirm."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable

from gi.repository import Adw, Gtk

from .widgets import clear

if TYPE_CHECKING:
    from .main import MainWindow


def confirm(win: "MainWindow", heading: str, body: str, ok_label: str, on_ok: Callable[[], None],
            destructive: bool = True) -> None:
    d = Adw.AlertDialog(heading=heading, body=body)
    d.add_response("cancel", "Cancel")
    d.add_response("ok", ok_label)
    d.set_response_appearance("ok", Adw.ResponseAppearance.DESTRUCTIVE if destructive
                              else Adw.ResponseAppearance.SUGGESTED)
    d.set_default_response("cancel")
    d.set_close_response("cancel")
    d.connect("response", lambda _d, r: on_ok() if r == "ok" else None)
    d.present(win)


def _dialog_shell(title: str, content: Gtk.Widget, ok_label: str | None,
                  on_ok: Callable[[], None] | None, width: int = 480) -> tuple[Adw.Dialog, Gtk.Button | None]:
    dialog = Adw.Dialog(title=title, content_width=width)
    view = Adw.ToolbarView()
    hb = Adw.HeaderBar(show_end_title_buttons=ok_label is None, show_start_title_buttons=False)
    ok = None
    if ok_label:
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: dialog.close())
        hb.pack_start(cancel)
        ok = Gtk.Button(label=ok_label, css_classes=["suggested-action"])
        ok.connect("clicked", lambda *_: on_ok and on_ok())
        hb.pack_end(ok)
    view.add_top_bar(hb)
    view.set_content(content)
    dialog.set_child(view)
    return dialog, ok


def setup_dialog(win: "MainWindow", existing: dict[str, Any] | None = None,
                 prefill: dict[str, Any] | None = None) -> None:
    s = existing or prefill or {}
    page = Adw.PreferencesPage()
    group = Adw.PreferencesGroup(description="The agent prints its token at the end of install.sh "
                                             "(CONFIG_TOKEN=…).")
    page.add(group)
    name = Adw.EntryRow(title="Name", text=s.get("name") or "")
    host = Adw.EntryRow(title="Host or IP address", text=s.get("host") or "")
    default_port = int(win.store.data()["agent_defaults"].get("port") or 8765)
    port = Adw.SpinRow.new_with_range(1, 65535, 1)
    port.set_title("Agent port")
    port.set_value(int(s.get("port") or default_port))
    token = Adw.PasswordEntryRow(title="Agent token" + (" (empty = keep the stored token)" if existing and
                                                       existing.get("has_token") else ""))
    usbip_port = Adw.SpinRow.new_with_range(1, 65535, 1)
    usbip_port.set_title("USB/IP port")
    usbip_port.set_value(int(s.get("usbip_port") or 3240))
    fp = Adw.EntryRow(title="TLS fingerprint (CONFIG_FINGERPRINT from install.sh; empty = trust at the first "
                            "connection)", text=s.get("fingerprint") or "")
    fp.add_css_class("monospace")
    verify = Adw.SwitchRow(title="Certificate from a real CA", subtitle="Off for the self-signed certificate of "
                           "the agent (it is pinned by its fingerprint)", active=bool(s.get("verify_tls")))
    notes = Adw.EntryRow(title="Notes (board, location, owner)", text=s.get("notes") or "")
    for row in (name, host, port, token, fp, usbip_port, verify, notes):
        group.add(row)

    def save() -> None:
        h = host.get_text().strip()
        if not h:
            host.add_css_class("error")
            return
        body: dict[str, Any] = {
            "name": name.get_text().strip() or h, "host": h, "port": int(port.get_value()),
            "usbip_port": int(usbip_port.get_value()), "verify_tls": verify.get_active(),
            "notes": notes.get_text().strip(),
        }
        if token.get_text():
            body["token"] = token.get_text()
        from ..agent_client import clean_fingerprint
        fp_text = fp.get_text().strip()
        if fp_text and not clean_fingerprint(fp_text):
            fp.add_css_class("error")
            win.toast("The fingerprint must be 64 hex characters (SHA-256)")
            return
        body["fingerprint"] = clean_fingerprint(fp_text)
        if body["fingerprint"]:
            body["transport"] = "tls"
        assert ok is not None
        ok.set_sensitive(False)
        ok.set_child(Gtk.Spinner(spinning=True))
        coro = win.fleet.update_setup(existing["id"], body) if existing else win.fleet.add_setup(body)

        def done(r: dict[str, Any]) -> None:
            dialog.close()
            if r.get("online"):
                win.toast(f"{r['setup']['name']}: connected")
            else:
                win.toast(f"{r['setup']['name']} saved, but offline: {r.get('error')}", timeout=8)
            win.refresh_now(select=r["setup"]["id"])

        def fail(e: Exception) -> None:
            ok.set_sensitive(True)
            ok.set_label("Save" if existing else "Add")
            win.toast(getattr(e, "message", str(e)))

        win.bridge.run(coro, done, fail)

    dialog, ok = _dialog_shell(f"Edit {existing['name']}" if existing else "Add setup", page,
                               "Save" if existing else "Add", save)
    host.connect("entry-activated", lambda *_: save())
    dialog.present(win)
    (host if not s.get("host") else name).grab_focus()


def discover_dialog(win: "MainWindow") -> None:
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_top=12, margin_bottom=18,
                  margin_start=18, margin_end=18)
    status = Gtk.Label(label="Scanning…", xalign=0, css_classes=["dim-label"], wrap=True)
    box.append(status)
    lst = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, css_classes=["boxed-list"])
    box.append(lst)
    again = Gtk.Button(label="Scan again", halign=Gtk.Align.START)
    box.append(again)
    box.append(Gtk.Label(label="Discovery uses a UDP broadcast and finds agents in the same subnet only. "
                               "It does not send the token: you type it when you add the setup.",
                         wrap=True, xalign=0, css_classes=["dim-label", "caption"]))
    dialog, _ = _dialog_shell("Scan network", box, None, None, width=560)

    def show(found: list[dict[str, Any]]) -> None:
        clear(lst)
        status.set_label(f"{len(found)} agent(s) answered." if found else "No agents answered.")
        for a in found:
            row = Adw.ActionRow(title=a.get("name") or a["host"], use_markup=False,
                                subtitle=f"{a['host']}:{a.get('port')}  ·  {a.get('hostname')}  ·  v{a.get('version')}")
            if a.get("known_as"):
                row.add_suffix(Gtk.Label(label=f"added as {a['known_as']}", css_classes=["sm-pill", "green"],
                                         valign=Gtk.Align.CENTER))
            else:
                b = Gtk.Button(label="Add…", valign=Gtk.Align.CENTER, css_classes=["suggested-action"])
                b.connect("clicked", lambda _b, a=a: (dialog.close(), setup_dialog(
                    win, prefill={"name": a.get("name"), "host": a["host"], "port": a.get("port"),
                                  "tls": a.get("tls")})))
                row.add_suffix(b)
            lst.append(row)

    def scan() -> None:
        status.set_label("Scanning…")
        win.bridge.run(win.fleet.discover(), show, lambda e: status.set_label(f"Scan failed: {e}"))

    again.connect("clicked", lambda *_: scan())
    dialog.present(win)
    scan()


def preferences_dialog(win: "MainWindow") -> None:
    data = win.store.data()
    d = Adw.PreferencesDialog()
    page = Adw.PreferencesPage(title="General", icon_name="preferences-system-symbolic")
    d.add(page)

    agents = Adw.PreferencesGroup(title="Agents", description="Used by setups that do not set their own value.")
    token = Adw.PasswordEntryRow(title="Default agent token", text=data["agent_defaults"].get("token") or "")
    port = Adw.SpinRow.new_with_range(1, 65535, 1)
    port.set_title("Default agent port")
    port.set_value(int(data["agent_defaults"].get("port") or 8765))
    client = Adw.EntryRow(title=f"Your name for the setups (empty = {win.fleet.client_name})",
                          text=data.get("client_name") or "")
    poll = Adw.SpinRow.new_with_range(1, 120, 1)
    poll.set_title("Refresh interval (seconds)")
    poll.set_value(float(data.get("poll_interval") or 5))
    require = Adw.SwitchRow(title="Require encryption",
                            subtitle="Refuse agents without TLS (older than 0.4): their token goes in clear text",
                            active=bool(data.get("require_encryption")))
    encrypt_usbip = Adw.SwitchRow(title="Encrypt USB/IP",
                                  subtitle="Off: plain USB/IP, as usbip by hand (faster). The token always goes "
                                           "over TLS. On: USB/IP through the encrypted tunnel",
                                  active=bool(data.get("encrypt_usbip")))
    for r in (client, token, port, poll, require, encrypt_usbip):
        agents.add(r)
    page.add(agents)

    local = Adw.PreferencesGroup(title="This PC (usbip client)",
                                 description="Attach and detach run here. Without root, the app uses "
                                             "'sudo -n usbip'. install-app.sh adds the sudo rule.")
    raw_cmd = data["local_usbip"].get("command") or ""
    cmd = Adw.EntryRow(title="usbip command (empty = usbip)",
                       text=" ".join(raw_cmd) if isinstance(raw_cmd, list) else raw_cmd)
    sudo = Adw.ComboRow(title="Use sudo", model=Gtk.StringList.new(["Auto (when not root)", "Yes", "No"]))
    sudo.set_selected({None: 0, True: 1, False: 2}.get(data["local_usbip"].get("sudo"), 0))
    info = win.fleet.local.info()
    state = Adw.ActionRow(title="Status", use_markup=False,
                          subtitle=f"{info['command']}  ·  {'found' if info['found'] else 'NOT FOUND'}"
                                   + (f"  ·  {info['error']}" if info.get("error") else ""))
    for r in (cmd, sudo, state):
        local.add(r)
    page.add(local)

    files = Adw.PreferencesGroup(title="Files")
    files.add(Adw.ActionRow(title="Config file", subtitle=str(win.store.path), use_markup=False,
                            subtitle_selectable=True))
    page.add(files)

    def closed(*_: Any) -> None:
        data["client_name"] = client.get_text().strip()
        data["agent_defaults"]["token"] = token.get_text()
        data["agent_defaults"]["port"] = int(port.get_value())
        data["poll_interval"] = int(poll.get_value())
        data["require_encryption"] = require.get_active()
        data["encrypt_usbip"] = encrypt_usbip.get_active()
        data["local_usbip"]["command"] = cmd.get_text().strip() or None
        data["local_usbip"]["sudo"] = {0: None, 1: True, 2: False}[sudo.get_selected()]
        win.store.save()
        win.fleet.local.update(data["local_usbip"])

    d.connect("closed", closed)
    d.present(win)
