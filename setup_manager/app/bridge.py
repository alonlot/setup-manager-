"""Run the asyncio Fleet in a background thread and get results back on the GTK thread."""

from __future__ import annotations

import asyncio
import logging
import threading
from concurrent.futures import Future
from typing import Any, Awaitable, Callable

from gi.repository import GLib

log = logging.getLogger("setup-manager")


class AsyncBridge:
    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self.loop.run_forever, name="asyncio", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def run(self, coro: Awaitable[Any], on_done: Callable[[Any], None] | None = None,
            on_error: Callable[[Exception], None] | None = None) -> Future:
        """Schedule a coroutine. The callbacks run on the GTK main thread."""
        fut = asyncio.run_coroutine_threadsafe(coro, self.loop)  # type: ignore[arg-type]

        def done(f: Future) -> None:
            try:
                result = f.result()
            except (KeyboardInterrupt, SystemExit):
                raise
            except BaseException as e:  # noqa: BLE001 - shown to the user (CancelledError is a BaseException)
                if on_error:
                    GLib.idle_add(_once, on_error, e)
                else:
                    log.warning("%s failed: %r", getattr(coro, "__qualname__", "task"), e)
                return
            if on_done:
                GLib.idle_add(_once, on_done, result)

        fut.add_done_callback(done)
        return fut

    def call(self, coro: Awaitable[Any], timeout: float = 10) -> Any:
        """Run a coroutine and wait for the result (blocks the caller)."""
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)  # type: ignore[arg-type]

    def stop(self) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)


def _once(fn: Callable[[Any], None], arg: Any) -> bool:
    fn(arg)
    return False
