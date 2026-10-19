"""Sleep timer (pure Python, testable headless).

Kodi's remote API can't schedule a stop, so Tonearm keeps the timer and stops Kodi
itself: after a number of minutes (fading the volume out over the last seconds), at
the end of the playing track, or at the end of the playing album.
"""
from __future__ import annotations

FADE = 12.0          # seconds of volume fade before a timed stop
END_MARGIN = 1.2     # stop this close to a track's end (before Kodi starts the next)

CHOICES = [("off", "Off"), ("15", "15 Minutes"), ("30", "30 Minutes"), ("45", "45 Minutes"),
           ("60", "1 Hour"), ("90", "1½ Hours"), ("track", "End of Track"),
           ("album", "End of Album")]


class SleepTimer:
    def __init__(self):
        self.mode = "off"            # off | minutes | track | album
        self.deadline = 0.0          # monotonic seconds (minutes mode)
        self.album = 0               # the album playing when "album" was chosen

    @property
    def active(self) -> bool:
        return self.mode != "off"

    def set(self, choice: str, now: float, albumid: int = 0):
        """``choice``: one of CHOICES' keys."""
        if choice == "off":
            self.mode = "off"
        elif choice in ("track", "album"):
            self.mode, self.album = choice, albumid
        else:
            self.mode, self.deadline = "minutes", now + int(choice) * 60

    def choice(self) -> str:
        return {"off": "off", "track": "track", "album": "album"}.get(self.mode, "minutes")

    def remaining(self, now: float) -> float:
        return max(0.0, self.deadline - now) if self.mode == "minutes" else 0.0

    def label(self, now: float) -> str:
        """For the player: "Sleep in 23 min", "Sleep after this track", …"""
        if self.mode == "minutes":
            mins = int(self.remaining(now) + 59) // 60
            return f"Sleep in {mins} min" if mins > 1 else "Sleep in under a minute"
        return {"track": "Sleep after this track", "album": "Sleep after this album"}.get(
            self.mode, "")

    def decide(self, now: float, *, playing: bool, position: float, duration: float,
               albumid: int, next_albumid: int | None, last_in_queue: bool) -> str | None:
        """What to do now: "stop", "fade" (lower the volume a step), or None.

        ``next_albumid``: the album of the next queue item (None: not known yet).
        Track / album modes stop just before the track ends, so the next one never
        starts; when a track ends the queue anyway, Kodi stops by itself."""
        if self.mode == "off" or not playing:
            return None
        if self.mode == "minutes":
            left = self.deadline - now
            if left <= 0:
                return "stop"
            return "fade" if left <= FADE else None
        near_end = duration > 0 and duration - position <= END_MARGIN
        if not near_end:
            return None
        if self.mode == "track":
            return "stop"
        # album: stop where the next item is another album (or nothing follows)
        if last_in_queue or (next_albumid is not None and next_albumid != albumid):
            return "stop"
        return None

    def fade_volume(self, start: int, now: float) -> int:
        """The volume for this moment of the fade (from ``start`` down to 0)."""
        left = max(0.0, self.deadline - now)
        return int(round(start * min(1.0, left / FADE)))


__all__ = ["SleepTimer", "CHOICES", "FADE"]
