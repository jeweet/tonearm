"""Spotlight: what's new in the library, laid out like a music magazine.

A grid of big and small blocks: the newest album as the cover story, a few more new
arrivals as story cards, the artists with the most new albums in the spotlight and
a random artist from the whole library to rediscover — each block a different artist;
then everything else grouped by when it arrived. Reviews, styles and moods come from
Kodi where the library has them.

The grid has 4 columns on a big screen, 2 on a medium one and 1 on a phone; blocks
change shape with it (a big block spans 2 columns, a small one 1)."""
from __future__ import annotations

import datetime as _dt
import re

from gi.repository import Adw, Gdk, GLib, Gtk, Pango

from ..colors import TILE_COLORS, distinct_colors, dominant_color, stable_index, tune_for_glow
from ..digest import pick, pick_upcoming, rediscover, when_label
from ..lidarr import best_image
from .. import listening
from ..util import fmt_total, plural, run_async
from .columns import WidthBox
from .listview import SizeWatcher
from .widgets import (AlbumTile, Cover, TILE_SIZES, album_menu, attach_context_menu, follow,
                      pill, scrolled, set_menu, status_page)

MAX_WIDTH = 1800                    # the page uses the window's width up to this
SHELF_TILE = TILE_SIZES["medium"]   # one tile size for every shelf on this page …
SHELF_TILE_PHONE = 136              # … but two a row on a phone: 2 × (136 + 16) + 6 =
                                    #   310 px of the 328 a 360 px screen leaves



def grid_columns(width: int) -> int:
    """Bento columns for the content width: phone 1, medium 2, big screen 4."""
    if width < 700:
        return 1
    if width < 1180:
        return 2
    return 4


# -- small helpers ---------------------------------------------------------
def _added(album) -> _dt.datetime | None:
    try:
        return _dt.datetime.strptime((album.dateadded or "")[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        try:
            return _dt.datetime.strptime((album.dateadded or "")[:10], "%Y-%m-%d")
        except ValueError:
            return None


def ago(album, now: _dt.datetime | None = None) -> str:
    when = _added(album)
    if when is None:
        return ""
    now = now or _dt.datetime.now()
    days = (now.date() - when.date()).days
    if days <= 0:
        return "today"
    if days == 1:
        return "yesterday"
    if days < 7:
        return f"{days} days ago"
    if days < 31:
        weeks = days // 7
        return f"{weeks} week{'s' if weeks > 1 else ''} ago"
    return when.strftime("%b %Y") if when.year != now.year else when.strftime("%-d %b")


def group_of(album, now: _dt.datetime | None = None) -> str:
    when = _added(album)
    if when is None:
        return "Earlier"
    days = ((now or _dt.datetime.now()).date() - when.date()).days
    if days < 7:
        return "This Week"
    if days < 31:
        return "This Month"
    return "Earlier"


def album_facts(a, genre_budget: int | None = None) -> str:
    """Year · genres · songs · length. ``genre_budget``: shorten a long genre list to
    about that many characters ("Hip Hop, Neo Soul +9")."""
    genre = short_genres(a.genre, genre_budget) if genre_budget else a.genre
    bits = [str(a.year) if a.year else "", genre,
            plural(a.song_count, 'song') if a.song_count else "",
            fmt_total(a.duration) if a.duration else ""]
    return " · ".join(b for b in bits if b)


def _label(text="", css=(), wrap=False, lines=0, xalign=0.0, **kw) -> Gtk.Label:
    lbl = Gtk.Label(label=text, xalign=xalign, wrap=wrap, **kw)
    if wrap:
        lbl.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
    if lines:
        lbl.set_lines(lines)
        lbl.set_ellipsize(Pango.EllipsizeMode.END)
    elif not wrap:
        lbl.set_ellipsize(Pango.EllipsizeMode.END)
    for c in css:
        lbl.add_css_class(c)
    return lbl


def short_genres(genre: str, budget: int = 34) -> str:
    """A long genre list shortened to what fits one line: "Hip Hop, Neo Soul +9"."""
    parts, seen = [], set()
    for g in re.split(r"[,/;|]", genre or ""):
        g = g.strip()
        if g and g.lower() not in seen:
            seen.add(g.lower())
            parts.append(g)
    shown, used = [], 0
    for g in parts:
        extra = len(g) + (2 if shown else 0)
        if shown and used + extra > budget:
            break
        shown.append(g)
        used += extra
    rest = len(parts) - len(shown)
    return ", ".join(shown) + (f" +{rest}" if rest else "")


def primary_genre(genre: str) -> str:
    return (genre or "").split(",")[0].split("/")[0].split(";")[0].strip()


def overline(text: str) -> Gtk.Label:
    """Small accent-coloured caps above a headline (a magazine's category label)."""
    return _label(text.upper(), ("overline",))



def _clickable(widget, callback):
    """Click / tap opens something. A clickable thing inside a clickable block (a cover
    in an artist block) wins: the block ignores clicks that land on it. (Claiming the
    press instead would stop touch scrolling that starts on a tile.)"""
    widget.add_css_class("clickable")
    click = Gtk.GestureClick()

    def released(_g, _n, x, y):
        hit = widget.pick(x, y, Gtk.PickFlags.DEFAULT)
        while hit is not None and hit is not widget:
            if hit.has_css_class("clickable"):
                return  # an inner clickable handles this one
            hit = hit.get_parent()
        callback()

    click.connect("released", released)
    widget.add_controller(click)
    widget.set_cursor_from_name("pointer")


# -- blocks ----------------------------------------------------------------
ART_DROP = 1.35   # artwork behind blocks is this much taller than the block, top-aligned


def _drop_art(overlay: Gtk.Overlay, picture: Gtk.Picture):
    """Lay ``picture`` taller than the overlay, anchored at the top: the crop then keeps
    the top of the image (faces, band photos) instead of the middle — the image sits
    lower in the block."""
    def position(ov, widget, allocation=None):
        # PyGObject versions differ: some pass the (caller-allocated) rectangle in to be
        # filled, others expect it back with the boolean.
        if widget is not picture:
            return False if allocation is not None else (False, Gdk.Rectangle())
        rect = allocation if allocation is not None else Gdk.Rectangle()
        rect.x, rect.y = 0, 0
        rect.width = ov.get_width()
        rect.height = int(ov.get_height() * ART_DROP)
        return True if allocation is not None else (True, rect)
    overlay.connect("get-child-position", position)


class CardArt(Gtk.Overlay):
    """A card with faint artwork filling its background."""

    def __init__(self, css: str):
        super().__init__(hexpand=True)
        self.add_css_class("card")
        self.add_css_class(css)
        self.set_overflow(Gtk.Overflow.HIDDEN)
        # The picture is an unmeasured overlay, so a 1600 px fanart never sizes the card.
        self.art = Gtk.Picture(content_fit=Gtk.ContentFit.COVER, can_shrink=True, opacity=0,
                               can_target=False)
        self.art.add_css_class("card-art")
        self.add_overlay(self.art)
        _drop_art(self, self.art)

    def set_content(self, widget):
        """The content is the card's main child, so the card is exactly as tall as it
        needs at its width. (As a measured overlay it was measured without a width:
        wrapped text then got too little height and the block sat low — clipped at the
        bottom — e.g. a one-album artist in "From your library".) The picture is moved
        in front of it in the child list, so it's still drawn underneath."""
        self.set_child(widget)
        self.art.insert_before(self, widget)

    def load_art(self, session, key: str, sharp: bool):
        if not key or session.artwork is None:
            return
        if not sharp:
            self.art.add_css_class("soft")

        def done(tex):
            if tex is not None:
                self.art.set_paintable(tex)
                self.art.set_opacity(1)
        session.artwork.load(key, "wide" if sharp else "tiny", done)


def _center(labels, buttons, narrow: bool):
    x = 0.5 if narrow else 0.0
    just = Gtk.Justification.CENTER if narrow else Gtk.Justification.LEFT
    for lbl in labels:
        lbl.set_xalign(x)
        lbl.set_justify(just)
    for w in buttons:
        w.set_halign(Gtk.Align.CENTER if narrow else Gtk.Align.START)


class CoverStory(CardArt):
    """The newest album. ``shape``: "big" (cover beside the text) or "phone" (stacked)."""

    def __init__(self, page, album, shape: str = "big"):
        super().__init__("cover-story")
        window, session = page.window, page.session
        phone = shape == "phone"
        fanart = session.cache.album_fanart(album.id) if session.cache else ""
        self.load_art(session, fanart or album.thumbnail, sharp=bool(fanart))

        row = Gtk.Box(spacing=28, margin_top=24, margin_bottom=24, margin_start=24, margin_end=24,
                      orientation=Gtk.Orientation.VERTICAL if phone else Gtk.Orientation.HORIZONTAL)
        if phone:
            row.set_spacing(16)
            for side in ("top", "bottom", "start", "end"):
                getattr(row, f"set_margin_{side}")(16)
        cover = Cover(240 if phone else 280, css="hero-cover")
        cover.set_art(session, album.thumbnail, "large")
        _clickable(cover, lambda: window.show_album(album))
        attach_context_menu(cover, lambda: album_menu(album.id))

        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, valign=Gtk.Align.CENTER,
                       hexpand=True)
        when = ago(album)
        kicker = overline("Just added" + (f" · {when}" if when else ""))
        title = _label(album.title, ("title-1",), wrap=True)
        # an ellipsizing label: a long artist name must not set the block's minimum width
        artist = Gtk.Button(child=_label(album.artist or "Unknown Artist", (), wrap=True,
                                         lines=2, xalign=0.5 if phone else 0.0,
                                         justify=Gtk.Justification.CENTER if phone
                                         else Gtk.Justification.LEFT))
        artist.add_css_class("flat")
        artist.add_css_class("story-artist")
        artist.set_action_name("win.artist-of-album")
        artist.set_action_target_value(GLib.Variant("i", album.id))
        # one line: the genre list is shortened ("Hip Hop, Neo Soul +9"), the full list
        # is the tooltip
        facts = _label(album_facts(album, 22 if phone else 34), ("dim-label",))
        if album.genre:
            facts.set_tooltip_text(album.genre)
        self.review = _label("", ("body",), wrap=True, lines=3 if phone else 5, visible=False)
        buttons = Gtk.Box(spacing=8 if phone else 12, margin_top=10)
        play = pill("Play", "tonearm-play-symbolic", True)
        play.connect("clicked", lambda *_: session.play_album(album.id))
        shuffle = pill("Shuffle", "tonearm-shuffle-symbolic")
        if phone:  # Adwaita's pills are wide (32 px side padding): smaller ones fit a phone
            play.add_css_class("small")
            shuffle.add_css_class("small")
        shuffle.connect("clicked", lambda *_: session.play_album(album.id, shuffle=True))
        more = Gtk.MenuButton(icon_name="tonearm-more-symbolic", valign=Gtk.Align.CENTER,
                              tooltip_text="More")
        set_menu(more, album_menu(album.id))
        more.add_css_class("circular")
        for w in (play, shuffle, more):
            buttons.append(w)
        for w in (kicker, title, artist, facts, self.review, buttons):
            text.append(w)
        _center((kicker, title, facts), (artist, buttons), phone)
        self._phone = phone
        row.append(cover)
        row.append(text)
        self.set_content(row)
        session.fetch_album_info(album.id, self._on_info)

    def _on_info(self, info):
        if not info:
            return
        if info["description"]:
            self.review.set_label(info["description"])
            self.review.set_visible(True)


def texture_pixels(tex):
    """(bytes, width, height, rowstride, channels) of a Gdk.Texture, or None."""
    try:
        if hasattr(Gdk, "TextureDownloader"):              # GTK ≥ 4.10
            dl = Gdk.TextureDownloader.new(tex)
            dl.set_format(Gdk.MemoryFormat.R8G8B8A8)
            data, stride = dl.download_bytes()
            return data.get_data(), tex.get_width(), tex.get_height(), stride, 4
        pb = Gdk.pixbuf_get_from_texture(tex)
        return (pb.get_pixels(), pb.get_width(), pb.get_height(), pb.get_rowstride(),
                pb.get_n_channels())
    except Exception:  # noqa: BLE001 — no colour is fine
        return None


class Glow(Gtk.Box):
    """A soft blur of colour: a wide radial glow of the cover's dominant colour behind
    the cover, and a fainter one at the far end. Drawn with a CSS radial gradient (one
    shared style provider holds a rule per glow)."""

    _rules: dict = {}
    _provider = None
    _next = 0

    def __init__(self, anchor_x: float):
        super().__init__(hexpand=True, vexpand=True, can_target=False)
        Glow._next += 1
        self._cls = f"tonearm-glow-{Glow._next}"
        self.add_css_class(self._cls)
        self.anchor_x = anchor_x
        self.connect("destroy", lambda *_: Glow._drop(self._cls))

    def set_rgb(self, rgb):
        r, g, b = (int(c * 255) for c in rgb)
        col = lambda a: f"rgba({r},{g},{b},{a:.2f})"  # noqa: E731
        Glow._rules[self._cls] = (
            f".{self._cls} {{ background-image: "
            f"radial-gradient(ellipse farthest-corner at {self.anchor_x:.0f}px 50%, "
            f"{col(0.50)} 0%, {col(0.26)} 40%, {col(0)} 100%), "
            f"radial-gradient(circle farthest-side at 100% 0%, {col(0.22)} 0%, {col(0)} 100%); }}")
        Glow._reload()

    @classmethod
    def _drop(cls, name):
        if cls._rules.pop(name, None) is not None:
            cls._reload()

    @classmethod
    def _reload(cls):
        css = "\n".join(cls._rules.values())
        if cls._provider is None:
            cls._provider = Gtk.CssProvider()
            Gtk.StyleContext.add_provider_for_display(
                Gdk.Display.get_default(), cls._provider,
                Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 1)
        if hasattr(cls._provider, "load_from_string"):     # GTK ≥ 4.12
            cls._provider.load_from_string(css)
            return
        try:                                               # GTK 4.10 binding
            cls._provider.load_from_data(css, -1)
        except TypeError:                                  # older: a byte array
            cls._provider.load_from_data(css.encode())


class StoryCard(Gtk.Overlay):
    """A smaller new arrival: cover beside genre kicker, title, artist, when and a line
    or two of review — compact, so the cover story next to it stays its own height.
    Behind it a blurred glow in the cover's dominant colour."""

    def __init__(self, page, album, compact: bool = False):
        super().__init__(valign=Gtk.Align.FILL)
        self.add_css_class("card")
        self.add_css_class("story-card")
        self.set_overflow(Gtk.Overflow.HIDDEN)
        self.album = album
        session = page.session
        size = 72 if compact else 96
        self.glow = Glow(anchor_x=12 + size / 2)
        self.set_child(self.glow)

        row = Gtk.Box(spacing=14, margin_top=12, margin_bottom=12, margin_start=12,
                      margin_end=12)
        cover = Cover(size)
        cover.set_art(session, album.thumbnail, "small")
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True,
                       valign=Gtk.Align.CENTER)
        text.append(overline(primary_genre(album.genre) or "New album"))
        text.append(_label(album.title, ("heading",), wrap=True, lines=2))
        when = ago(album)
        text.append(_label(" · ".join(b for b in (album.artist, when) if b), ("dim-label",)))
        self.review = _label("", ("caption", "dim-label"), wrap=True, lines=2, visible=False,
                             margin_top=4)
        text.append(self.review)
        row.append(cover)
        row.append(text)
        self.add_overlay(row)
        self.set_measure_overlay(row, True)

        _clickable(self, lambda: page.window.show_album(album))
        attach_context_menu(self, lambda: album_menu(album.id))
        session.fetch_album_info(album.id, self._on_info)
        self._base = None
        self._styles = Adw.StyleManager.get_default()
        self._dark_id = self._styles.connect("notify::dark", lambda *_: self._apply_glow())
        self.connect("destroy", lambda *_: self._styles.disconnect(self._dark_id))
        if album.thumbnail and session.artwork:
            session.artwork.load(album.thumbnail, "tiny", self._on_tiny)

    def _on_tiny(self, tex):
        if tex is None:
            return
        px = texture_pixels(tex)
        if px is None:
            return
        self._base = dominant_color(*px)
        self._apply_glow()

    def _apply_glow(self):
        if self._base is not None:
            self.glow.set_rgb(tune_for_glow(self._base, self._styles.get_dark()))

    def _on_info(self, info):
        if info and info["description"]:
            self.review.set_label(info["description"])
            self.review.set_visible(True)


class Spotlight(CardArt):
    """An artist block. ``shape``: "big" (avatar beside the text, bio, up to 6 covers,
    Play) or "small" (stacked and centred, short bio, up to 4 covers, no Play).
    ``rediscover``: the random pick from the library, with Shuffle and "another one"."""

    def __init__(self, page, artist, albums: list, shape="big", kicker="",
                 rediscover=False):
        super().__init__("spotlight")
        self.page, self.artist = page, artist
        window, session = page.window, page.session
        big = shape == "big"
        self.load_art(session, artist.fanart or artist.thumbnail, sharp=bool(artist.fanart))

        row = Gtk.Box(spacing=24 if big else 10, margin_top=20, margin_bottom=20,
                      margin_start=20, margin_end=20,
                      orientation=Gtk.Orientation.HORIZONTAL if big else Gtk.Orientation.VERTICAL)
        self.avatar = Adw.Avatar(size=128 if big else 88, text=artist.name, show_initials=True,
                                 valign=Gtk.Align.CENTER, halign=Gtk.Align.CENTER)
        if artist.thumbnail:
            self._load_avatar(artist.thumbnail)
        _clickable(self.avatar, lambda: window.show_artist(artist.id))

        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, hexpand=True,
                       valign=Gtk.Align.CENTER)
        kicker_lbl = overline(kicker)
        name = _label(artist.name, ("title-2" if big else "title-3",), wrap=True)
        n = artist.album_count
        # one line: a long genre list is shortened ("Hip Hop, Neo Soul +9"), the line
        # itself ellipsized as a last resort on a narrow phone
        # albums, years, genres — the order of the artist list's subtitles
        facts = _label(" · ".join(b for b in (
            plural(n, 'album') if rediscover and n else "", artist.years,
            short_genres(artist.genre, 34 if big else 24)) if b),
            ("dim-label",))
        self.rediscover = rediscover
        # the rediscover block shows no biography: picture, name, facts and covers
        self.bio = _label("", ("body",) if big else ("caption",), wrap=True,
                          lines=3 if big else 2, visible=False, margin_top=2)

        covers = Gtk.Box(spacing=8, margin_top=8)
        for a in albums[:6 if big else 4]:
            c = Cover(72 if big else 52)
            c.set_art(session, a.thumbnail, "small")
            c.set_tooltip_text(a.title)
            _clickable(c, lambda a=a: window.show_album(a))
            attach_context_menu(c, lambda a=a: album_menu(a.id))
            covers.append(c)

        for w in (kicker_lbl, name, facts, self.bio, covers):
            text.append(w)
        _center((kicker_lbl, name, facts, self.bio), (covers,), not big)
        # no buttons: the block itself opens the artist (the covers open their album)
        _clickable(self, lambda: window.show_artist(artist.id))
        if big:  # small blocks are text and covers only
            row.append(self.avatar)
        row.append(text)
        self.set_content(row)

        if not rediscover:  # "From your library" never shows a biography
            if artist.description:
                self._set_bio(artist.description)
            else:
                session.fetch_artist_description(artist.id, self._set_bio)
        if not artist.thumbnail and not artist.fanart:
            session.fetch_artist_art(artist.id, self._on_art)

    def _load_avatar(self, key):
        art = self.page.session.artwork
        if art is not None:
            art.load(key, "small", lambda tex: tex is not None and self.avatar.set_custom_image(tex))

    def _on_art(self, art):
        thumb, fanart = art or ("", "")
        if thumb:
            self._load_avatar(thumb)
        if fanart or thumb:
            self.load_art(self.page.session, fanart or thumb, sharp=bool(fanart))

    def _set_bio(self, text):
        text = (text or "").strip()
        self.bio.set_label(text)
        self.bio.set_visible(bool(text))


class SpotlightTile(Gtk.Overlay):
    """A small spotlight block: the artist's fanart, unmasked, with a short caption on
    it. The whole tile opens the artist."""

    HEIGHT = 220

    def __init__(self, page, artist, n_new: int, height: int = HEIGHT):
        super().__init__(hexpand=True)
        self.add_css_class("card")
        self.add_css_class("spotlight-tile")
        self.set_overflow(Gtk.Overflow.HIDDEN)
        self.page, self.artist = page, artist
        # size comes from this spacer; the picture is an unmeasured overlay
        self.set_child(Gtk.Box(height_request=height))
        self.placeholder = Adw.Avatar(size=96, text=artist.name, show_initials=True,
                                      halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER,
                                      margin_bottom=40)
        self.add_overlay(self.placeholder)
        self.picture = Gtk.Picture(content_fit=Gtk.ContentFit.COVER, can_shrink=True,
                                   can_target=False, visible=False)
        self.add_overlay(self.picture)
        _drop_art(self, self.picture)

        # a bar across the bottom: the labels get the tile's full width to wrap in,
        # instead of a shrink-wrapped box that ellipsizes them early
        caption = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2,
                          halign=Gtk.Align.FILL, valign=Gtk.Align.END,
                          margin_start=10, margin_end=10, margin_bottom=10, can_target=False)
        caption.add_css_class("spot-caption")
        kicker = _label(f"In the spotlight · {n_new} new".upper(), ("overline",), wrap=True)
        caption.append(kicker)
        caption.append(_label(artist.name, ("title-4",), wrap=True))
        facts = " · ".join(b for b in (artist.years, short_genres(artist.genre, 24)) if b)
        if facts:
            caption.append(_label(facts, ("caption",)))  # one line
        self.add_overlay(caption)

        _clickable(self, lambda: page.window.show_artist(artist.id))
        self.set_tooltip_text(f"Go to {artist.name}")
        key = artist.fanart or artist.thumbnail
        if key:
            self._load(key)
        else:
            page.session.fetch_artist_art(artist.id, self._on_art)

    def _load(self, key):
        art = self.page.session.artwork
        if art is None:
            return

        def done(tex):
            if tex is not None:
                self.picture.set_paintable(tex)
                self.picture.set_visible(True)
                self.placeholder.set_visible(False)
        art.load(key, "large", done)

    def _on_art(self, art):
        thumb, fanart = art or ("", "")
        if fanart or thumb:
            self._load(fanart or thumb)


def _heading(title: str, sub: str) -> Gtk.Box:
    """A section title with a small grey note beside it ("Coming Soon  from Lidarr")."""
    head = Gtk.Box(spacing=10)
    head.append(_label(title, ("title-3",)))
    head.append(_label(sub, ("dim-label", "caption"), valign=Gtk.Align.CENTER))
    return head


class UpcomingCard(Gtk.Box):
    """A release Lidarr expects: cover with a little calendar badge, when, title,
    artist and type. Dashed outline: it's not in the library yet. Opens the artist's
    Lidarr page."""

    def __init__(self, page, item: dict):
        super().__init__(spacing=14)
        self.add_css_class("upcoming-card")
        session = page.session
        artist = item.get("artist") or {}
        cover_box = Gtk.Overlay(valign=Gtk.Align.CENTER)
        cover = Cover(88)
        cover.set_art(session, best_image(item, ("cover", "poster", "disc"))
                      or best_image(artist), "small")
        cover_box.set_child(cover)
        import datetime as dt
        day = dt.date.fromisoformat(item["day"])
        badge = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, halign=Gtk.Align.START,
                        valign=Gtk.Align.START, margin_start=4, margin_top=4)
        badge.add_css_class("date-badge")
        month = Gtk.Label(label=day.strftime("%b").upper())
        month.add_css_class("date-badge-month")
        num = Gtk.Label(label=str(day.day))
        num.add_css_class("date-badge-day")
        badge.append(month)
        badge.append(num)
        cover_box.add_overlay(badge)

        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True,
                       valign=Gtk.Align.CENTER)
        when = when_label(item["days"], item["day"])
        text.append(overline(when))
        text.append(_label(item.get("title") or "Untitled", ("heading",), wrap=True, lines=2))
        text.append(_label(artist.get("artistName") or "", ("dim-label",)))
        kind = (item.get("albumType") or "Album").title()
        st = item.get("statistics") or {}
        tracks = st.get("totalTrackCount") or 0
        text.append(_label(" · ".join(b for b in (
            kind, f"{tracks} tracks" if tracks else "") if b), ("caption", "dim-label")))
        self.append(cover_box)
        self.append(text)
        if artist:
            _clickable(self, lambda: page.window.show_lidarr_artist(artist))
            self.set_tooltip_text(f"{artist.get('artistName', '')} in Lidarr")


class JumpCard(Gtk.Box):
    """An album left half-way: cover, title, artist, how far you got; a click plays on
    from the next track."""

    def __init__(self, page, jump: dict, album):
        super().__init__(spacing=12)
        self.add_css_class("card")
        self.add_css_class("jump-card")
        session = page.session
        cover = Cover(56)
        cover.set_art(session, album.thumbnail, "small")
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3, hexpand=True,
                       valign=Gtk.Align.CENTER)
        text.append(_label(album.title, ("heading",)))
        text.append(_label(album.artist or "", ("dim-label", "caption")))
        bar = Gtk.ProgressBar(fraction=jump["resume"] / max(1, jump["total"]), margin_top=4)
        bar.add_css_class("jump-progress")
        text.append(bar)
        # a round accent-coloured play button (the whole card is the click target)
        # (no hexpand anywhere in it: an expanding child would make the button share
        # the card's spare width with the text, so it drifted to a different spot on
        # every card — now it always sits against the right edge)
        play = Gtk.CenterBox(valign=Gtk.Align.CENTER, halign=Gtk.Align.END, hexpand=False)
        play.add_css_class("jump-play")
        play.set_center_widget(Gtk.Image(icon_name="tonearm-play-symbolic"))
        self.append(cover)
        self.append(text)
        self.append(play)
        nxt = jump["resume"] + 1
        self.set_tooltip_text(f"Play on from track {nxt} of {jump['total']}")
        _clickable(self, lambda: session.play_album(album.id, jump["resume"]))
        attach_context_menu(self, lambda: album_menu(album.id))


RADIO_COUNT = 8     # radio tiles on the Spotlight page (two rows of four)


def radio_colors(picks: list[dict], per_row: int = 4) -> list[int]:
    """A colour per station: its own (from the label, so it keeps it between visits)
    unless another tile already has it — no colour twice."""
    return distinct_colors([stable_index(p["label"]) for p in picks])


class RadioTile(Gtk.Box):
    """A big coloured tile that starts a radio for a genre / decade straight away."""

    def __init__(self, page, pick: dict, color: int):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.add_css_class("radio-tile")
        self.add_css_class(f"c{color % TILE_COLORS}")
        icon = Gtk.Image(icon_name="tonearm-radio-symbolic", halign=Gtk.Align.START,
                         pixel_size=20)
        title = _label(pick["label"], ("radio-tile-title",), wrap=True, lines=2, vexpand=True,
                       valign=Gtk.Align.END)
        self.append(icon)
        self.append(title)
        self.append(_label("Radio", ("radio-tile-sub",)))
        self.set_tooltip_text(f"Start a {pick['label']} radio on Kodi")
        _clickable(self, lambda: page.session.start_radio(pick["decade"], pick["genre"]))


# -- page ------------------------------------------------------------------
class RecentPage(Adw.NavigationPage):
    def __init__(self, window):
        super().__init__(title="Spotlight", tag="recent")
        self.window = window
        self.session = window.session
        self.dirty = True
        self._picks: dict | None = None
        self._cols = None
        self._expanded: set = set()      # shelves opened with Show More

        tv = Adw.ToolbarView()
        header = window.root_header()
        tv.add_top_bar(header)

        # bento grid on top, grouped sections below; only the grid changes with the width
        self.bento = Adw.Bin()
        self.sections = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=36)
        page_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=36, margin_top=16,
                           margin_bottom=36, margin_start=16, margin_end=16)
        self.upcoming_slot = Adw.Bin(visible=False)   # coming soon, from Lidarr
        self._upcoming: list = []
        # "Highlights": the bento grid under its own heading, like the other sections
        top = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        top.append(_heading("Highlights", "new in your library"))
        top.append(self.bento)
        self.jump_slot = Adw.Bin(visible=False)     # Jump Back In: albums left half-way
        self.listen_slot = Adw.Bin(visible=False)   # radio tiles
        self._listening = None
        page_box.append(top)
        page_box.append(self.jump_slot)
        page_box.append(self.upcoming_slot)
        page_box.append(self.listen_slot)
        page_box.append(self.sections)
        # elastic: the bento grid's minimum width never holds the window wide while the
        # page re-lays itself out for a narrower width
        width_box = WidthBox(self._on_width, elastic=True)
        width_box.set_vexpand(False)
        width_box.append(Adw.Clamp(maximum_size=MAX_WIDTH, tightening_threshold=MAX_WIDTH,
                                   child=page_box))
        self.scroller = scrolled(width_box)
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.stack.add_named(self.scroller, "page")
        self.stack.add_named(status_page(
            "tonearm-spotlight-symbolic", "Nothing Added Yet",
            "New albums show up here once Kodi has them and Tonearm has synced."), "empty")
        tv.set_content(self.stack)
        self.set_child(tv)
        self.connect("showing", lambda *_: self._on_showing())
        follow(self.session, "lidarr-changed", self, lambda page: page._load_upcoming())

    # -- data ------------------------------------------------------------
    def refresh(self):
        self.dirty = False
        cache = self.session.cache
        if cache is None:
            self._show(None)
            return

        def load():
            return pick(cache)

        run_async(load, done=self._show)
        self._load_upcoming()
        self._load_listening()

    def _load_listening(self):
        """Play data from Kodi → Jump Back In and the radio tiles."""
        cache = self.session.cache

        def got(data, _from_cache):
            if cache is None or cache is not self.session.cache:
                return

            def work():
                recent, top = data.get("recent") or [], data.get("top") or []
                jumps = listening.jump_back_in(
                    recent, cache.album_track_order({s.get("albumid") for s in recent
                                                     if s.get("albumid")}), limit=4)
                jump_albums = [(j, cache.album(j["albumid"])) for j in jumps]
                lib_genres = sorted(cache.genres(), key=lambda g: -g[1])
                radios = listening.radio_picks(
                    top, {g for g, _n, _c in lib_genres}, RADIO_COUNT,
                    fallback_genres=[g for g, _n, _c in lib_genres[:RADIO_COUNT]],
                    fallback_decades=[d for d, _n, _c in sorted(cache.decades(),
                                                               key=lambda d: -d[1])[:RADIO_COUNT]])
                return {"jumps": [(j, a) for j, a in jump_albums if a is not None],
                        "radios": radios}

            def done(res):
                self._listening = res
                self._build_listening()

            run_async(work, done=done)

        self.session.fetch_listening(got)
    def _load_upcoming(self):
        """Lidarr's calendar (when Lidarr is set up): its cached copy at once, then a
        fresh one if that copy is older than half an hour."""
        client = self.session.lidarr
        if client is None:
            self._upcoming = []
            self._build_upcoming()
            return
        cached = client.cached("upcoming")
        if cached is not None:
            self._upcoming = cached
            self._build_upcoming()
        if client.cached("upcoming", max_age=1800) is not None:
            return

        def done(items):
            if items is not None and client is self.session.lidarr:
                self._upcoming = items
                self._build_upcoming()

        self.session.lidarr_run(client.upcoming, done=done, toast_errors=False)

    def _show(self, picks):
        self._picks = picks
        if not picks or not picks["albums"]:
            self.stack.set_visible_child_name("empty")
            return
        self.stack.set_visible_child_name("page")
        self._build_bento()
        self._build_sections()

    def _on_showing(self):
        """Every visit: a new artist to rediscover, and fresh play data (it moves on
        with every track). A changed library rebuilds the whole page instead."""
        if self.dirty:
            self.refresh()
            return
        if self._picks:
            self.reroll_rediscover()
        self._load_listening()

    def reroll_rediscover(self):
        cache, picks = self.session.cache, self._picks
        if cache is None or not picks:
            return

        def load():
            return rediscover(cache, picks["featured_artists"] | {picks["rediscover"][0].id}
                               if picks["rediscover"] else picks["featured_artists"])

        def done(res):
            if res is not None and picks is self._picks:
                picks["rediscover"] = res
                self._fill_rediscover()

        run_async(load, done=done)


    # -- width -------------------------------------------------------------
    def _on_width(self, width):
        cols = grid_columns(min(width, MAX_WIDTH) - 32)
        if cols != self._cols:
            self._cols = cols
            GLib.idle_add(self._rebuild_bento)  # not while GTK is allocating

    def _rebuild_bento(self):
        if self._picks and self._picks["albums"]:
            self._build_bento()
            self._build_upcoming()
            self._build_listening()
            self._build_sections()   # how many tiles a shelf shows depends on the width
        return False

    # -- Jump Back In, radio tiles ---------------------------------
    def _build_listening(self):
        data = self._listening
        if not data:
            return
        phone = (self._cols or 1) == 1
        # Jump Back In: compact cards right under the Highlights
        jumps = data["jumps"][:2 if phone else 4]
        self.jump_slot.set_visible(bool(jumps))
        if jumps:
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
            box.append(_heading("Jump Back In", "albums you left halfway"))
            grid = Gtk.Grid(column_homogeneous=True, column_spacing=12, row_spacing=12)
            per_row = 1 if phone else (2 if self._cols == 2 else 4)
            for i, (j, album) in enumerate(jumps):
                grid.attach(JumpCard(self, j, album), i % per_row, i // per_row, 1, 1)
            box.append(grid)
            self.jump_slot.set_child(box)
        else:
            self.jump_slot.set_child(None)

        # two rows of radio stations: 8 tiles side by side, 6 on a phone (2 a row)
        radios = data["radios"][:6 if phone else RADIO_COUNT]
        if not radios:
            self.listen_slot.set_visible(False)
            return
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        if radios:
            head = Gtk.Box(spacing=10)
            head.append(_label("Start a Radio", ("title-3",)))
            head.append(_label("from what you play most", ("dim-label", "caption"),
                               valign=Gtk.Align.CENTER))
            box.append(head)
            grid = Gtk.Grid(column_homogeneous=True, column_spacing=12, row_spacing=12)
            per_row = 2 if phone else 4
            for i, (rp, color) in enumerate(zip(radios, radio_colors(radios, per_row))):
                grid.attach(RadioTile(self, rp, color), i % per_row, i // per_row, 1, 1)
            box.append(grid)
        self.listen_slot.set_child(box)
        self.listen_slot.set_visible(True)

    def _build_upcoming(self):
        """Releases Lidarr expects, as cards right under the Highlights: always whole
        rows — two rows of 4 / 2 side by side, 4 cards on a phone — and never a
        half-empty last row (fewer releases: as many full rows as there are)."""
        per_row = 1 if (self._cols or 1) == 1 else (2 if self._cols == 2 else 4)
        want = 4 if per_row == 1 else 2 * per_row
        items = pick_upcoming(self._upcoming, want)
        if len(items) > per_row:
            items = items[:len(items) - len(items) % per_row]
        self.upcoming_slot.set_visible(bool(items))
        if not items:
            self.upcoming_slot.set_child(None)
            return
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.append(_heading("Coming Soon", "from Lidarr"))
        grid = Gtk.Grid(column_homogeneous=True, column_spacing=12, row_spacing=12)
        for i, it in enumerate(items):
            grid.attach(UpcomingCard(self, it), i % per_row, i // per_row, 1, 1)
        # a single short row (1–3 releases): keep the cards their usual width
        for i in range(len(items), per_row):
            grid.attach(Gtk.Box(), i, 0, 1, 1)
        box.append(grid)
        self.upcoming_slot.set_child(box)

    # -- bento grid ------------------------------------------------------
    def _build_bento(self):
        """
        4 columns                          2 columns            1 column
        [ cover story ][ new ][ new ]      [ cover story  ]     everything
        [             ][ new ][ new ]      [ new ][ new   ]     stacked,
        [ spotlight   ][ sp  ][ sp  ]      [ new ][ new   ]     small shapes
        [ rediscover (full width)   ]      [ spotlight    ]
                                           [ sp  ][ sp    ]
                                           [ rediscover   ]
        """
        cols = self._cols or 1
        picks = self._picks
        grid = Gtk.Grid(column_homogeneous=True, column_spacing=12, row_spacing=12)
        phone = cols == 1
        stories = picks["stories"]

        def place(widget, col, row, w=1, h=1):
            grid.attach(widget, col, row, w, h)

        hero = CoverStory(self, picks["hero"], "phone" if phone else "big")
        if cols == 4:
            # hero 2×2, the new arrivals 2×2 beside it
            place(hero, 0, 0, 2, 2)
            for i, a in enumerate(stories):
                place(StoryCard(self, a), 2 + i % 2, i // 2)
            row = 2
        else:
            place(hero, 0, 0, cols)
            row = 1
            for i, a in enumerate(stories):
                place(StoryCard(self, a, compact=phone), i % cols, row + i // cols)
            row += (len(stories) + cols - 1) // cols
        row = self._place_spotlights(place, picks["spots"], cols, row)
        self._rd_slot = Adw.Bin()
        self._rd_phone = phone
        place(self._rd_slot, 0, row, cols)
        self._fill_rediscover()
        self.bento.set_child(grid)

    def _fill_rediscover(self):
        rd = self._picks["rediscover"]
        self._rd_slot.set_visible(rd is not None)
        if rd is not None:
            artist, albums = rd
            self._rd_slot.set_child(Spotlight(self, artist, albums,
                                              "small" if self._rd_phone else "big",
                                              "From your library · rediscover", rediscover=True))

    def _place_spotlights(self, place, spots, cols, row) -> int:
        """Lays out the spotlight artists from ``row``; returns the next free row."""
        def block(spot, shape):
            artist, albums = spot
            n = len(albums)
            if shape == "small":  # fanart tile: picture, caption, click → artist
                return SpotlightTile(self, artist, n, 200 if cols == 1 else SpotlightTile.HEIGHT)
            return Spotlight(self, artist, albums, shape,
                             f"In the spotlight · {plural(n, 'new album')}")

        if not spots:
            return row
        if cols == 1:
            for i, sp in enumerate(spots):
                place(block(sp, "small"), 0, row + i)
            return row + len(spots)
        first, rest = spots[0], spots[1:]
        if cols == 4:
            # the leading artist big; the others small next to it (or big if alone)
            place(block(first, "big"), 0, row, 4 if not rest else 2)
            if len(rest) == 1:
                place(block(rest[0], "big"), 2, row, 2)
            else:
                for i, sp in enumerate(rest[:2]):
                    place(block(sp, "small"), 2 + i, row)
            return row + 1
        place(block(first, "big"), 0, row, 2)
        if len(rest) == 1:
            place(block(rest[0], "big"), 0, row + 1, 2)
        else:
            for i, sp in enumerate(rest[:2]):
                place(block(sp, "small"), i, row + 1)
        return row + (2 if rest else 1)

    # -- grouped sections -------------------------------------------------
    def _build_sections(self):
        while (child := self.sections.get_first_child()) is not None:
            self.sections.remove(child)
        picks = self._picks
        shown = {picks["hero"].id} | {a.id for a in picks["stories"]}
        rest = [a for a in picks["albums"] if a.id not in shown]
        groups: dict[str, list] = {}
        now = _dt.datetime.now()
        for a in rest:
            groups.setdefault(group_of(a, now), []).append(a)
        only_earlier = list(groups) == ["Earlier"]
        for name in ("This Week", "This Month", "Earlier"):
            if name not in groups:
                continue
            albums = groups[name]
            tiles = []
            for a in albums:
                tile = AlbumTile(SHELF_TILE_PHONE if (self._cols or 1) == 1 else SHELF_TILE,
                                 True)
                tile.bind(self.session, a, " · ".join(b for b in (a.artist, ago(a, now)) if b))
                attach_context_menu(tile, lambda a=a: album_menu(a.id, with_fav=False))
                tiles.append(tile)
            n = len(albums)
            self.sections.append(self._shelf(
                "More New Arrivals" if only_earlier else name,
                plural(n, 'album'), tiles,
                lambda t: self.window.show_album(t.album)))

    def _shelf(self, title, count_text, tiles, on_activate) -> Gtk.Box:
        """One grid layout for every shelf on this page (This Week, …): same tile size,
        exactly two rows first, Show More / Show Less for the rest.

        How many tiles fit a row is measured from the shelf itself (its width and a
        tile's real width) whenever its width changes — an estimate from the page
        width was off by one at some sizes, leaving the second row short."""
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        head = Gtk.Box(spacing=10)
        head.append(_label(title, ("title-3",)))
        if count_text:
            head.append(_label(count_text, ("dim-label", "caption"), valign=Gtk.Align.CENTER))
        head.append(Gtk.Box(hexpand=True))
        box.append(head)
        spacing, max_per_line = 6, 16
        flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, valign=Gtk.Align.START,
                           homogeneous=True, min_children_per_line=1,
                           max_children_per_line=max_per_line, column_spacing=spacing,
                           row_spacing=6, activate_on_single_click=True)
        flow.add_css_class("album-flow")
        flow.connect("child-activated", lambda fb, child: on_activate(child.get_child()))
        for tile in tiles:
            flow.append(tile)
        expanded = [title in self._expanded]
        limit = [2 * 4]                      # until the first width arrives
        more = Gtk.Button(valign=Gtk.Align.CENTER, visible=False)
        more.add_css_class("flat")
        head.append(more)

        def apply():
            for i, tile in enumerate(tiles):
                tile.get_parent().set_visible(expanded[0] or i < limit[0])
            more.set_visible(len(tiles) > limit[0])
            more.set_label("Show Less" if expanded[0] else f"Show More ({len(tiles) - limit[0]})")

        def toggle(*_):
            expanded[0] = not expanded[0]
            (self._expanded.add if expanded[0] else self._expanded.discard)(title)
            apply()

        def on_width(width):
            child = flow.get_first_child()
            if child is None:
                return
            tile_w = child.measure(Gtk.Orientation.HORIZONTAL, -1)[0]
            per_row = max(1, min(max_per_line, (width + spacing) // max(1, tile_w + spacing)))
            if 2 * per_row != limit[0]:
                limit[0] = 2 * per_row
                apply()

        more.connect("clicked", toggle)
        watcher = SizeWatcher(flow, on_width)
        watcher.set_vexpand(False)
        box.append(watcher)
        apply()
        return box


__all__ = ["RecentPage", "ago", "group_of", "album_facts", "grid_columns"]
