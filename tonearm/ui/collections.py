"""Sections: Played (recent / most), Genres, Decades, Playlists."""
from __future__ import annotations

import datetime as _dt

from gi.repository import Adw, Gio, GLib, Gtk, Pango

from ..cache import Album
from ..colors import TILE_COLORS, distinct_colors
from ..util import fmt_time, fmt_total, plural, run_async
from .albumview import AlbumView, view_options_button
from .widgets import (FAVOURITES, AlbumObj, Cover, FavToggle, Spinner, ask_name,
                      attach_context_menu, follow, pill, plain_row, scrolled, set_menu,
                      song_menu, stars_text, status_page)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _ago(ts: str) -> str:
    """'2026-10-15 21:03:00' → 'today' / 'yesterday' / '3 days ago' / '12 Oct 2025'."""
    try:
        then = _dt.datetime.strptime(ts[:19], "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return ""
    days = (_dt.date.today() - then.date()).days
    if days <= 0:
        return f"today {then:%H:%M}"
    if days == 1:
        return "yesterday"
    if days < 7:
        return f"{days} days ago"
    if days < 60:
        return f"{days // 7} weeks ago"
    return then.strftime("%-d %b %Y")


def _album_from_kodi(a: dict) -> Album:
    artist = a.get("displayartist") or ", ".join(a.get("artist") or [])
    genre = ", ".join(a.get("genre") or []) if isinstance(a.get("genre"), list) else (a.get("genre") or "")
    return Album(a.get("albumid", 0), a.get("title") or a.get("label") or "", artist,
                 int(a.get("year") or 0), genre, a.get("thumbnail") or "", "")


# ---------------------------------------------------------------------------
# Played
# ---------------------------------------------------------------------------
PLAYED = {   # kind → (section / tag, title, empty-page text)
    "recent": ("recent-played", "Recently Played", "Albums you play on Kodi show up here."),
    "most": ("most-played", "Most Played", "Your most played albums on Kodi show up here."),
}


class PlayedPage(Adw.NavigationPage):
    """Albums by last played (``kind="recent"``) or play count (``"most"``), asked live
    from Kodi — each its own section in the sidebar."""

    def __init__(self, window, kind: str = "recent"):
        tag, title, empty_text = PLAYED[kind]
        super().__init__(title=title, tag=tag)
        self.window = window
        self.session = window.session
        self.kind = kind
        self.dirty = True

        tv = Adw.ToolbarView()
        header = window.root_header()
        header.pack_end(view_options_button(window))
        tv.add_top_bar(header)
        self.banner = Adw.Banner(title="Offline — showing the last known list")
        tv.add_top_bar(self.banner)

        self.view = AlbumView(window)
        self.store = self.view.store
        self.spinner = Spinner(size=32, halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.stack.add_named(self.view, "grid")
        self.stack.add_named(self.spinner, "loading")
        self.empty = status_page("tonearm-played-symbolic" if kind == "recent"
                                 else "tonearm-most-played-symbolic",
                                 "Nothing Played Yet", empty_text)
        self.stack.add_named(self.empty, "empty")
        tv.set_content(self.stack)
        self.set_child(tv)
        self.connect("showing", lambda *_: self.dirty and self.refresh())

    def refresh(self):
        self.dirty = False
        kind = self.kind
        if not self.store.get_n_items():
            self.spinner.start()
            self.stack.set_visible_child_name("loading")

        def done(albums, from_cache):
            if kind != self.kind:
                return
            self.spinner.stop()
            self.banner.set_revealed(from_cache and bool(albums))
            items = []
            for a in albums:
                alb = _album_from_kodi(a)
                extra = _ago(a.get("lastplayed", "")) if kind == "recent" else \
                    f"{a.get('playcount', 0)} play{'s' if a.get('playcount', 0) != 1 else ''}"
                items.append(AlbumObj(alb, " · ".join(x for x in (alb.artist, extra) if x)))
            self.view.set_albums(items)
            self.stack.set_visible_child_name("grid" if items else "empty")

        self.session.fetch_played(kind, done)


# ---------------------------------------------------------------------------
# Top Rated (Kodi's user ratings)
# ---------------------------------------------------------------------------
class TopRatedPage(Adw.NavigationPage):
    """Rated albums and songs, best first — Kodi's own ratings, kept in the cache (so
    they show offline too). Albums and Songs each a tab."""

    def __init__(self, window):
        super().__init__(title="Top Rated", tag="top-rated")
        self.window = window
        self.session = window.session
        self.dirty = True

        tv = Adw.ToolbarView()
        header = window.root_header()
        header.pack_end(view_options_button(window))
        tv.add_top_bar(header)

        self.tabs = Adw.ViewStack(vexpand=True)
        self.view = AlbumView(window)
        self.album_stack = self._with_empty(
            self.view, "No Rated Albums",
            "Rate albums from their ⋯ menu or page — or in Kodi — and the best show up here.")
        self.tabs.add_titled_with_icon(self.album_stack, "albums", "Albums",
                                       "tonearm-album-symbolic")
        # valign START: a short list stays as tall as its rows (a boxed list stretched
        # to the page's height drew an empty card below the last song)
        self.song_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE,
                                     valign=Gtk.Align.START)
        self.song_list.add_css_class("boxed-list")
        self.song_list.connect("row-activated", lambda _l, row: window.activate_action(
            "win.play-song", GLib.Variant("i", row.songid)))
        songs = Adw.Clamp(maximum_size=960, child=self.song_list, valign=Gtk.Align.START,
                          margin_top=12,
                          margin_bottom=24, margin_start=12, margin_end=12)
        self.song_stack = self._with_empty(
            scrolled(songs), "No Rated Songs",
            "Rate songs from their ⋯ menu or in the player — or in Kodi — and the best "
            "show up here.")
        self.tabs.add_titled_with_icon(self.song_stack, "songs", "Songs",
                                       "tonearm-song-symbolic")
        switcher = (Adw.InlineViewSwitcher(stack=self.tabs, halign=Gtk.Align.CENTER,
                                           margin_top=6, margin_bottom=6)
                    if hasattr(Adw, "InlineViewSwitcher") else
                    Adw.ViewSwitcher(stack=self.tabs, policy=Adw.ViewSwitcherPolicy.WIDE,
                                     halign=Gtk.Align.CENTER))
        tv.add_top_bar(switcher)
        tv.set_content(self.tabs)
        self.set_child(tv)
        follow(self.session, "ratings-changed", self, lambda page: page._ratings_changed())
        follow(self.session, "library-changed", self, lambda page: page._ratings_changed())
        self.connect("showing", lambda *_: self.dirty and self.refresh())

    @staticmethod
    def _with_empty(content, title, text) -> Gtk.Stack:
        st = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        st.add_named(content, "list")
        st.add_named(status_page("tonearm-star-half-symbolic", title, text), "empty")
        return st

    def _ratings_changed(self):
        if self.get_mapped():
            self.refresh()
        else:
            self.dirty = True

    def refresh(self):
        self.dirty = False
        cache = self.session.cache
        if cache is None:
            self.view.set_albums([])
            self.album_stack.set_visible_child_name("empty")
            self.song_stack.set_visible_child_name("empty")
            return

        def done(res):
            albums, songs = res
            self.view.set_albums([AlbumObj(a, " · ".join(x for x in (
                a.artist, stars_text(a.rating)) if x)) for a in albums])
            self.album_stack.set_visible_child_name("list" if albums else "empty")
            self._fill_songs(songs)
            self.song_stack.set_visible_child_name("list" if songs else "empty")

        run_async(lambda: (cache.top_rated_albums(), cache.top_rated_songs()), done=done)

    def _fill_songs(self, songs):
        while (row := self.song_list.get_first_child()) is not None:
            self.song_list.remove(row)
        for song, rating, album, thumb in songs:
            row = plain_row(title=song.title,
                            subtitle=" · ".join(x for x in (song.artist, album) if x),
                            activatable=True)
            cover = Cover(40, icon="tonearm-song-symbolic")
            cover.set_valign(Gtk.Align.CENTER)
            cover.set_art(self.session, thumb, "small")
            row.add_prefix(cover)
            stars = Gtk.Label(label=stars_text(rating))
            stars.add_css_class("rating-text")
            row.add_suffix(stars)
            more = Gtk.MenuButton(icon_name="tonearm-more-symbolic", valign=Gtk.Align.CENTER)
            more.add_css_class("flat")
            more.set_create_popup_func(lambda b, s=song: set_menu(b, song_menu(s.id, s.albumid)))
            row.add_suffix(more)
            row.songid = song.id
            attach_context_menu(row, lambda s=song: song_menu(s.id, s.albumid))
            self.song_list.append(row)


# ---------------------------------------------------------------------------
# Album collections (a genre, a decade)
# ---------------------------------------------------------------------------
class AlbumCollectionPage(Adw.NavigationPage):
    """A pushed grid of albums with Play / Shuffle for the whole set."""

    def __init__(self, window, title: str, tag: str, loader, subtitle=None):
        super().__init__(title=title, tag=tag)
        self.window = window
        self.session = window.session
        self.loader = loader
        self.subtitle = subtitle
        self.dirty = False
        self.albums: list = []

        tv = Adw.ToolbarView()
        header = window.search_header()
        header.pack_end(view_options_button(window))
        tv.add_top_bar(header)
        bar = Gtk.Box(spacing=12, halign=Gtk.Align.CENTER, margin_top=8, margin_bottom=4)
        self.heading = Gtk.Label(label=title, ellipsize=Pango.EllipsizeMode.END)
        self.heading.add_css_class("title-3")
        bar.append(self.heading)
        play = pill("Play", "tonearm-play-symbolic", True)
        play.connect("clicked", lambda *_: self.session.play_albums([a.id for a in self.albums]))
        shuffle = pill("Shuffle", "tonearm-shuffle-symbolic")
        shuffle.connect("clicked", lambda *_: self.session.play_albums([a.id for a in self.albums], True))
        bar.append(play)
        bar.append(shuffle)
        tv.add_top_bar(bar)

        self.view = AlbumView(window)
        self.store = self.view.store
        tv.set_content(self.view)
        self.set_child(tv)
        self.refresh()

    def refresh(self):
        self.dirty = False

        def done(albums):
            self.albums = albums
            self.view.set_albums([AlbumObj(a, self.subtitle(a) if self.subtitle else None)
                                  for a in albums])
            n = len(albums)
            self.heading.set_tooltip_text(plural(n, 'album'))

        run_async(self.loader, done=done)


# ---------------------------------------------------------------------------
# Decades (Genres: genres.py)
# ---------------------------------------------------------------------------
DECADE_COLORS = TILE_COLORS   # .decade-tile.c0 … (style.css), shared with the radio tiles
COVER = 88          # cover size in the fan …
FAN_STEP = 34       # … and how far each one sits left of the one before


def decade_label(decade: int) -> str:
    return f"{decade}s" if decade else "Unknown Year"


class ColorTile(Gtk.Overlay):
    """A wide coloured tile (like the Spotlight radio tiles): a title in big type, a
    line under it, a fan of covers on the right and a round radio button. Used for
    decades and genres. ``data``: what the tile opens (read by its page)."""

    __gtype_name__ = "TonearmColorTile"

    def __init__(self, session, title: str, sub: str, covers: list[str], color: int,
                 on_radio=None, radio_tooltip: str = "", data=None):
        super().__init__()
        self.data = data
        self.add_css_class("color-tile")      # the hover zoom scales all of it, button too
        body = Gtk.Box(spacing=12)
        body.add_css_class("decade-tile")
        body.add_css_class(f"c{color % DECADE_COLORS}")
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True,
                       valign=Gtk.Align.END, margin_top=34)   # room for the radio button
        head = Gtk.Label(label=title, xalign=0, ellipsize=Pango.EllipsizeMode.END)
        head.add_css_class("decade-tile-title")
        line = Gtk.Label(label=sub, xalign=0, ellipsize=Pango.EllipsizeMode.END)
        line.add_css_class("decade-tile-sub")
        text.append(head)
        text.append(line)
        body.append(text)
        # covers fanned out on the right, the newest on top
        shown = covers[:3]
        if shown:
            fan = Gtk.Overlay(valign=Gtk.Align.CENTER, halign=Gtk.Align.END)
            fan.set_size_request(COVER + FAN_STEP * (len(shown) - 1) + 8, COVER + 12)
            fan.set_can_target(False)
            for i, thumb in reversed(list(enumerate(shown))):
                c = Cover(COVER)
                c.add_css_class("decade-cover")
                c.add_css_class(f"fan{i}")
                c.set_halign(Gtk.Align.END)
                c.set_margin_end(4 + i * FAN_STEP)
                c.set_art(session, thumb, "small")
                fan.add_overlay(c)
            body.append(fan)
        self.set_child(body)
        if on_radio is not None:
            # top left: the title sits bottom left, the covers on the right
            radio = Gtk.Button(icon_name="tonearm-radio-symbolic", halign=Gtk.Align.START,
                               valign=Gtk.Align.START, margin_top=10, margin_start=10,
                               tooltip_text=radio_tooltip)
            radio.add_css_class("circular")
            radio.add_css_class("decade-radio")
            radio.connect("clicked", lambda *_: on_radio())
            self.add_overlay(radio)


class DecadeTile(ColorTile):
    """A decade: its albums, a fan of covers from it, a radio from it."""

    __gtype_name__ = "TonearmDecadeTile"

    def __init__(self, session, decade: int, count: int, covers: list[str], color: int):
        super().__init__(session, decade_label(decade),
                         plural(count, 'album'), covers, color,
                         on_radio=lambda: session.start_radio(decade, None),
                         radio_tooltip=f"Start a {decade_label(decade)} radio on Kodi",
                         data=decade)
        self.decade = decade
        self.set_tooltip_text(f"Albums from the {decade_label(decade)}" if decade
                              else "Albums without a year")


class DecadesPage(Adw.NavigationPage):
    """Decades as wide coloured tiles (newest first); a tile opens its albums, its
    round button starts a radio from that decade."""

    def __init__(self, window):
        super().__init__(title="Decades", tag="decades")
        self.window = window
        self.session = window.session
        self.dirty = True
        tv = Adw.ToolbarView()
        tv.add_top_bar(window.root_header())
        self.flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=True,
                                valign=Gtk.Align.START, min_children_per_line=1,
                                max_children_per_line=4, column_spacing=12, row_spacing=12,
                                activate_on_single_click=True)
        self.flow.add_css_class("decade-flow")
        self.flow.connect("child-activated", lambda fb, ch: self._open(ch.get_child().decade))
        box = Adw.Clamp(maximum_size=1400, child=self.flow, margin_top=12, margin_bottom=24,
                        margin_start=12, margin_end=12)
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.stack.add_named(scrolled(box), "grid")
        self.stack.add_named(status_page(
            "tonearm-decade-symbolic", "No Albums",
            "Your library is empty, or it is still being synced from Kodi."), "empty")
        tv.set_content(self.stack)
        self.set_child(tv)
        self.connect("showing", lambda *_: self.dirty and self.refresh())

    @staticmethod
    def _label(decade: int) -> str:
        return decade_label(decade)

    def _open(self, decade):
        cache = self.session.cache
        self.window.push_page(AlbumCollectionPage(
            self.window, self._label(decade), f"decade-{decade}",
            lambda: cache.albums_for_decade(decade),
            subtitle=lambda a: " · ".join(x for x in (str(a.year) if a.year else "", a.artist) if x)))

    def refresh(self):
        self.dirty = False
        cache = self.session.cache
        if cache is None:
            return

        def done(res):
            decades, covers = res
            while (child := self.flow.get_first_child()) is not None:
                self.flow.remove(child)
            # each decade its own colour (consecutive decades: consecutive colours);
            # "Unknown Year" takes one that's free
            colors = distinct_colors([(d // 10) if d else DECADE_COLORS - 1
                                      for d, _n, _t in decades])
            for (d, n, thumb), color in zip(decades, colors):
                self.flow.append(DecadeTile(self.session, d, n,
                                            covers.get(d) or ([thumb] if thumb else []), color))
            self.stack.set_visible_child_name("grid" if decades else "empty")

        run_async(lambda: (cache.decades(), cache.decade_covers(3)), done=done)


# ---------------------------------------------------------------------------
# Playlists
# ---------------------------------------------------------------------------
def playlist_header(title: str, on_play, on_shuffle, extra: Gtk.Widget | None = None):
    """A playlist's simple header: name and song count on the left, round Shuffle and
    Play buttons on the right (``extra``, e.g. a ★, before them). Returns (header,
    title label, meta label, play, shuffle)."""
    head = Gtk.Box(spacing=8, margin_bottom=4)
    texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True,
                    valign=Gtk.Align.CENTER)
    name = Gtk.Label(label=title, xalign=0, ellipsize=Pango.EllipsizeMode.END)
    name.add_css_class("title-2")
    meta = Gtk.Label(xalign=0)
    meta.add_css_class("dim-label")
    texts.append(name)
    texts.append(meta)
    shuffle = Gtk.Button(icon_name="tonearm-shuffle-symbolic", tooltip_text="Shuffle",
                         valign=Gtk.Align.CENTER)
    shuffle.add_css_class("circular")
    shuffle.connect("clicked", lambda *_: on_shuffle())
    play = Gtk.Button(icon_name="tonearm-play-symbolic", tooltip_text="Play",
                      valign=Gtk.Align.CENTER)
    play.add_css_class("circular")
    play.add_css_class("suggested-action")
    play.add_css_class("playlist-play")
    play.connect("clicked", lambda *_: on_play())
    for w in (texts, extra, shuffle, play):
        if w is not None:
            head.append(w)
    return head, name, meta, play, shuffle


def kodi_playlist_menu(pl: dict) -> Gio.Menu:
    """Right-click / long-press on a Kodi playlist: Play, Shuffle — and for a smart
    playlist Kodi's Party Mode from it."""
    m = Gio.Menu()
    for label, action in (("Play", "win.play-kodi-playlist"),
                          ("Shuffle", "win.shuffle-kodi-playlist")):
        it = Gio.MenuItem.new(label, None)
        it.set_action_and_target_value(action, GLib.Variant("s", pl["file"]))
        m.append_item(it)
    if pl.get("smart"):
        it = Gio.MenuItem.new("Party Mode", None)
        it.set_action_and_target_value("win.partymode",
                                       GLib.Variant("(ss)", (pl["file"], pl["label"])))
        m.append_item(it)
    return m


def playlist_star(session, pl: dict) -> Gtk.Button:
    """A small ★ for a Kodi playlist row: in or out of Kodi's Favorites."""
    star = Gtk.Button(icon_name="tonearm-starred-symbolic", valign=Gtk.Align.CENTER)
    star.add_css_class("flat")
    star.add_css_class("track-star")
    star.connect("clicked", lambda *_: session.toggle_playlist_favourite(pl["file"], pl["label"]))
    star.path = pl["file"]
    sync_playlist_star(star)
    return star


def sync_playlist_star(star):
    fav = star.path in FAVOURITES["playlists"]
    (star.add_css_class if fav else star.remove_css_class)("on")
    star.set_tooltip_text("Remove from Favorites" if fav else "Add to Favorites")


class PlaylistsPage(Adw.NavigationPage):
    """Your playlists (made in Tonearm: add, rename, delete, fill from any menu) and
    Kodi's own playlist files (read-only: Kodi's API can't write them)."""

    def __init__(self, window):
        super().__init__(title="Playlists", tag="playlists")
        self.window = window
        self.session = window.session
        self.dirty = True
        tv = Adw.ToolbarView()
        header = window.root_header()
        new = Gtk.Button(icon_name="tonearm-add-symbolic", tooltip_text="New Playlist")
        new.set_action_name("win.playlist-new")
        header.pack_end(new)
        tv.add_top_bar(header)
        self.banner = Adw.Banner(title="Offline — showing the last known Kodi playlists")
        tv.add_top_bar(self.banner)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_top=12,
                      margin_bottom=24, margin_start=12, margin_end=12)
        self.mine = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.mine.add_css_class("boxed-list")
        self.mine.connect("row-activated", lambda lb, row: getattr(row, "mine", None) and
                          self.window.push_page(MyPlaylistPage(self.window, row.mine)))
        box.append(self.mine)
        # none of your own yet (but Kodi has some): one quiet line instead of the list
        self.mine_hint = Gtk.Label(
            label="Make your own playlist with + above, or with “Add to Playlist” in any "
                  "album or song menu.", xalign=0, wrap=True, visible=False)
        self.mine_hint.add_css_class("dim-label")
        box.append(self.mine_hint)
        # Kodi's
        self.kodi_title = Gtk.Label(label="In Kodi", xalign=0, margin_top=18)
        self.kodi_title.add_css_class("title-4")
        box.append(self.kodi_title)
        self.list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.list.add_css_class("boxed-list")
        self.list.connect("row-activated", lambda lb, row: self._open(row.playlist))
        box.append(self.list)
        self.spinner = Spinner(size=24, halign=Gtk.Align.CENTER)
        box.append(self.spinner)
        # no playlists at all: the same placeholder as every other empty page
        empty = status_page("tonearm-playlist-symbolic", "No Playlists",
                            "Make one here, or with “Add to Playlist” in any album or "
                            "song menu")
        create = Gtk.Button(label="New Playlist", halign=Gtk.Align.CENTER)
        create.add_css_class("pill")
        create.add_css_class("suggested-action")
        create.set_action_name("win.playlist-new")
        empty.set_child(create)
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.stack.add_named(scrolled(Adw.Clamp(maximum_size=760, child=box)), "list")
        self.stack.add_named(empty, "empty")
        self._kodi_count = None          # not loaded yet
        tv.set_content(self.stack)
        self.set_child(tv)
        self.connect("showing", lambda *_: self.dirty and self.refresh())
        follow(self.session, "playlists-changed", self, lambda page: page._fill_mine())
        self._stars: list = []     # ★ of the Kodi playlists (they can be Kodi Favorites)
        follow(self.session, "favourites-changed", self,
               lambda page: [sync_playlist_star(s) for s in page._stars])
        self._fill_mine()

    def _open(self, pl):
        self.window.push_page(PlaylistPage(self.window, pl))

    def _fill_mine(self):
        while (row := self.mine.get_first_child()) is not None:
            self.mine.remove(row)
        mine = self.session.my_playlists()
        self.mine.set_visible(bool(mine))
        self.mine_hint.set_visible(not mine)
        self._sync_empty()
        for pl in mine:
            n = len(pl["songs"])
            row = plain_row(title=pl["name"], subtitle=plural(n, 'song'),
                            activatable=True)
            row.add_prefix(Gtk.Image(icon_name="tonearm-playlist-symbolic"))
            play = Gtk.Button(icon_name="tonearm-play-symbolic", valign=Gtk.Align.CENTER,
                              tooltip_text="Play", sensitive=n > 0)
            play.add_css_class("flat")
            play.connect("clicked", lambda b, p=pl: self.session.play_my_playlist(p["id"]))
            row.add_suffix(play)
            row.add_suffix(Gtk.Image(icon_name="tonearm-go-next-symbolic"))
            row.mine = pl["id"]
            self.mine.append(row)

    def refresh(self):
        self.dirty = False
        self._kodi_count = None
        self._fill_mine()
        self.spinner.start()
        self.spinner.set_visible(True)

        def done(pls, from_cache):
            self.spinner.stop()
            self.spinner.set_visible(False)
            self.banner.set_revealed(from_cache and bool(pls))
            while (row := self.list.get_first_child()) is not None:
                self.list.remove(row)
            self._stars = []
            for pl in pls:
                row = plain_row(title=pl["label"], use_markup=False, activatable=True,
                                subtitle="Smart playlist" if pl.get("smart") else "Playlist")
                row.add_prefix(Gtk.Image(icon_name="tonearm-playlist-symbolic"))
                star = playlist_star(self.session, pl)
                self._stars.append(star)
                row.add_suffix(star)
                play = Gtk.Button(icon_name="tonearm-play-symbolic", valign=Gtk.Align.CENTER,
                                  tooltip_text="Play")
                play.add_css_class("flat")
                play.connect("clicked", lambda b, p=pl: self.session.play_playlist(p["file"]))
                row.add_suffix(play)
                row.add_suffix(Gtk.Image(icon_name="tonearm-go-next-symbolic"))
                row.playlist = pl
                attach_context_menu(row, lambda p=pl: kodi_playlist_menu(p))
                self.list.append(row)
            self.kodi_title.set_visible(bool(pls))
            self.list.set_visible(bool(pls))
            self._kodi_count = len(pls)
            self._sync_empty()

        self.session.fetch_playlists(done)

    def _sync_empty(self):
        """The placeholder once we know there are none of yours and none in Kodi."""
        empty = not self.session.my_playlists() and self._kodi_count == 0
        self.stack.set_visible_child_name("empty" if empty else "list")


class MyPlaylistPage(Adw.NavigationPage):
    """One of your playlists: play / shuffle, its songs (remove with ✕), rename, delete."""

    def __init__(self, window, pid: str):
        self.window = window
        self.session = window.session
        self.pid = pid
        pl = self.session.playlists.get(pid) if self.session.playlists else None
        super().__init__(title=pl["name"] if pl else "Playlist", tag=f"my-playlist-{pid}")
        self.dirty = False

        tv = Adw.ToolbarView()
        header = window.search_header()
        menu = Gio.Menu()
        menu.append("Rename…", "page.rename")
        menu.append("Delete Playlist", "page.delete")
        header.pack_end(Gtk.MenuButton(icon_name="tonearm-more-symbolic", menu_model=menu,
                                       tooltip_text="More"))
        tv.add_top_bar(header)
        group = Gio.SimpleActionGroup()
        for name, cb in (("rename", self._rename), ("delete", self._delete)):
            act = Gio.SimpleAction.new(name, None)
            act.connect("activate", lambda *_a, cb=cb: cb())
            group.add_action(act)
        self.insert_action_group("page", group)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_top=18,
                      margin_bottom=24, margin_start=12, margin_end=12)
        head, self.title, self.meta, self.play, self.shuffle = playlist_header(
            pl["name"] if pl else "", lambda: self.session.play_my_playlist(pid),
            lambda: self.session.play_my_playlist(pid, shuffle=True))
        self.tracks = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.tracks.add_css_class("boxed-list")
        self.tracks.connect("row-activated", lambda lb, row: self.session.play_my_playlist(
            pid, row.position))
        self.empty = status_page("tonearm-playlist-symbolic", "Empty Playlist",
                                 "Add songs and albums with “Add to Playlist” in their menus.")
        for w in (head, self.tracks, self.empty):
            box.append(w)
        tv.set_content(scrolled(Adw.Clamp(maximum_size=760, child=box)))
        self.set_child(tv)
        follow(self.session, "playlists-changed", self, lambda page: page.refresh())
        self.refresh()

    def refresh(self):
        pl = self.session.playlists.get(self.pid) if self.session.playlists else None
        if pl is None:          # deleted (here or elsewhere): leave the page
            nav = self.get_parent()
            while nav is not None and not isinstance(nav, Adw.NavigationView):
                nav = nav.get_parent()
            if nav is not None and nav.get_visible_page() is self:
                nav.pop()
            return
        self.set_title(pl["name"])
        self.title.set_label(pl["name"])
        while (row := self.tracks.get_first_child()) is not None:
            self.tracks.remove(row)
        cache, total = self.session.cache, 0
        for pos, entry in enumerate(pl["songs"]):
            song = cache.song(entry["id"]) if cache else None
            album = cache.album(song.albumid) if cache and song else None
            title = song.title if song else (entry.get("title") or "Missing song")
            artist = song.artist if song else entry.get("artist", "")
            sub = " · ".join(x for x in (artist, album.title if album else "") if x)
            row = plain_row(title=title, subtitle=sub, activatable=song is not None)
            num = Gtk.Label(label=str(pos + 1), width_chars=3, xalign=1)
            num.add_css_class("dim-label")
            num.add_css_class("numeric")
            row.add_prefix(num)
            if song is not None:
                total += song.duration
                dur = Gtk.Label(label=fmt_time(song.duration))
                dur.add_css_class("dim-label")
                dur.add_css_class("numeric")
                row.add_suffix(dur)
                attach_context_menu(row, lambda sid=song.id, aid=song.albumid: song_menu(sid, aid))
            else:
                row.set_subtitle("No longer in Kodi's library")
            rm = Gtk.Button(icon_name="tonearm-remove-symbolic", valign=Gtk.Align.CENTER,
                            tooltip_text="Remove from Playlist")
            rm.add_css_class("flat")
            rm.connect("clicked", lambda b, i=pos: self.session.remove_from_playlist(self.pid, i))
            row.add_suffix(rm)
            row.position = pos
            self.tracks.append(row)
        n = len(pl["songs"])
        self.meta.set_label(plural(n, 'song')
                            + (f" · {fmt_total(total)}" if total else ""))
        self.tracks.set_visible(n > 0)
        self.empty.set_visible(n == 0)
        self.play.set_sensitive(n > 0)
        self.shuffle.set_sensitive(n > 0)

    def _rename(self):
        pl = self.session.playlists.get(self.pid)
        if pl is not None:
            ask_name(self.window, "Rename Playlist", pl["name"], "Rename",
                     lambda name: self.session.rename_playlist(self.pid, name))

    def _delete(self):
        pl = self.session.playlists.get(self.pid)
        if pl is None:
            return
        dlg = Adw.AlertDialog(heading=f"Delete “{pl['name']}”?",
                              body="The playlist goes; the music stays in your library.")
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("delete", "Delete")
        dlg.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dlg.set_close_response("cancel")
        dlg.connect("response", lambda d, r: r == "delete" and
                    self.session.delete_playlist(self.pid))
        dlg.present(self.window)


class PlaylistPage(Adw.NavigationPage):
    """One of Kodi's own playlists (smart or .m3u): its tracks, Play / Shuffle, ★."""

    def __init__(self, window, pl: dict):
        super().__init__(title=pl["label"], tag=f"playlist-{pl['file']}")
        self.window = window
        self.session = window.session
        self.pl = pl
        self.dirty = False

        tv = Adw.ToolbarView()
        tv.add_top_bar(window.search_header())
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_top=18,
                      margin_bottom=24, margin_start=12, margin_end=12)
        # Kodi's playlists can be Kodi Favorites (ours can't: Kodi doesn't know them)
        self.fav = FavToggle(lambda: self.session.toggle_playlist_favourite(pl["file"],
                                                                            pl["label"]))
        self.fav.add_css_class("flat")
        extra = self.fav
        if pl.get("smart"):
            # Kodi's Party Mode from this smart playlist: endless, run by Kodi itself
            party = Gtk.Button(icon_name="tonearm-radio-symbolic", valign=Gtk.Align.CENTER,
                               tooltip_text="Party Mode — Kodi keeps playing songs from "
                                            "this playlist, also with Tonearm closed")
            party.add_css_class("circular")
            party.connect("clicked", lambda *_: self.session.start_partymode(pl["file"],
                                                                              pl["label"]))
            extra = Gtk.Box(spacing=8)
            extra.append(self.fav)
            extra.append(party)
        head, _title, self.meta, _play, _shuffle = playlist_header(
            pl["label"], lambda: self.session.play_playlist(pl["file"]),
            lambda: self.session.play_playlist(pl["file"], shuffle=True), extra=extra)
        follow(self.session, "favourites-changed", self, lambda page: page._sync_fav())
        self._sync_fav()
        self.tracks = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.tracks.add_css_class("boxed-list")
        self.tracks.connect("row-activated",
                            lambda lb, row: self.session.play_playlist(pl["file"], row.position))
        self.spinner = Spinner(spinning=True, size=32, halign=Gtk.Align.CENTER)
        for w in (head, self.spinner, self.tracks):
            box.append(w)
        tv.set_content(scrolled(Adw.Clamp(maximum_size=760, child=box)))
        self.set_child(tv)
        self.refresh()

    def _sync_fav(self):
        self.fav.show_state(self.pl["file"] in self.session.fav_playlist_paths())

    def refresh(self):
        self.dirty = False

        def done(items):
            self.spinner.stop()
            if items is None:
                self.meta.set_label("Kodi is offline — tracks can't be listed right now")
                return
            total = 0
            for pos, it in enumerate(items):
                artist = ", ".join(it.get("artist") or []) if isinstance(it.get("artist"), list) \
                    else (it.get("artist") or "")
                sub = " · ".join(x for x in (artist, it.get("album") or "") if x)
                row = plain_row(title=it.get("title") or it.get("label") or "", subtitle=sub,
                                    use_markup=False, activatable=True)
                num = Gtk.Label(label=str(pos + 1), width_chars=3, xalign=1)
                num.add_css_class("dim-label")
                num.add_css_class("numeric")
                row.add_prefix(num)
                dur = Gtk.Label(label=fmt_time(it.get("duration")))
                dur.add_css_class("dim-label")
                dur.add_css_class("numeric")
                row.add_suffix(dur)
                row.position = pos
                self.tracks.append(row)
                total += int(it.get("duration") or 0)
            n = len(items)
            self.meta.set_label(plural(n, 'song')
                                + (f" · {total // 3600} h {total % 3600 // 60} min" if total >= 3600
                                   else f" · {total // 60} min" if total else ""))

        self.session.fetch_playlist_items(self.pl["file"], done)
