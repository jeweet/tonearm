"""Minimal stand-in for PyGObject so Session logic can be tested headless.

Only used when the real `gi` with GTK 4 isn't importable (e.g. CI containers).
"""
import queue
import sys
import time
import types

_q: "queue.Queue" = queue.Queue()
_timers = {}
_ids = iter(range(1, 10**9))


def install():
    gi = types.ModuleType("gi")
    gi.FAKE = True
    gi.require_version = lambda *a: None
    repo = types.ModuleType("gi.repository")

    GLib = types.SimpleNamespace(
        SOURCE_REMOVE=False, SOURCE_CONTINUE=True,
        idle_add=lambda fn, *a: (_q.put((fn, a)), next(_ids))[1],
        timeout_add=lambda ms, fn, *a: _add_timer(ms / 1000, fn, a),
        timeout_add_seconds=lambda s, fn, *a: _add_timer(s, fn, a),
        source_remove=lambda i: _timers.pop(i, None),
        markup_escape_text=lambda s: s,
    )

    class Property:
        def __init__(self, type=None, default=None):
            self.default = default

        def __set_name__(self, owner, name):
            self.name = name

    class Object:
        def __init__(self):
            self._handlers = {}
            self._hid = 0
            obj = self

            class Props:
                def __getattr__(s, k):
                    return obj.__dict__.get("_p_" + k, getattr(type(obj), k).default)

                def __setattr__(s, k, v):
                    old = obj.__dict__.get("_p_" + k, getattr(type(obj), k).default)
                    obj.__dict__["_p_" + k] = v
                    if old != v:
                        obj.emit("notify::" + k.replace("_", "-"))
            self.props = Props()

        def connect(self, sig, cb, *extra):
            self._hid += 1
            self._handlers[self._hid] = (sig, cb, extra)
            return self._hid

        def disconnect(self, hid):
            self._handlers.pop(hid, None)

        def emit(self, sig, *args):
            for s, cb, extra in list(self._handlers.values()):
                if s == sig:
                    cb(self, *args, *extra)

    GObject = types.SimpleNamespace(Object=Object, Property=Property,
                                    SignalFlags=types.SimpleNamespace(RUN_FIRST=1))
    Gdk = types.SimpleNamespace(Texture=types.SimpleNamespace(new_from_filename=lambda p: ("tex", p)))
    GdkPixbuf = types.SimpleNamespace(Pixbuf=None)
    for name, mod in (("GLib", GLib), ("GObject", GObject), ("Gdk", Gdk), ("GdkPixbuf", GdkPixbuf)):
        setattr(repo, name, mod)
    gi.repository = repo
    sys.modules["gi"] = gi
    sys.modules["gi.repository"] = repo


def _add_timer(delay, fn, args):
    i = next(_ids)
    _timers[i] = (time.monotonic() + delay, fn, args, delay)
    return i


def pump(seconds=0.5, until=None):
    """Run queued idle callbacks (and due timers) for a while."""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if until is not None and until():
            return True
        try:
            fn, a = _q.get(timeout=0.02)
            fn(*a)
        except queue.Empty:
            pass
        now = time.monotonic()
        for i, (due, fn, a, delay) in list(_timers.items()):
            if due <= now and i in _timers:
                if fn(*a) and i in _timers:
                    _timers[i] = (now + delay, fn, a, delay)
                else:
                    _timers.pop(i, None)
    return until() if until else True

