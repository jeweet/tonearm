"""Small helpers shared by the UI layer."""
from __future__ import annotations

import html
import traceback

from gi.repository import GLib

from .workers import DaemonPool

_pool = DaemonPool(4, "tonearm-worker")


def run_async(fn, *args, done=None, error=None):
    """Run ``fn(*args)`` on a worker thread; deliver result/exception on the main loop."""

    def work():
        try:
            res = fn(*args)
        except Exception as exc:  # noqa: BLE001
            if error is not None:
                GLib.idle_add(_once, error, exc)
            else:
                traceback.print_exc()
            return
        if done is not None:
            GLib.idle_add(_once, done, res)

    _pool.submit(work)


def shutdown_workers():
    _pool.shutdown()


def _once(cb, arg):
    try:
        cb(arg)
    except Exception:  # noqa: BLE001
        traceback.print_exc()
    return GLib.SOURCE_REMOVE


def idle(cb, *args):
    """Schedule ``cb(*args)`` on the main loop from any thread."""
    def run():
        try:
            cb(*args)
        except Exception:  # noqa: BLE001
            traceback.print_exc()
        return GLib.SOURCE_REMOVE
    GLib.idle_add(run)


def plural(n: int, word: str) -> str:
    """"1 album", "3 albums"."""
    return f"{n} {word}{'s' if n != 1 else ''}"


def fmt_time(seconds) -> str:
    seconds = int(seconds or 0)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def fmt_total(seconds) -> str:
    seconds = int(seconds or 0)
    h, m = divmod(seconds // 60, 60)
    return f"{h} h {m} min" if h else f"{m} min"


def highlight(text: str, positions, offset: int = 0) -> str:
    """Pango markup with matched characters emphasised."""
    pos = {p - offset for p in positions}
    out = []
    for i, ch in enumerate(text):
        e = html.escape(ch, quote=False)
        out.append(f"<b><span underline='single'>{e}</span></b>" if i in pos else e)
    return "".join(out)


def human_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"
