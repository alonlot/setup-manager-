"""Smoke test of the GTK app. Runs only where GTK 4, libadwaita, VTE and a display exist."""
import os

import pytest

if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
    pytest.skip("no display", allow_module_level=True)
gi = pytest.importorskip("gi")
try:
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    gi.require_version("Vte", "3.91")
except ValueError as e:
    pytest.skip(str(e), allow_module_level=True)

from gi.repository import Gio, GLib  # noqa: E402

from setup_manager.app import main as appmain  # noqa: E402
from setup_manager.app.pages import OverviewPage, SetupPage  # noqa: E402
from setup_manager.config import ConfigStore  # noqa: E402


def test_app_smoke(manager_config):
    app = appmain.SetupManagerApp(ConfigStore(manager_config))
    app.set_flags(Gio.ApplicationFlags.NON_UNIQUE)
    seen = {}

    def step1():
        win = app.props.active_window
        seen["overview"] = isinstance(win.current, OverviewPage)
        seen["rows"] = list(win.setup_rows)
        win.select_setup("bench")
        GLib.timeout_add(2500, step2)
        return False

    def step2():
        win = app.props.active_window
        page = win.current
        seen["setup_page"] = isinstance(page, SetupPage)
        seen["devices"] = [d["busid"] for d in (page.devices or [])]
        n = 0
        row = page.usb_list.get_first_child()
        while row is not None:
            n += 1
            row = row.get_next_sibling()
        seen["usb_rows"] = n
        from setup_manager.app.identify import IdentifyDialog
        dlg = IdentifyDialog(win, "bench", page.devices[0])
        seen["dialog"] = dlg.options()["reset"] == "auto" and "ESP32 / ESP8266" in dlg.profile_names
        page.stack.set_visible_child_name("terminal")
        GLib.timeout_add(500, step3)
        return False

    def step3():
        page = app.props.active_window.current
        seen["tabs"] = page.tabs.get_n_pages()
        app.quit()
        return False

    GLib.timeout_add(3500, step1)
    GLib.timeout_add(30000, lambda: app.quit() or False)
    app.run([])
    assert seen["overview"] and seen["rows"] == ["bench"]
    assert seen["setup_page"] and seen["devices"] == ["1-1", "1-2", "1-3", "2-1.3"]
    assert seen["usb_rows"] == 4 and seen["tabs"] == 1 and seen["dialog"]
