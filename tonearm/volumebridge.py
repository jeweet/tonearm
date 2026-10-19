"""Phone volume keys → Kodi volume.

Mobile shells (Phosh, Plasma Mobile) keep the volume keys to themselves and use
them to change the default audio output's volume. So while Kodi plays, we make
that default output a virtual "Kodi (Tonearm)" sink and mirror its volume to
Kodi, and Kodi's volume back to it:

    volume keys → shell → virtual sink volume ─┐            ┌→ Kodi Application.SetVolume
                                               └ pactl subscribe ┘
    Kodi volume changed elsewhere → pactl set-sink-volume → shell OSD shows it

The phone's own sounds keep playing: the virtual sink's monitor is looped back
to the real speaker (at Kodi's volume level while the bridge is on).

Uses `pactl`, which talks to PipeWire (pipewire-pulse) or PulseAudio. Blocking
calls; run them off the UI thread.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import threading

SINK = "tonearm_kodi"
DESCRIPTION = "Kodi (Tonearm)"
_EVENT = re.compile(r"Event '(\w+)' on (\w+) #(\d+)")
_PERCENT = re.compile(r"(\d+)%")


def available() -> bool:
    return shutil.which("pactl") is not None


def _default_sink() -> str:
    """`pactl get-default-sink` exists since PulseAudio 15; older pactl: parse `pactl info`."""
    try:
        return _pactl("get-default-sink")
    except RuntimeError:
        for line in _pactl("info").splitlines():
            if line.lower().startswith("default sink:"):
                return line.split(":", 1)[1].strip()
        return ""


def _load_first(module: str, variants: list[list[str]]) -> str:
    errors = []
    for args in variants:
        try:
            return _pactl("load-module", module, *args)
        except RuntimeError as exc:
            errors.append(str(exc))
    raise RuntimeError(f"couldn't load {module}:\n" + "\n".join(errors))


def _pactl(*args, timeout=5) -> str:
    res = subprocess.run(["pactl", *args], capture_output=True, text=True, timeout=timeout)
    if res.returncode != 0:
        raise RuntimeError(f"pactl {' '.join(args)}: {res.stderr.strip() or res.returncode}")
    return res.stdout.strip()


class VolumeBridge:
    def __init__(self, on_change):
        """``on_change(volume_percent: int, muted: bool)`` — called from a worker thread
        when the user changes the virtual sink's volume (keys, shell slider)."""
        self.on_change = on_change
        self._lock = threading.RLock()
        self.active = False
        self._modules: list[str] = []
        self._previous_default: str | None = None
        self._sink_index: str | None = None
        self._known: tuple[int, bool] | None = None   # last state we saw or set
        self._sub: subprocess.Popen | None = None

    # -- lifecycle -------------------------------------------------------
    def cleanup_leftovers(self):
        """Remove our modules left behind by a crash (default sink is restored too)."""
        try:
            for line in _pactl("list", "short", "modules").splitlines():
                parts = line.split("\t")
                if len(parts) >= 3 and SINK in parts[2]:
                    _pactl("unload-module", parts[0])
        except Exception:  # noqa: BLE001
            pass

    def start(self, volume: int, muted: bool):
        with self._lock:
            if self.active:
                return
            self.cleanup_leftovers()
            try:
                self._start(volume, muted)
            except Exception:
                self.stop()  # never leave a half-built setup behind
                raise

    def _start(self, volume: int, muted: bool):
        prev = _default_sink()
        if not prev or prev == SINK:
            prev = next((s for s in self._sinks() if s != SINK), None)
        if not prev:
            raise RuntimeError("no audio output to fall back to")
        self._previous_default = prev
        # pipewire-pulse and PulseAudio differ in which module arguments they accept:
        # try the nicest form first, then simpler ones.
        self._modules.append(_load_first("module-null-sink", [
            [f"sink_name={SINK}", f"sink_properties=device.description=\"{DESCRIPTION}\""],
            [f"sink_name={SINK}", "sink_properties=device.description=Kodi-Tonearm"],
            [f"sink_name={SINK}"],
        ]))
        # phone sounds still reach the real speaker
        self._modules.append(_load_first("module-loopback", [
            [f"source={SINK}.monitor", f"sink={prev}", "latency_msec=60",
             "source_dont_move=true", "sink_dont_move=true"],
            [f"source={SINK}.monitor", f"sink={prev}", "latency_msec=60"],
            [f"source={SINK}.monitor", f"sink={prev}"],
        ]))
        self._sink_index = self._index_of(SINK)
        if self._sink_index is None:
            raise RuntimeError(f"the virtual output {SINK} didn't appear in `pactl list short sinks`")
        self._apply(volume, muted)
        _pactl("set-default-sink", SINK)
        self.active = True
        self._sub = subprocess.Popen(["pactl", "subscribe"], stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, text=True, bufsize=1)
        threading.Thread(target=self._watch, args=(self._sub,), name="tonearm-volume",
                         daemon=True).start()

    def stop(self):
        with self._lock:
            if not self.active and not self._modules:
                return
            self.active = False
            if self._sub is not None:
                self._sub.terminate()
                self._sub = None
            try:
                if self._previous_default and _pactl("get-default-sink") == SINK:
                    _pactl("set-default-sink", self._previous_default)
            except Exception:  # noqa: BLE001
                pass
            for mod in reversed(self._modules):
                try:
                    _pactl("unload-module", mod)
                except Exception:  # noqa: BLE001
                    pass
            self._modules = []
            self._known = None

    # -- Kodi → sink -----------------------------------------------------
    def push(self, volume: int, muted: bool):
        """Kodi's volume changed (or we just set it): show it on the virtual sink."""
        with self._lock:
            if self.active and (volume, muted) != self._known:
                self._apply(volume, muted)

    def _apply(self, volume: int, muted: bool):
        volume = max(0, min(100, int(volume)))
        self._known = (volume, bool(muted))
        _pactl("set-sink-volume", SINK, f"{volume}%")
        _pactl("set-sink-mute", SINK, "1" if muted else "0")

    # -- sink → Kodi -----------------------------------------------------
    def _watch(self, proc: subprocess.Popen):
        for line in proc.stdout:
            m = _EVENT.search(line)
            if not m or m.group(1) != "change" or m.group(2) != "sink" or m.group(3) != self._sink_index:
                continue
            try:
                state = self._read()
            except Exception:  # noqa: BLE001
                continue
            with self._lock:
                if not self.active or state == self._known:
                    continue  # our own echo
                self._known = state
            self.on_change(*state)

    def _read(self) -> tuple[int, bool]:
        vol = _PERCENT.search(_pactl("get-sink-volume", SINK))
        mute = _pactl("get-sink-mute", SINK).lower().endswith("yes")
        return (int(vol.group(1)) if vol else 0), mute

    # -- helpers ---------------------------------------------------------
    @staticmethod
    def _sinks() -> list[str]:
        return [ln.split("\t")[1] for ln in _pactl("list", "short", "sinks").splitlines()
                if "\t" in ln]

    @staticmethod
    def _index_of(name: str) -> str | None:
        for ln in _pactl("list", "short", "sinks").splitlines():
            parts = ln.split("\t")
            if len(parts) > 1 and parts[1] == name:
                return parts[0]
        return None


if __name__ == "__main__":  # python3 -m tonearm.volumebridge  — try it out from a terminal
    import sys
    import time

    if not available():
        sys.exit("pactl not found: install pipewire-pulse / pulseaudio-utils")
    print("default output:", _default_sink() or "(none)")
    print("outputs:", ", ".join(VolumeBridge._sinks()) or "(none)")
    b = VolumeBridge(lambda v, m: print(f"volume keys → {v}%{' (muted)' if m else ''}"))
    try:
        b.start(50, False)
    except Exception as exc:  # noqa: BLE001
        sys.exit(f"setup failed:\n{exc}")
    print(f"'{DESCRIPTION}' is now the default output. Press the volume keys for 15 s…")
    try:
        time.sleep(15)
    finally:
        b.stop()
        print("restored:", _default_sink())
