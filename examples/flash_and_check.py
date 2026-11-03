"""Flash a board and check its boot message. Run: python examples/flash_and_check.py build/zephyr.hex"""
import sys

from setup_manager import BoardBusy, ExpectTimeout, FlashFailed, Lab

FIRMWARE = sys.argv[1] if len(sys.argv) > 1 else "build/zephyr/zephyr.hex"

with Lab() as lab:                                  # the same setups as the app
    board = lab.device("Board A")                   # nickname, serial number, "bench-1:1-2" or "0483:374b"
    try:
        with board.reserve(ttl=600, wait=120):      # nobody else can change the board meanwhile
            with board.serial(115200) as con:       # open before the flash: we get the first boot output
                board.flash(FIRMWARE)               # the last profile of this board (or a suggested one)
                con.expect(r"Booting Zephyr", timeout=15)
                con.send("kernel version")
                print("version:", con.expect(r"version (\S+)", timeout=5).group(1))
    except BoardBusy as e:
        sys.exit(f"board busy: {e}")
    except FlashFailed as e:
        sys.exit(f"flash failed:\n{e.result.stderr or e.result.stdout}")
    except ExpectTimeout as e:
        sys.exit(f"no boot message. Serial output:\n{e.text}")
