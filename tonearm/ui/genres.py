"""Genres: the page that lists them and a genre's own page.

The list page features your genres (most played, else biggest) as wide coloured tiles,
like the Decades page, then every genre as a compact pill — A→Z under letter headings,
or biggest first — with Sort and "hide one-album genres" in the top bar.

A genre's page has a coloured header (Play, Shuffle, Radio), its top artists, decade
chips that narrow the albums, the genres it's often paired with, and the albums.
"""
from __future__ import annotations

from gi.repository import Adw, Gio, GLib, Gtk, Pango

from .. import genres as G
from ..util import plural, run_async
from .albumview import AlbumView, view_options_button
from .collections import ColorTile, decade_label
from .listview import SizeWatcher
from .widgets import AlbumObj, load_avatar, pill, scrolled, status_page

TOP = 8          # featured genre tiles (4 on a phone)
PILLS = 30       # genres in "All Genres" before Show More …
PILLS_PHONE = 16  # … on a phone
PHONE = 600      # narrower than this: phone layout



def _listening_songs(cache) -> list:
    """Kodi's most played songs, as last fetched for the Spotlight page (no new call)."""
    try:
        return (cache.get_meta_json("listening", {}) or {}).get("top") or []
    except Exception:  # noqa: BLE001 — a cache without play data is fine
        return []


def genrepill(name: str, count: int | None = None) -> Gtk.Button:
    box = Gtk.Box(spacing=6)
    box.append(Gtk.Label(label=name, ellipsize=Pango.EllipsizeMode.END, max_width_chars=28))
    if count is not None:
        n = Gtk.Label(label=str(count))
        n.add_css_class("genre-pill-count")
        box.append(n)
    b = Gtk.Button(child=box)
    b.add_css_class("genre-pill")
    return b


def _flow(**kw) -> Gtk.FlowBox:
    return Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, valign=Gtk.Align.START,
                       column_spacing=8, row_spacing=8, **kw)


def _wrap() -> Gtk.Widget:
    """Pills that wrap like words: Adw.WrapBox (libadwaita ≥ 1.7); older: a FlowBox
    (lines its pills up in columns, which looks a bit more like a table)."""
    if hasattr(Adw, "WrapBox"):
        box = Adw.WrapBox(child_spacing=8, line_spacing=8)
        box.add_css_class("genre-pills")
        return box
    flow = _flow(min_children_per_line=1, max_children_per_line=40)
    flow.add_css_class("genre-pills")
    return flow


def _section_title(text: str, sub: str = "") -> Gtk.Widget:
    head = Gtk.Box(spacing=10, margin_top=6)
    t = Gtk.Label(label=text, xalign=0)
    t.add_css_class("title-4")
    head.append(t)
    if sub:
        s = Gtk.Label(label=sub, xalign=0, valign=Gtk.Align.CENTER)
        s.add_css_class("dim-label")
        s.add_css_class("caption")
        head.append(s)
    return head


# ---------------------------------------------------------------------------
# All genres
# ---------------------------------------------------------------------------
class GenresPage(Adw.NavigationPage):
    def __init__(self, window):
        super().__init__(title="Genres", tag="genres")
        self.window = window
        self.session = window.session
        self.dirty = True
        self._stats: list[dict] | None = None    # None: not loaded yet
        self._phone = False
        self._expanded = False            # All Genres: everything, or the biggest few
        prefs = window.config.prefs
        self.sort = prefs.get("genre_sort", "name")
        self.hide_small = bool(prefs.get("genre_hide_small", False))

        tv = Adw.ToolbarView()
        header = window.root_header()
        self._install_actions()
        sort_btn = Gtk.MenuButton(icon_name="tonearm-sort-symbolic", tooltip_text="Sort and Filter",
                                  menu_model=self._menu())
        header.pack_end(sort_btn)
        tv.add_top_bar(header)

        self.content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                               margin_top=12, margin_bottom=24, margin_start=12, margin_end=12)
        clamp = Adw.Clamp(maximum_size=1400, child=self.content)
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.stack.add_named(SizeWatcher(scrolled(clamp), self._on_width), "list")
        self.stack.add_named(status_page("tonearm-genre-symbolic", "No Genres",
                                         "Your albums have no genre tags yet."), "empty")
        tv.set_content(self.stack)
        self.set_child(tv)
        self.connect("showing", lambda *_: self.dirty and self.refresh())

    # -- sort / filter -----------------------------------------------------
    def _install_actions(self):
        group = Gio.SimpleActionGroup()
        sort = Gio.SimpleAction.new_stateful("sort", GLib.VariantType("s"),
                                             GLib.Variant("s", self.sort))
        sort.connect("change-state", lambda a, v: (a.set_state(v), self._set(sort=v.unpack())))
        hide = Gio.SimpleAction.new_stateful("hide-small", None, GLib.Variant("b", self.hide_small))
        hide.connect("change-state", lambda a, v: (a.set_state(v), self._set(hide=v.unpack())))
        group.add_action(sort)
        group.add_action(hide)
        self.insert_action_group("genres", group)

    @staticmethod
    def _menu() -> Gio.Menu:
        menu = Gio.Menu()
        sort = Gio.Menu()
        for key, label in (("name", "A–Z"), ("count", "Most Albums")):
            it = Gio.MenuItem.new(label, None)
            it.set_action_and_target_value("genres.sort", GLib.Variant("s", key))
            sort.append_item(it)
        menu.append_section("Sort", sort)
        filt = Gio.Menu()
        filt.append("Hide Genres With One Album", "genres.hide-small")
        menu.append_section(None, filt)
        return menu

    def _set(self, sort=None, hide=None):
        if sort is not None:
            self.sort = sort
        if hide is not None:
            self.hide_small = bool(hide)
        p = self.window.config.prefs
        p["genre_sort"], p["genre_hide_small"] = self.sort, self.hide_small
        self.window.config.save()
        self._build()

    # -- data --------------------------------------------------------------
    def refresh(self):
        self.dirty = False
        cache = self.session.cache
        if cache is None:
            return

        def done(stats):
            self._stats = stats
            self._build()

        run_async(lambda: G.overview(cache.genre_rows(), _listening_songs(cache)), done=done)

    def _on_width(self, width: int):
        phone = width < PHONE
        if phone != self._phone:
            self._phone = phone
            self._build()

    def _build(self):
        while (child := self.content.get_first_child()) is not None:
            self.content.remove(child)
        stats = self._stats
        if stats is None:          # still loading: no "No Genres" flash
            return
        self.stack.set_visible_child_name("list" if stats else "empty")
        if not stats:
            return
        # featured: most played (when Kodi counts plays), else the biggest
        top = G.top(stats, 4 if self._phone else TOP)
        played = any(s["plays"] for s in top)
        self.content.append(_section_title("Your Genres",
                                           "most played" if played else "the biggest in your library"))
        tiles = _flow(homogeneous=True, min_children_per_line=1, max_children_per_line=4)
        tiles.set_column_spacing(12)
        tiles.set_row_spacing(12)
        tiles.add_css_class("decade-flow")
        tiles.set_activate_on_single_click(True)
        tiles.connect("child-activated", lambda fb, ch: self._open(ch.get_child().data))
        colors = G.spread_colors([s["name"] for s in top])
        for s, color in zip(top, colors):
            name = s["name"]
            tile = ColorTile(self.session, name, plural(s["count"], "album"), s["covers"], color,
                             on_radio=lambda n=name: self.session.start_radio(None, n),
                             radio_tooltip=f"Start a {name} radio on Kodi", data=name)
            tiles.append(tile)
        self.content.append(tiles)

        # everything, as pills — first only the biggest few (in the chosen order),
        # Show More for the rest
        rest = G.ordered(stats, self.sort, self.hide_small)
        hidden = len(stats) - len(rest)
        sub = plural(len(rest), "genre") + (f" · {hidden} with one album hidden" if hidden else "")
        head = _section_title("All Genres", sub)
        self.content.append(head)
        limit = PILLS_PHONE if self._phone else PILLS
        if len(rest) > limit:
            spacer = Gtk.Box(hexpand=True)
            head.append(spacer)
            more = Gtk.Button(label="Show Less" if self._expanded
                              else f"Show More ({len(rest) - limit})", valign=Gtk.Align.CENTER)
            more.add_css_class("flat")
            more.connect("clicked", self._toggle_more)
            head.append(more)
        if self._expanded or len(rest) <= limit:
            shown = rest
        else:
            keep = {id(s) for s in sorted(rest, key=lambda s: (-s["count"], s["name"].lower()))[:limit]}
            shown = [s for s in rest if id(s) in keep]
        # letter headings only for the whole A–Z list (a short list reads better without)
        letters = self.sort == "name" and (self._expanded or len(rest) <= limit) and len(shown) > 40
        groups = G.by_letter(shown) if letters else [("", shown)]
        for letter, items in groups:
            if letter:
                lbl = Gtk.Label(label=letter, xalign=0, margin_top=4)
                lbl.add_css_class("genre-letter")
                self.content.append(lbl)
            flow = _wrap()
            for s in items:
                pill = genrepill(s["name"], s["count"])
                pill.connect("clicked", lambda _b, n=s["name"]: self._open(n))
                flow.append(pill)
            self.content.append(flow)

    def _toggle_more(self, *_):
        self._expanded = not self._expanded
        self._build()

    def _open(self, genre: str):
        self.window.push_page(GenrePage(self.window, genre))


# ---------------------------------------------------------------------------
# One genre
# ---------------------------------------------------------------------------
class GenrePage(Adw.NavigationPage):
    """A genre: coloured header with Play / Shuffle / Radio, top artists, decade chips
    (narrow the albums), related genres and the albums. The upper part folds away as
    the albums scroll, so the grid gets the room."""

    def __init__(self, window, genre: str):
        super().__init__(title=genre, tag=f"genre-{genre}")
        self.window = window
        self.session = window.session
        self.genre = genre
        self.dirty = False
        self.all_albums: list = []
        self.albums: list = []
        self.decade: int | None = None

        tv = Adw.ToolbarView()
        header = window.search_header()
        header.pack_end(view_options_button(window))
        tv.add_top_bar(header)

        # coloured band: name, count, Play / Shuffle / Radio
        band = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        band.add_css_class("genre-hero")
        band.add_css_class(f"c{G.color_index(genre)}")
        title = Gtk.Label(label=genre, xalign=0, wrap=True)
        title.add_css_class("genre-hero-title")
        self.count = Gtk.Label(xalign=0)
        self.count.add_css_class("genre-hero-sub")
        buttons = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, column_spacing=8,
                              row_spacing=8, min_children_per_line=1, max_children_per_line=3,
                              homogeneous=False)
        play = pill("Play", "tonearm-play-symbolic")
        play.connect("clicked", lambda *_: self.session.play_albums([a.id for a in self.albums]))
        shuffle = pill("Shuffle", "tonearm-shuffle-symbolic")
        shuffle.connect("clicked", lambda *_: self.session.play_albums(
            [a.id for a in self.albums], True))
        radio = pill("Radio", "tonearm-radio-symbolic")
        radio.set_tooltip_text("An endless mix from this genre (and the chosen decade)")
        radio.connect("clicked", lambda *_: self.session.start_radio(self.decade, self.genre))
        for b in (play, shuffle, radio):
            b.add_css_class("on-color")
            buttons.append(b)
        for w in (title, self.count, buttons):
            band.append(w)

        # top artists (avatars) and chips: folded away while the albums scroll
        self.artists = Gtk.Box(spacing=14)
        self.artists_row = Gtk.ScrolledWindow(child=self.artists, vscrollbar_policy=Gtk.PolicyType.NEVER,
                                              hscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
                                              visible=False)
        self.artists_row.add_css_class("genre-artists")
        self.decades = Gtk.Box(spacing=6)
        self.related = Gtk.Box(spacing=6)
        chips = Gtk.Box(spacing=6)
        chips.append(self.decades)
        chips.append(self.related)
        self.chips_row = chips_row = Gtk.ScrolledWindow(child=chips, vscrollbar_policy=Gtk.PolicyType.NEVER,
                                       hscrollbar_policy=Gtk.PolicyType.AUTOMATIC)
        chips_row.add_css_class("genre-chips")
        extra = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.artists_head = Gtk.Label(label="Top Artists", xalign=0, visible=False)
        self.artists_head.add_css_class("heading")
        extra.append(self.artists_head)
        extra.append(self.artists_row)
        extra.append(chips_row)
        self.fold = Gtk.Revealer(child=extra, reveal_child=True, transition_duration=200,
                                 transition_type=Gtk.RevealerTransitionType.SLIDE_DOWN)
        self._settling = False

        top = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin_top=8,
                      margin_start=12, margin_end=12, margin_bottom=4)
        top.append(band)
        top.append(self.fold)
        tv.add_top_bar(top)

        self.view = AlbumView(window)
        for jump in (self.view.grid_jump, self.view.list_jump):
            jump.scrolled.get_vadjustment().connect("value-changed", self._on_scroll)
        tv.set_content(self.view)
        self.set_child(tv)
        self.refresh()

    def _on_scroll(self, adj):
        """Fold the artists / chips away once the albums have scrolled a bit; unfold
        only back at the very top. Folding resizes the album view, which moves the
        scroll position itself — so: a gap between the two thresholds, no reacting
        while the fold animates, and no folding when the albums would then fit
        without scrolling (that would bounce open and shut)."""
        if self._settling:
            return
        value = adj.get_value()
        if self.fold.get_reveal_child():
            room = adj.get_upper() - adj.get_page_size()
            if value > 64 and room > self.fold.get_height() + 120:
                self._toggle_fold(False)
        elif value <= 0:
            self._toggle_fold(True)

    def _toggle_fold(self, reveal: bool):
        self._settling = True
        self.fold.set_reveal_child(reveal)

        def settled():
            self._settling = False
            return GLib.SOURCE_REMOVE

        GLib.timeout_add(self.fold.get_transition_duration() + 60, settled)

    # -- data --------------------------------------------------------------
    def refresh(self):
        self.dirty = False
        cache = self.session.cache
        if cache is None:
            return
        genre = self.genre

        def work():
            albums = cache.albums_for_genre(genre)
            return {"albums": albums,
                    "artists": cache.top_artists_for_albums([a.id for a in albums], 12),
                    "decades": G.decades([a.year for a in albums]),
                    "related": G.related([a.genre for a in albums], genre, 6)}

        run_async(work, done=self._show)

    def _show(self, data):
        self.all_albums = data["albums"]
        self._fill_artists(data["artists"])
        self._fill_chips(data["decades"], data["related"])
        self._apply_decade()

    def _fill_artists(self, artists):
        while (c := self.artists.get_first_child()) is not None:
            self.artists.remove(c)
        for aid, name, thumb, n in artists:
            self.artists.append(_ArtistChip(self.window, aid, name, thumb, n))
        show = len(artists) > 1          # one artist: nothing to pick between
        self.artists_row.set_visible(show)
        self.artists_head.set_visible(show)

    def _fill_chips(self, decades, related):
        for box in (self.decades, self.related):
            while (c := box.get_first_child()) is not None:
                box.remove(c)
        self._decade_buttons = {}
        if len(decades) > 1:
            first = None
            for d, n in [(None, len(self.all_albums))] + decades:
                b = Gtk.ToggleButton(label="All" if d is None else decade_label(d),
                                     group=first, active=d is None)
                b.set_tooltip_text(plural(n, "album"))
                b.add_css_class("genre-chip")
                first = first or b
                b.connect("toggled", lambda btn, d=d: btn.get_active() and self._set_decade(d))
                self.decades.append(b)
        if related:
            if len(decades) > 1:
                sep = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL, margin_start=6,
                                    margin_end=6)
                self.related.append(sep)
            lbl = Gtk.Label(label="Often with")
            lbl.add_css_class("dim-label")
            lbl.add_css_class("caption")
            self.related.append(lbl)
            for name, n in related:
                b = genrepill(name)
                b.set_tooltip_text(f"{plural(n, 'album')} tagged both")
                b.connect("clicked", lambda _b, g=name: self.window.push_page(
                    GenrePage(self.window, g)))
                self.related.append(b)
        self.chips_row.set_visible(len(decades) > 1 or bool(related))

    def _set_decade(self, decade):
        self.decade = decade
        self._apply_decade()

    def _apply_decade(self):
        d = self.decade
        self.albums = [a for a in self.all_albums
                       if d is None or (a.year and (a.year // 10) * 10 == d)]
        self.view.set_albums([AlbumObj(a, " · ".join(x for x in (
            a.artist, str(a.year) if a.year else "") if x)) for a in self.albums])
        n = len(self.albums)
        artists = len({(a.artist or "").lower() for a in self.albums})
        self.count.set_label(f"{plural(n, 'album')} · {plural(artists, 'artist')}"
                             + (f" · {decade_label(d)}" if d is not None else ""))


class _ArtistChip(Gtk.Button):
    """Avatar + name; opens the artist."""

    __gtype_name__ = "TonearmGenreArtist"

    def __init__(self, window, aid, name, thumb, n):
        super().__init__(tooltip_text=f"{name} · {plural(n, 'album')}")
        self.add_css_class("flat")
        self.add_css_class("genre-artist")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, width_request=76)
        self.avatar = Adw.Avatar(size=56, text=name, show_initials=True,
                                 halign=Gtk.Align.CENTER)
        label = Gtk.Label(label=name, ellipsize=Pango.EllipsizeMode.END, max_width_chars=1,
                          hexpand=True)
        label.add_css_class("caption")
        box.append(self.avatar)
        box.append(label)
        self.set_child(box)
        self.connect("clicked", lambda *_: window.show_artist(aid))
        load_avatar(self, self.avatar, window.session, name, thumb, "small")


__all__ = ["GenresPage", "GenrePage"]
