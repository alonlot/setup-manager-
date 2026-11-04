"""pytest plugin for hardware-in-the-loop tests. Opt-in:

    pytest -p setup_manager.pytest_plugin            # or in conftest.py:
    pytest_plugins = ["setup_manager.pytest_plugin"]

    @pytest.mark.board("Board A")                    # nickname, serial, "bench-1:1-2", ...
    def test_boot(board):                            # reserved for this test, released after it
        board.flash("build/zephyr.hex")
        board.console.expect("boot OK", timeout=10)

Options: --lab-config PATH, --lab-owner NAME, --lab-wait SECONDS (wait for a reserved board).
Marker arguments: board(ref, setup=None, reserve=True, ttl=600, wait=None, baud=115200).
When a test fails, the serial output of its boards is added to the report.
"""

from __future__ import annotations

import os
import socket
import warnings
from typing import Any, Iterator

import pytest

from .lab import Device, Lab, LabError


def pytest_addoption(parser: Any) -> None:
    g = parser.getgroup("setup-manager")
    g.addoption("--lab-config", default=None, help="Setup Manager config file (default: the app config)")
    g.addoption("--lab-owner", default=None, help="owner name of the reservations (default: pytest@host)")
    g.addoption("--lab-wait", type=float, default=300.0, help="seconds to wait for a reserved board")


def pytest_configure(config: Any) -> None:
    config.addinivalue_line("markers", "board(ref, setup=None, reserve=True, ttl=600, wait=None, baud=115200): "
                                       "the test uses this board (Setup Manager)")


@pytest.fixture(scope="session")
def lab(request: Any) -> Iterator[Lab]:
    owner = request.config.getoption("--lab-owner") or f"pytest@{socket.gethostname()}"
    with Lab(config=request.config.getoption("--lab-config"), owner=owner) as lab_:
        yield lab_


@pytest.fixture
def board(request: Any, lab: Lab) -> Iterator[Device]:
    marker = request.node.get_closest_marker("board")
    if marker is None or not marker.args:
        pytest.fail("the board fixture needs @pytest.mark.board('<nickname, serial or setup:busid>')")
    kw = dict(marker.kwargs)
    dev = lab.device(marker.args[0], setup=kw.get("setup"))
    res = None
    if kw.get("reserve", True):
        wait = kw.get("wait")
        res = dev.reserve(owner=f"{lab.owner} {request.node.nodeid}"[:80], ttl=kw.get("ttl", 600),
                          wait=request.config.getoption("--lab-wait") if wait is None else wait)
    dev._console = None
    request.node._lab_devices = getattr(request.node, "_lab_devices", []) + [dev]
    try:
        # Open the console before the test, so that the boot banner after board.flash() is not lost.
        baud = kw.get("baud", 115200)
        try:
            dev._console = dev.serial(baud)
        except LabError as e:
            if "baud" in kw:
                raise  # the test asked for a serial port and it cannot be opened
            # No serial port, or it cannot be opened now: board.console tries again, and raises,
            # only if the test uses it.
            warnings.warn(f"{dev.display_name}: serial port not opened before the test: {e}", stacklevel=1)
        yield dev
    finally:
        if dev._console is not None:
            dev._console.close()
        if res is not None:
            res.release()


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: Any, call: Any) -> Iterator[None]:
    outcome = yield
    rep = outcome.get_result()
    if rep.when == "call" and rep.failed:
        for dev in getattr(item, "_lab_devices", []):
            con = dev._console
            if con is not None and con.log:
                rep.sections.append((f"serial {dev.display_name} ({con.tty})", con.log[-20000:]))


def pytest_report_header(config: Any) -> str:
    return f"setup-manager lab: config {config.getoption('--lab-config') or os.environ.get('SETUP_MANAGER_CONFIG') or 'default'}"
