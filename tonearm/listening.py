"""What your listening says, for the Spotlight page (pure data, testable headless).

Kodi keeps per track only *when it was last played* and *how often*; no history.
So "Jump back in" looks at the tracks of an album played together recently, and the
month's listening time counts each track once.
"""
from __future__ import annotations

import datetime as _dt
from collections import Counter

from .years import clean_year

SESSION_GAP = _dt.timedelta(hours=4)   # plays this close to the album's last play: one sitting
RECENT = _dt.timedelta(days=30)


def _when(s: str) -> _dt.datetime | None:
    try:
        return _dt.datetime.strptime((s or "")[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def _genres(g) -> list[str]:
    if isinstance(g, str):
        g = g.replace(";", "/").replace(",", "/").split("/")
    return [x.strip() for x in g or [] if x and x.strip()]


# -- Jump back in -------------------------------------------------------------
def jump_back_in(songs: list[dict], album_tracks: dict, now: _dt.datetime | None = None,
                 limit: int = 6) -> list[dict]:
    """Albums started recently but not finished.

    ``songs``: recently played tracks ({songid, albumid, lastplayed}).
    ``album_tracks``: albumid → song ids in album order.
    An album counts when, in its last sitting, at least two of its tracks were played,
    and there is a next track after the furthest one played. Returns newest first:
    {albumid, played, total, resume (index into the album), when}."""
    now = now or _dt.datetime.now()
    by_album: dict[int, list] = {}
    for s in songs:
        when = _when(s.get("lastplayed"))
        aid = s.get("albumid")
        if when is None or not aid or now - when > RECENT:
            continue
        by_album.setdefault(aid, []).append((when, s["songid"]))
    out = []
    for aid, plays in by_album.items():
        order = album_tracks.get(aid) or []
        if len(order) < 3:
            continue  # singles and EPs of two: nothing to come back to
        last = max(w for w, _ in plays)
        sitting = {sid for w, sid in plays if last - w <= SESSION_GAP}
        if len(sitting) < 2:
            continue  # one track (radio, shuffle) isn't "listening to the album"
        positions = [order.index(sid) for sid in sitting if sid in order]
        if not positions:
            continue
        resume = max(positions) + 1
        if resume >= len(order) or len(sitting) >= len(order):
            continue  # got to the end
        out.append({"albumid": aid, "played": len(sitting), "total": len(order),
                    "resume": resume, "when": last})
    out.sort(key=lambda a: a["when"], reverse=True)
    return out[:limit]


# -- Radio tiles --------------------------------------------------------------
def decade_label(decade: int) -> str:
    if 1950 <= decade < 2000:
        return f"{decade % 100:02d}s"
    return f"{decade}s"


def radio_picks(songs: list[dict], known_genres: set | None = None, limit: int = 4,
                fallback_genres: list | None = None, fallback_decades: list | None = None
                ) -> list[dict]:
    """Radio stations from what you play most: genre + decade pairs first (a different
    genre each), then a genre or a decade on its own. With no play data: the
    library's biggest genres and decades. Each: {genre, decade, label}."""
    pairs, genres, decades = Counter(), Counter(), Counter()
    canon = {g.lower(): g for g in known_genres or ()}
    for s in songs:
        weight = max(1, int(s.get("playcount") or 0))
        year = clean_year(s.get("year"))
        decade = (year // 10) * 10 if year > 0 else None
        gs = [canon.get(g.lower(), g) for g in _genres(s.get("genre"))]
        if canon:
            gs = [g for g in gs if g.lower() in canon]
        for g in gs[:2]:
            genres[g] += weight
            if decade:
                pairs[(g, decade)] += weight
        if decade:
            decades[decade] += weight

    picks, used_genres, used_decades = [], set(), set()

    def add(genre, decade):
        label = " ".join(x for x in (decade_label(decade) if decade else "", genre or "") if x)
        picks.append({"genre": genre, "decade": decade, "label": label})

    total = sum(genres.values()) or 1
    for (g, d), n in pairs.most_common():
        if len(picks) >= limit - 1:
            break
        if g in used_genres or n < total * 0.03:
            continue
        add(g, d)
        used_genres.add(g)
        used_decades.add(d)
    for g, _n in genres.most_common():
        if len(picks) >= limit - 1:
            break
        if g not in used_genres:
            add(g, None)
            used_genres.add(g)
    for d, _n in decades.most_common():
        if len(picks) >= limit:
            break
        if d not in used_decades:
            add(None, d)
            used_decades.add(d)
    # nothing played yet (or very little): fill from the library's make-up
    for g in fallback_genres or []:
        if len(picks) >= limit:
            break
        if g not in used_genres:
            add(g, None)
            used_genres.add(g)
    for d in fallback_decades or []:
        if len(picks) >= limit:
            break
        if d and d not in used_decades:
            add(None, d)
            used_decades.add(d)
    # room left (a long list): more decades of genres you play, then of the
    # library's biggest genres
    done = {(p["genre"], p["decade"]) for p in picks}
    for (g, d), _n in pairs.most_common():
        if len(picks) >= limit:
            break
        if (g, d) not in done:
            add(g, d)
            done.add((g, d))
    # still short (few distinct genres): a whole decade, then a whole genre, even if
    # it also appears in a pair above ("90s" next to "90s Hip Hop")
    labels = {pk["label"] for pk in picks}
    for d, _n in decades.most_common():
        if len(picks) >= limit:
            break
        if decade_label(d) not in labels:
            add(None, d)
            labels.add(decade_label(d))
    for g, _n in genres.most_common():
        if len(picks) >= limit:
            break
        if g not in labels:
            add(g, None)
            labels.add(g)
    return picks[:limit]


