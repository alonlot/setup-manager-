"""Hardware-in-the-loop tests with pytest.

    pytest -p setup_manager.pytest_plugin examples/test_hil_example.py --lab-owner ci-runner-1
"""
import pytest


@pytest.mark.board("Board A")                         # reserved for the test, released after it
def test_boot_message(board):
    board.flash("build/zephyr/zephyr.hex")
    board.console.expect(r"Booting Zephyr", timeout=15)


@pytest.mark.board("Board A", ttl=900)
def test_shell_version(board):
    board.reset()
    board.console.expect(r"uart:~\$", timeout=10)
    board.console.send("kernel version")
    assert board.console.expect(r"Zephyr version (\d+)\.", timeout=5).group(1) == "3"
    # When a test fails, the serial output of the board is in the pytest report.
