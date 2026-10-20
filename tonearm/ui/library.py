"""Root pages: Recently Added, Albums, Artists."""
from __future__ import annotations

from gi.repository import Adw, Gio, GLib, Gtk, Pango

from ..util import fmt_total, plural, run_async
from .albumview import AlbumView, view_options_button
from ..listlayout import Spec
from .listview import TableView
from .widgets import AlbumObj, ArtistObj, JumpView, initial, load_avatar, scrolled, status_page

class AlbumGridPage(Adw.NavigationPage):
    """Cover grid. ``mode`` is "albums" (all, sortable) or "recent"."""

    def __init__(self, window, mode: str = "albums"):
        title = "Recently Added" if mode == "recent" else "Albums"
        # the tag is the section's name (the window pops back to it by that)
        super().__init__(title=title, tag="recent-added" if mode == "recent" else mode)
        self.window = window
        self.session = window.session
        self.mode = mode
        self.dirty = True

        tv = Adw.ToolbarView()
        header = window.root_header()
        header.pack_end(view_options_button(window, sortable=mode == "albums"))
        tv.add_top_bar(header)

        self.view = AlbumView(window, sortable=mode == "albums")
        self.store = self.view.store
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.jump = self.view
        self.stack.add_named(self.view, "grid")
        self.stack.add_named(status_page(
            "tonearm-album-symbolic", "No Albums",
            "Your library is empty, or it is still being synced from Kodi."), "empty")
        tv.set_content(self.stack)
        self.set_child(tv)
        self.connect("showing", lambda *_: self.dirty and self.refresh())

    def refresh(self):
        self.dirty = False
        cache = self.session.cache
        if cache is None:
            self.store.remove_all()
            self.stack.set_visible_child_name("empty")
            return
        if self.mode == "recent":
            fetch = lambda: cache.albums("recent", limit=200)  # noqa: E731
        else:
            order = self.window.config.prefs.get("album_sort", "title")
            fetch = lambda: cache.albums(order)  # noqa: E731

        sort = None if self.mode == "recent" else self.window.config.prefs.get("album_sort", "title")
        reverse = sort is not None and self.window.config.prefs.get("album_sort_reverse", False)

        def done(albums):
            if reverse:
                albums = albums[::-1]
            if self.mode == "recent":     # "Artist · 3 days ago"
                from .recent import ago   # noqa: PLC0415  (recent imports much of the UI)
                self.view.set_albums([AlbumObj(a, " · ".join(x for x in (a.artist, ago(a))
                                                             if x)) for a in albums])
            else:
                self.view.set_albums([AlbumObj(a) for a in albums])
            # A–Z rail whenever the order is alphabetical (Z→A too: a letter jumps to
            # its first album either way)
            if sort == "title":
                self.jump.set_index([initial(a.title) for a in albums])
            elif sort == "artist":
                self.jump.set_index([initial(a.artist) for a in albums])
            else:
                self.jump.set_index(None)
            self.stack.set_visible_child_name("grid" if albums else "empty")

        run_async(fetch, done=done)



def artist_specs() -> list[Spec]:
    """Artist list columns: avatar, name, genre, albums, years, length, songs.
    Flexible columns share the room; the rest drop out as the list narrows."""
    return [
        Spec("avatar", 40),
        Spec("name", title="Name"),
        Spec("genre", need=480, title="Genre",
             value=lambda a: a.genre or "", meta=lambda a: a.genre),
        Spec("albums", 76, need=330, title="Albums", value=lambda a: str(a.album_count),
             meta=lambda a: plural(a.album_count, 'album'),
             xalign=1.0, numeric=True),
        Spec("years", 96, need=600, title="Years", value=lambda a: a.years or "–",
             meta=lambda a: a.years, xalign=1.0, numeric=True),
        Spec("duration", 80, need=700, title="Length",
             value=lambda a: fmt_total(a.duration) if a.duration else "–",
             meta=lambda a: "", xalign=1.0, numeric=True),
        Spec("songs", 64, need=800, title="Songs",
             value=lambda a: str(a.song_count) if a.song_count else "–",
             meta=lambda a: "", xalign=1.0, numeric=True),
    ]


ARTIST_SORTS = [("name", "Name"), ("genre", "Genre"), ("albums", "Albums"), ("songs", "Songs"),
                ("years", "Years"), ("duration", "Length")]
ARTIST_DESCENDING = {"albums", "songs", "duration"}  # numbers read best biggest-first

ARTIST_SORT = {
    "name": None,  # cache order (sort name)
    "genre": lambda a: (a.genre or "\uffff").lower(),
    "albums": lambda a: a.album_count,
    "songs": lambda a: a.song_count,
    "years": lambda a: a.year_from or 99999,
    "duration": lambda a: a.duration,
}


class ArtistAvatar(Adw.Bin):
    """40 px avatar for the list's first column; loads the artist's thumbnail.
    (Adw.Avatar itself is final, hence the Bin around it.)"""

    __gtype_name__ = "TonearmArtistAvatar"

    def __init__(self):
        super().__init__(valign=Gtk.Align.CENTER)
        self.avatar = Adw.Avatar(size=40, show_initials=True)
        self.set_child(self.avatar)

    def bind(self, session, artist):
        load_avatar(self, self.avatar, session, artist.name, artist.thumbnail, "small")


class ArtistTile(Gtk.Box):
    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6, width_request=140,
                         halign=Gtk.Align.CENTER)   # name never wider than the picture
        self.add_css_class("artist-tile")
        self.avatar = Adw.Avatar(size=128, show_initials=True, halign=Gtk.Align.CENTER)
        self.name = Gtk.Label(ellipsize=Pango.EllipsizeMode.END, max_width_chars=1, hexpand=True)
        self.name.add_css_class("tile-title")
        self.meta = Gtk.Label(ellipsize=Pango.EllipsizeMode.END, max_width_chars=1, hexpand=True)
        self.meta.add_css_class("dim-label")
        self.meta.add_css_class("caption")
        for w in (self.avatar, self.name, self.meta):
            self.append(w)

    def bind(self, session, artist):
        self.name.set_label(artist.name)
        n = artist.album_count
        self.meta.set_label(plural(n, 'album'))
        load_avatar(self, self.avatar, session, artist.name, artist.thumbnail, "large")


class ArtistListPage(Adw.NavigationPage):
    """Artists as a full-width table (columns drop out as room runs short) or an avatar
    grid. Same view button as the album pages: click = grid/list, arrow = sort menu."""

    def __init__(self, window):
        super().__init__(title="Artists", tag="artists")
        self.window = window
        self.session = window.session
        self.dirty = True
        self._artists = []
        prefs = window.config.prefs
        self.view = prefs.get("artist_view", "list")
        self.sort_key = prefs.get("artist_sort", "name")
        self.sort_reverse = bool(prefs.get("artist_sort_reverse", False))

        self._install_actions()
        tv = Adw.ToolbarView()
        header = window.root_header()
        # like Nautilus: click switches grid/list, the arrow opens the sort options
        self.view_btn = Adw.SplitButton(menu_model=self._menu(), dropdown_tooltip="View and Sort")
        self.view_btn.connect("clicked", lambda *_: self._view_action.change_state(
            GLib.Variant("s", "list" if self.view == "grid" else "grid")))
        header.pack_end(self.view_btn)
        tv.add_top_bar(header)

        self.store = Gio.ListStore(item_type=ArtistObj)
        activate = lambda v, pos: window.show_artist(self.store.get_item(pos).artist.id)  # noqa: E731

        # list
        # list: a Gtk.ColumnView, as in Nautilus (GTK's own sortable, resizable headers)
        self.table = TableView(
            artist_specs(), self.store, prefix_key="avatar", title_key="name",
            get=lambda o: o.artist, title=lambda a: a.name,
            meta_order=["albums", "years", "genre"],   # "12 albums · 1990–2020 · Rock"
            prefix=(ArtistAvatar, lambda w, o: w.bind(self.session, o.artist)),
            on_sort=self._sort_clicked,
            on_activate=lambda o: window.show_artist(o.artist.id))
        self.list = self.table.view
        self.list.add_css_class("artist-list")
        list_sw = scrolled(self.list)
        self.list_jump = JumpView(list_sw, self.list)
        list_box = self.table.wrap(self.list_jump)

        # grid
        gf = Gtk.SignalListItemFactory()
        gf.connect("setup", lambda f, li: li.set_child(ArtistTile()))
        gf.connect("bind", lambda f, li: li.get_child().bind(self.session, li.get_item().artist))
        self.grid = Gtk.GridView(model=Gtk.NoSelection(model=self.store), factory=gf,
                                 single_click_activate=True, min_columns=2, max_columns=12)
        self.grid.add_css_class("album-grid")
        self.grid.remove_css_class("view")   # on the page, not in the darker view colour
        self.grid.add_css_class("artist-grid")
        self.grid.connect("activate", activate)
        grid_sw = scrolled(self.grid)

        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.stack.add_named(list_box, "list")
        self.grid_jump = JumpView(grid_sw, self.grid)
        self.stack.add_named(self.grid_jump, "grid")
        self.stack.add_named(status_page(
            "tonearm-artist-symbolic", "No Artists",
            "Your library is empty, or it is still being synced from Kodi."), "empty")
        tv.set_content(self.stack)
        self.set_child(tv)
        self.connect("showing", lambda *_: self.dirty and self.refresh())
        self._update_view_button()
        self._update_sort_labels()

    # -- view button + sort menu -----------------------------------------
    def _install_actions(self):
        group = Gio.SimpleActionGroup()

        def stateful(name, ptype, value, on_change):
            act = Gio.SimpleAction.new_stateful(name, ptype, value)
            act.connect("change-state", lambda a, v: (a.set_state(v), on_change(v.unpack())))
            group.add_action(act)
            return act

        s = GLib.VariantType("s")
        self._view_action = stateful("view", s, GLib.Variant("s", self.view), self._set_view)
        self._sort_action = stateful("sort", s, GLib.Variant("s", self.sort_key),
                                     lambda key: self._set_sort(key, False))
        self._rev_action = stateful("sort-reverse", None, GLib.Variant("b", self.sort_reverse),
                                    lambda rev: self._set_sort(self.sort_key, rev))
        self._dir_action = stateful(
            "sort-direction", s, GLib.Variant("s", "desc" if self._descending() else "asc"),
            lambda d: self._set_sort(self.sort_key,
                                     (d == "desc") != (self.sort_key in ARTIST_DESCENDING)))
        self.insert_action_group("artists", group)

    @staticmethod
    def _menu() -> Gio.Menu:
        """The view button's menu: Sort and its direction (grid / list is the button)."""
        menu = Gio.Menu()

        def section(items, action, title=None):
            sec = Gio.Menu()
            for key, label in items:
                it = Gio.MenuItem.new(label, None)
                it.set_action_and_target_value(action, GLib.Variant("s", key))
                sec.append_item(it)
            menu.append_section(title, sec)

        # (grid / list is the button itself)
        section(ARTIST_SORTS, "artists.sort", "Sort")
        section([("asc", "Ascending"), ("desc", "Descending")], "artists.sort-direction")
        return menu

    def _update_view_button(self):
        grid = self.view == "grid"
        # the icon shows the view you can switch to (grid shown → list icon)
        self.view_btn.set_icon_name("tonearm-list-symbolic" if grid else "tonearm-grid-symbolic")
        self.view_btn.set_tooltip_text("Show as List" if grid else "Show as Grid")

    def _set_view(self, view):
        self.view = view
        self.window.config.prefs["artist_view"] = self.view
        self.window.config.save()
        self._update_view_button()
        if self._artists:
            self.stack.set_visible_child_name(self.view)

    # -- sorting ---------------------------------------------------------
    def _sort_clicked(self, key, descending):
        """Column header click (GTK flips the arrow): sort by that column, that way; a
        new column starts in its natural direction."""
        if key != self.sort_key:
            self._sort_action.change_state(GLib.Variant("s", key))
        else:
            reverse = descending != (key in ARTIST_DESCENDING)
            if reverse != self.sort_reverse:
                self._rev_action.change_state(GLib.Variant("b", reverse))

    def _set_sort(self, key, reverse):
        if key != self.sort_key:
            reverse = False
            self._rev_action.set_state(GLib.Variant("b", False))
        self.sort_key, self.sort_reverse = key, bool(reverse)
        self._rev_action.set_state(GLib.Variant("b", self.sort_reverse))
        self._dir_action.set_state(GLib.Variant("s", "desc" if self._descending() else "asc"))
        prefs = self.window.config.prefs
        prefs["artist_sort"], prefs["artist_sort_reverse"] = self.sort_key, self.sort_reverse
        self.window.config.save()
        self._update_sort_labels()
        self._fill()

    def _descending(self) -> bool:
        return (self.sort_key in ARTIST_DESCENDING) != self.sort_reverse

    def _update_sort_labels(self):
        self.table.set_sort(self.sort_key, self._descending())

    def _fill(self):
        items = list(self._artists)
        fn = ARTIST_SORT.get(self.sort_key)
        desc = self._descending()
        if fn is not None:
            items.sort(key=fn, reverse=desc)
        elif desc:
            items.reverse()
        self.store.splice(0, self.store.get_n_items(), [ArtistObj(a) for a in items])
        keys = None
        if self.sort_key == "name":            # A→Z or Z→A: the rail works either way
            keys = [initial(a.sortname or a.name) for a in items]
        elif self.sort_key == "genre":
            keys = [initial(a.genre) if a.genre else "#" for a in items]
        self.list_jump.set_index(keys)
        self.grid_jump.set_index(keys)

    def refresh(self):
        self.dirty = False
        cache = self.session.cache
        if cache is None:
            self._artists = []
            self.store.remove_all()
            self.stack.set_visible_child_name("empty")
            return

        def done(artists):
            self._artists = artists
            self._fill()
            self.stack.set_visible_child_name(self.view if artists else "empty")

        run_async(cache.artists, done=done)


__all__ = ["AlbumGridPage", "ArtistListPage"]
