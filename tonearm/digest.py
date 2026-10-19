"""What the Spotlight page features (pure data, so it is testable headless)."""
from __future__ import annotations

STORIES = 4         # story cards next to the cover story
SPOTLIGHTS = 3      # artists in the spotlight
LIMIT = 120         # albums on the page


def pick(cache) -> dict:
    """Choose the blocks (on a worker thread). Variety first: the story cards are by
    artists other than the cover story's, the spotlight skips artists already featured,
    and the rediscover pick is someone else again, from anywhere in the library."""
    albums = cache.recent_albums(LIMIT)
    picks = {"albums": albums, "hero": None, "stories": [], "spots": [], "rediscover": None,
             "featured_artists": set()}
    if not albums:
        return picks
    hero = albums[0]
    featured = set(cache.album_artist_ids(hero.id))
    names = {(hero.artist or "").lower()}
    stories, spare = [], []
    for a in albums[1:]:
        if len(stories) == STORIES:
            break
        ids = set(cache.album_artist_ids(a.id))
        if (a.artist or "").lower() in names or ids & featured:
            spare.append(a)
            continue
        stories.append(a)
        names.add((a.artist or "").lower())
        featured |= ids
    stories += spare[:STORIES - len(stories)]   # a small library: fill up anyway
    for a in stories:
        featured |= set(cache.album_artist_ids(a.id))

    by_id = {a.id: a for a in albums}
    spots = []
    # in the spotlight: only artists with fanart (their tile *is* the picture)
    for artistid, ids in cache.spotlight_artists([a.id for a in albums[:60]], SPOTLIGHTS * 6,
                                                 exclude=featured):
        if len(spots) == SPOTLIGHTS:
            break
        artist = cache.artist(artistid)
        if artist is not None and artist.fanart:
            spots.append((artist, [by_id[i] for i in ids if i in by_id]))
            featured.add(artistid)
    picks.update(hero=hero, stories=stories, spots=spots, featured_artists=featured,
                 rediscover=rediscover(cache, featured))
    return picks


def rediscover(cache, exclude) -> tuple | None:
    artistid = cache.random_artist(exclude)
    artist = cache.artist(artistid) if artistid is not None else None
    if artist is None:
        return None
    return artist, cache.albums_for_artist(artistid)


def pick_upcoming(items, limit: int = 4, today=None) -> list[dict]:
    """Lidarr's calendar → a few releases for the Spotlight page: out in the last week
    or coming up, soonest first, one per artist, skipping what's already complete.
    Each gets ``days`` (negative: already out) parsed from its releaseDate."""
    import datetime as dt
    today = today or dt.date.today()
    out, seen, again = [], set(), []
    for it in items or []:
        try:
            day = dt.date.fromisoformat((it.get("releaseDate") or "")[:10])
        except ValueError:
            continue
        days = (day - today).days
        if days < -7:
            continue
        st = it.get("statistics") or {}
        have, total = st.get("trackFileCount") or 0, st.get("totalTrackCount") or 0
        if total and have >= total:
            continue  # already in the library
        artist = (it.get("artist") or {}).get("foreignArtistId") or it.get("artistId")
        entry = dict(it, days=days, day=day.isoformat())
        if artist in seen:
            again.append(entry)      # a second release by the same artist: only to fill up
            continue
        seen.add(artist)
        out.append(entry)
    order = lambda a: (a["days"] < 0, abs(a["days"]))   # noqa: E731  soonest first, then just out
    out.sort(key=order)
    if len(out) < limit:
        out += sorted(again, key=order)[:limit - len(out)]
    return out[:limit]


def when_label(days: int, day_iso: str) -> str:
    """"Out today", "Out now", "Tomorrow", "In 3 days", "Fri 24 Oct"."""
    import datetime as dt
    if days == 0:
        return "Out today"
    if days < 0:
        return "Out now"
    if days == 1:
        return "Tomorrow"
    if days < 7:
        return f"In {days} days"
    d = dt.date.fromisoformat(day_iso)
    return d.strftime("%a %-d %b")
