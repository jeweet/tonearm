"""A tiny pool of *daemon* worker threads.

concurrent.futures.ThreadPoolExecutor joins its threads at interpreter exit, so a
slow network call (Kodi off, artwork download) would keep the app hanging after
quit. Daemon threads simply die with the process.
"""
from __future__ import annotations

import queue
import threading
import traceback


class DaemonPool:
    def __init__(self, workers: int, name: str, lifo: bool = False):
        # LIFO suits artwork: the newest requests are for what's on screen right now.
        self._q = queue.LifoQueue() if lifo else queue.SimpleQueue()
        self._stopped = False
        for i in range(workers):
            threading.Thread(target=self._loop, name=f"{name}-{i}", daemon=True).start()

    def _loop(self):
        while True:
            fn, args = self._q.get()
            if fn is None:
                return
            try:
                fn(*args)
            except Exception:  # noqa: BLE001
                traceback.print_exc()

    def submit(self, fn, *args):
        if not self._stopped:
            self._q.put((fn, args))

    def shutdown(self):
        """Drop queued work; running calls are abandoned (threads are daemons)."""
        self._stopped = True
        try:
            while True:
                self._q.get_nowait()
        except queue.Empty:
            pass
