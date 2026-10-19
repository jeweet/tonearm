"""Tonearm's log: ~/.local/state/tonearm/tonearm.log (rotating, 3 × 1 MB) plus stderr.

Uncaught exceptions — on the main thread, in worker threads and in GTK callbacks —
are logged too, so a bug report from a phone is "copy the troubleshooting info from
About". ``TONEARM_DEBUG=1`` logs debug detail (every Kodi call, artwork loads…) and
echoes it on stderr.
"""
from __future__ import annotations

import logging
import os
import platform
import sys
import threading
from logging.handlers import RotatingFileHandler


def _xdg(var: str, fallback: str) -> str:
    return os.environ.get(var) or os.path.join(os.path.expanduser("~"), fallback)


LOG_DIR = os.path.join(_xdg("XDG_STATE_HOME", ".local/state"), "tonearm")
LOG_FILE = os.path.join(LOG_DIR, "tonearm.log")
DEBUG = bool(os.environ.get("TONEARM_DEBUG"))

log = logging.getLogger("tonearm")


def setup_logging(level: str | None = None, log_file: str = LOG_FILE) -> str | None:
    """Log to a rotating file and to stderr (warnings; everything with TONEARM_DEBUG).
    Returns the file's path, or None when it can't be written."""
    level = level or ("DEBUG" if DEBUG else "INFO")
    log.setLevel(getattr(logging, level.upper(), logging.INFO))
    if log.handlers:
        return log_file
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(threadName)s %(message)s")
    stream = logging.StreamHandler()
    stream.setFormatter(fmt)
    stream.setLevel(logging.DEBUG if DEBUG else logging.WARNING)
    log.addHandler(stream)
    written = None
    try:
        os.makedirs(os.path.dirname(log_file), exist_ok=True)
        handler = RotatingFileHandler(log_file, maxBytes=1_000_000, backupCount=2,
                                      encoding="utf-8")
        handler.setFormatter(fmt)
        log.addHandler(handler)
        written = log_file
    except OSError as err:
        log.warning("Can't write the log file %s: %s", log_file, err)
    _install_hooks()
    return written


def _install_hooks():
    previous = sys.excepthook

    def on_exception(exc_type, value, tb):
        if not issubclass(exc_type, KeyboardInterrupt):
            log.critical("Uncaught exception", exc_info=(exc_type, value, tb))
        previous(exc_type, value, tb)

    def on_thread_exception(args):
        log.critical("Uncaught exception in thread %s",
                     args.thread.name if args.thread else "?",
                     exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    sys.excepthook = on_exception          # PyGObject routes callback errors here too
    threading.excepthook = on_thread_exception


def tail(lines: int = 200, log_file: str = LOG_FILE) -> str:
    """The end of the log, for the About dialog's troubleshooting section."""
    try:
        with open(log_file, encoding="utf-8", errors="replace") as fh:
            return "".join(fh.readlines()[-lines:])
    except OSError:
        return ""


def debug_report(version: str, toolkit: dict, kodi: dict | None, extra: dict | None = None,
                 lines: int = 200, log_file: str = LOG_FILE) -> str:
    """The text behind About → Troubleshooting → Debugging Information: versions, the
    environment, the connected Kodi (no addresses or passwords) and the log's end."""
    rows = [("Tonearm", version),
            ("Python", platform.python_version()),
            *[(k, v) for k, v in toolkit.items()],
            ("System", f"{platform.system()} {platform.release()} ({platform.machine()})"),
            ("Flatpak", os.environ.get("FLATPAK_ID") or "no"),
            ("Desktop", os.environ.get("XDG_CURRENT_DESKTOP") or "?"),
            ("Session", os.environ.get("XDG_SESSION_TYPE") or "?")]
    if kodi:
        rows.append(("Kodi", f"{kodi.get('name', 'Kodi')} {kodi.get('version', '?')} "
                             f"(JSON-RPC {kodi.get('api', '?')})"))
    for k, v in (extra or {}).items():
        rows.append((k, v))
    width = max(len(k) for k, _ in rows)
    head = "\n".join(f"{k.ljust(width)}  {v}" for k, v in rows)
    return (f"{head}\n\nLog file: {log_file}\n\n"
            f"{tail(lines, log_file) or '(the log is empty)'}")


__all__ = ["log", "setup_logging", "tail", "debug_report", "LOG_FILE", "DEBUG"]
