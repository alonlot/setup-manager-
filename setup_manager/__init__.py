"""Setup Manager: manage Linux setup PCs and their USB/IP devices."""

__version__ = "0.1.0"

_LAB_NAMES = {"Lab", "Setup", "Device", "SerialConsole", "Reservation", "CommandResult", "FlashResult",
              "IdentifyResult", "LabError", "DeviceNotFound", "AmbiguousDevice", "BoardBusy", "FlashFailed",
              "CommandFailed", "ExpectTimeout"}


def __getattr__(name):  # from setup_manager import Lab  (loaded only when used)
    if name in _LAB_NAMES:
        from . import lab
        return getattr(lab, name)
    raise AttributeError(f"module 'setup_manager' has no attribute {name!r}")
