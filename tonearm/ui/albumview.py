"""One album browser used everywhere albums are listed: grid or list, three tile sizes,
covers with or without titles — set once from any view button, applied to all."""
from __future__ import annotations

import weakref

from gi.repository import Adw, Gio, GLib, Gtk

from ..listlayout import Spec
from .listview import TableView
from .widgets import (AlbumObj, AlbumTile, Cover, JumpView, TILE_SIZES, album_menu,
                      attach_context_menu, scrolled, set_menu, stars_text)

DEFAULTS = {"album_view": "grid", "album_size": "medium", "album_titles": True}

_views: "weakref.WeakSet" = weakref.WeakSet()


def prefs(window) -> dict:
    p = window.config.prefs
    return {k: p.get(k, v) for k, v in DEFAULTS.items()}


def apply_everywhere(window):
    """The options changed: restyle every album view that exists."""
    for v in list(_views):
        v.apply()
    for page in list(_others):
        page.apply_album_prefs()


_others: "weakref.WeakSet" = weakref.WeakSet()


def register_other(page):
    """Pages with their own album widgets (artist page) that follow the same options."""
    _others.add(page)


def _added(a) -> str:
    return (a.dateadded or "")[:10] or "–"




def album_specs() -> list[Spec]:
    """Album list columns: cover, title, artist, year, genre, date added, ⋯.
    Flexible columns share the room; the rest drop out as the list narrows."""
    return [
        Spec("cover", 40),
        Spec("title", title="Title"),
        Spec("artist", need=420, title="Artist",
             value=lambda a: a.artist or "–", meta=lambda a: a.artist),
        Spec("year", 60, need=330, title="Year", value=lambda a: str(a.year) if a.year else "",
             xalign=1.0, numeric=True),
        Spec("genre", need=560, title="Genre",
             value=lambda a: a.genre or "", meta=lambda a: "", sortable=False),
        Spec("recent", 96, need=700, title="Added", value=_added, meta=lambda a: "",
             xalign=1.0, numeric=True),
        Spec("rating", 84, need=820, title="Rating", value=lambda a: stars_text(a.rating),
             meta=lambda a: ""),
        Spec("more", 34),
    ]


def _more_button() -> Gtk.MenuButton:
    b = Gtk.MenuButton(icon_name="tonearm-more-symbolic", valign=Gtk.Align.CENTER)
    b.add_css_class("flat")
    return b


class AlbumView(Gtk.Stack):
    """Grid / list of albums over one store, with the A–Z rail and back-to-top."""

    def __init__(self, window, on_activate=None, sortable: bool = False):
        super().__init__(transition_type=Gtk.StackTransitionType.CROSSFADE, vexpand=True)
        self.sortable = sortable
        self.window = window
        self.session = window.session
        self.store = Gio.ListStore(item_type=AlbumObj)
        on_activate = on_activate or window.show_album
        activate = lambda v, pos: on_activate(self.store.get_item(pos).album)  # noqa: E731

        self.grid = Gtk.GridView(model=Gtk.NoSelection(model=self.store),
                                 single_click_activate=True, min_columns=1, max_columns=14)
        self.grid.add_css_class("album-grid")
        self.grid.remove_css_class("view")   # on the page, not in the darker view colour
        self.grid.connect("activate", activate)
        session = self.session

        def bind_more(button, obj):
            # built when opened: Favorites and playlists in it are always current
            button.set_create_popup_func(
                lambda b, aid=obj.album.id: set_menu(b, album_menu(aid)))

        # list: a Gtk.ColumnView, as in Nautilus (GTK's own sortable, resizable headers)
        self.table = TableView(
            album_specs(), self.store, prefix_key="cover", title_key="title", suffix_key="more",
            get=lambda o: o.album, title=lambda a: a.title, extra=lambda o: o.subtitle,
            prefix=(lambda: Cover(40),
                    lambda w, o: w.set_art(session, o.album.thumbnail, "small")),
            suffix=(_more_button, bind_more),
            on_sort=self._sort_clicked if sortable else None,
            on_activate=lambda o: on_activate(o.album),
            menu=lambda o: album_menu(o.album.id))
        self.list = self.table.view
        self.list.add_css_class("album-list")

        self.grid_jump = JumpView(scrolled(self.grid), self.grid)
        self.list_jump = JumpView(scrolled(self.list), self.list)
        self.add_named(self.grid_jump, "grid")
        self.add_named(self.table.wrap(self.list_jump), "list")
        self._applied = None
        _views.add(self)
        self.apply()

    def apply(self):
        p = prefs(self.window)
        key = (p["album_size"], p["album_titles"])
        if key != self._applied:  # new tile size or labels: rebuild the grid's tiles
            self._applied = key
            size, labels = TILE_SIZES.get(p["album_size"], TILE_SIZES["medium"]), p["album_titles"]
            f = Gtk.SignalListItemFactory()

            def setup(_f, li):
                tile = AlbumTile(size, labels)
                li.set_child(tile)
                attach_context_menu(tile, lambda t=tile: album_menu(t.album.id, with_fav=False)
                                    if t.album else None)

            f.connect("setup", setup)
            f.connect("bind", lambda _f, li: li.get_child().bind(
                self.session, li.get_item().album, li.get_item().subtitle))
            self.grid.set_factory(f)
            if labels:
                self.grid.remove_css_class("covers-only")
            else:
                self.grid.add_css_class("covers-only")
        self.set_visible_child_name("list" if p["album_view"] == "list" else "grid")
        if self.sortable:
            prefs_ = self.window.config.prefs
            key = prefs_.get("album_sort", "title")
            self.table.set_sort(key, (key in DESCENDING) != bool(prefs_.get("album_sort_reverse")))
        else:
            self.table.set_sort(None)

    def _sort_clicked(self, key, descending):
        """Column header click (GTK flips the arrow): sort by that column, that way."""
        win = self.window
        reverse = descending != (key in DESCENDING)
        if win.config.prefs.get("album_sort", "title") != key:
            # a new column starts in its natural direction (newest / A→Z first)
            win.lookup_action("album-sort").change_state(GLib.Variant("s", key))
        elif bool(win.config.prefs.get("album_sort_reverse")) != reverse:
            win.lookup_action("album-sort-reverse").change_state(GLib.Variant("b", reverse))

    def set_albums(self, objs: list):
        self.store.splice(0, self.store.get_n_items(), objs)

    def set_index(self, keys):
        self.grid_jump.set_index(keys)
        self.list_jump.set_index(keys)


SORTS = [("recent", "Date Added"), ("artist", "Artist"), ("title", "Title"), ("year", "Year"),
         ("rating", "Rating")]
DESCENDING = {"year", "recent", "rating"}   # natural order: newest / best first
SIZES = [("small", "Small Covers"), ("medium", "Medium Covers"), ("large", "Large Covers")]
# View buttons are plain Adw.SplitButtons: PyGObject drops their Python wrappers (and
# with them any weak reference) while GTK keeps the widget, so a WeakSet of them goes
# empty. Each button instead follows the album-view action itself (see below).


def install_actions(window):
    """Window actions behind the view button's menu. Stateful actions make the popover
    menu draw radio marks (view, size, sort, direction) and a checkbox (titles)."""
    p = window.config.prefs

    def stateful(name, value, on_set):
        ptype = None if isinstance(value, bool) else GLib.VariantType("s")
        act = Gio.SimpleAction.new_stateful(
            name, ptype, GLib.Variant("b" if isinstance(value, bool) else "s", value))

        def change(a, v):
            a.set_state(v)
            on_set(v.unpack())

        act.connect("change-state", change)
        window.add_action(act)
        return act

    def refresh(refresh_albums=False):
        window.config.save()
        _sync_enabled(window)
        _sync_direction(window)
        if refresh_albums:
            window.roots["albums"].refresh()
        apply_everywhere(window)

    def set_pref(key, refresh_albums=False):
        def apply(value):
            p[key] = value
            if key == "album_sort":
                p["album_sort_reverse"] = False   # a new order starts in its natural direction
                rev = window.lookup_action("album-sort-reverse")
                if rev is not None:
                    rev.set_state(GLib.Variant("b", False))
            refresh(refresh_albums)
        return apply

    def set_direction(value):
        key = p.get("album_sort", "title")
        p["album_sort_reverse"] = (value == "desc") != (key in DESCENDING)
        window.lookup_action("album-sort-reverse").set_state(
            GLib.Variant("b", p["album_sort_reverse"]))
        refresh(True)

    stateful("album-view", prefs(window)["album_view"], set_pref("album_view"))
    stateful("album-size", prefs(window)["album_size"], set_pref("album_size"))
    stateful("album-titles", bool(prefs(window)["album_titles"]), set_pref("album_titles"))
    stateful("album-sort", p.get("album_sort", "title"), set_pref("album_sort", True))
    stateful("album-sort-reverse", bool(p.get("album_sort_reverse", False)),
             set_pref("album_sort_reverse", True))
    stateful("album-sort-direction", _direction(p), set_direction)
    _sync_enabled(window)


def _direction(p) -> str:
    key = p.get("album_sort", "title")
    return "desc" if (key in DESCENDING) != bool(p.get("album_sort_reverse")) else "asc"


def _sync_direction(window):
    act = window.lookup_action("album-sort-direction")
    if act is not None:
        act.set_state(GLib.Variant("s", _direction(window.config.prefs)))


def _sync_enabled(window):
    """Cover size and titles only mean something in the grid."""
    grid = prefs(window)["album_view"] != "list"
    for name in ("album-size", "album-titles"):
        act = window.lookup_action(name)
        if act is not None:
            act.set_enabled(grid)


def _update_button(window, button, view: str | None = None):
    """``view``: the new view when known (the action's state changes before the
    setting is saved — reading the setting then gives the old view)."""
    grid = (view or prefs(window)["album_view"]) != "list"
    # the icon shows the view you can switch to (grid shown → list icon)
    button.set_icon_name("tonearm-list-symbolic" if grid else "tonearm-grid-symbolic")
    button.set_tooltip_text("Show as List" if grid else "Show as Grid")


def view_menu(sortable: bool) -> Gio.Menu:
    """The view button's menu (grid / list is the button itself, as in Nautilus): cover
    sizes and titles, then — where it applies — Sort and its direction; each group a
    section, the current choice marked."""
    menu = Gio.Menu()

    def radio(section, label, action, value):
        it = Gio.MenuItem.new(label, None)
        it.set_action_and_target_value(action, GLib.Variant("s", value))
        section.append_item(it)

    size = Gio.Menu()
    for key, label in SIZES:
        radio(size, label, "win.album-size", key)
    size.append("Show Titles", "win.album-titles")
    menu.append_section(None, size)
    if sortable:
        sort = Gio.Menu()
        for key, label in SORTS:
            radio(sort, label, "win.album-sort", key)
        menu.append_section("Sort", sort)
        direction = Gio.Menu()
        radio(direction, "Ascending", "win.album-sort-direction", "asc")
        radio(direction, "Descending", "win.album-sort-direction", "desc")
        menu.append_section(None, direction)
    return menu


def view_options_button(window, sortable: bool = False) -> Adw.SplitButton:
    """Like Nautilus: the button switches between grid and list at once (its icon is
    the view you'd switch to); the small arrow beside it opens the menu with cover
    size, titles and — where it applies — the order and its direction."""
    # grid / list is the button itself, so the menu leaves it out
    button = Adw.SplitButton(menu_model=view_menu(sortable),
                             dropdown_tooltip="View and Sort" if sortable else "View Options")

    def toggle(_b):
        grid = prefs(window)["album_view"] != "list"
        window.lookup_action("album-view").change_state(
            GLib.Variant("s", "list" if grid else "grid"))

    button.connect("clicked", toggle)
    # follow the view setting (changed here or on any other page); stop once the
    # button's page is gone
    action = window.lookup_action("album-view")
    if action is not None:
        seen = [False]
        hid = [0]

        def on_state(act, _pspec):
            view = act.get_state().unpack()      # the new view (the setting lags behind)
            if button.get_root() is not None:
                seen[0] = True
                _update_button(window, button, view)
            elif seen[0]:
                action.disconnect(hid[0])
            else:
                _update_button(window, button, view)   # not on screen yet: update anyway
        hid[0] = action.connect("notify::state", on_state)
    _update_button(window, button)
    return button
