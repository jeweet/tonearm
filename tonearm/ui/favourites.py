"""Favorites (Kodi's favourites): a page with all of them, grouped — artists, albums,
songs, playlists, and the rest.

Albums, artists and playlists open as Tonearm pages; songs and other media play on
Kodi; Kodi screens (windows) and add-ons are opened on Kodi itself."""
from __future__ import annotations

from gi.repository import Adw, Gio, GLib, Gtk, Pango

from ..favourites import describe, grouped
from .albumview import prefs as album_prefs, register_other
from .widgets import (TILE_SIZES, Cover, attach_context_menu, follow, plain_row, scrolled,
                      status_page)

ICONS = {"album": "tonearm-album-symbolic", "artist": "tonearm-artist-symbolic",
         "song": "tonearm-song-symbolic", "playlist": "tonearm-playlist-symbolic",
         "media": "tonearm-song-symbolic", "window": "tonearm-remote-symbolic",
         "script": "tonearm-remote-symbolic", "other": "tonearm-starred-symbolic"}


def open_favourite(window, fav: dict):
    if fav.get("kind") == "album" and fav.get("albumid"):
        window.show_album_id(fav["albumid"])
    elif fav.get("kind") == "artist" and fav.get("artistid"):
        window.show_artist(fav["artistid"])
    elif fav.get("kind") == "playlist" and fav.get("playlist"):
        from .collections import PlaylistPage   # noqa: PLC0415  (collections → widgets only)
        path = fav["playlist"]
        window.push_page(PlaylistPage(window, {"file": path, "label": fav.get("title") or "Playlist",
                                               "smart": path.lower().endswith(".xsp")}))
    else:
        window.session.open_favourite(fav)


def favourite_menu(window, fav: dict) -> Gio.Menu:
    menu = Gio.Menu()
    if fav.get("kind") == "album" and fav.get("albumid"):
        for label, action in (("Play", "win.play-album"), ("Shuffle", "win.shuffle-album"),
                              ("Add to Queue", "win.queue-album")):
            it = Gio.MenuItem.new(label, None)
            it.set_action_and_target_value(action, GLib.Variant("i", fav["albumid"]))
            menu.append_item(it)
    elif fav.get("kind") == "artist" and fav.get("artistid"):
        for label, action in (("Play", "win.play-artist"), ("Shuffle", "win.shuffle-artist")):
            it = Gio.MenuItem.new(label, None)
            it.set_action_and_target_value(action, GLib.Variant("i", fav["artistid"]))
            menu.append_item(it)
    elif fav.get("kind") in ("song", "playlist", "media"):
        it = Gio.MenuItem.new("Play", None)
        it.set_action_and_target_value("win.fav-play", GLib.Variant("s", fav_key(fav)))
        menu.append_item(it)
    section = Gio.Menu()
    it = Gio.MenuItem.new("Remove from Favorites", None)
    it.set_action_and_target_value("win.fav-remove", GLib.Variant("s", fav_key(fav)))
    section.append_item(it)
    menu.append_section(None, section)
    return menu


def fav_key(fav: dict) -> str:
    return "\x1f".join((fav.get("type", ""), fav.get("path", ""), fav.get("window", ""),
                        fav.get("windowparameter", "")))


class FavouriteTile(Gtk.Box):
    """Cover (round for an artist), title and what it is."""

    def __init__(self, window, fav: dict, size: int, labels: bool = True):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.add_css_class("album-tile")
        self.fav = fav
        kind = fav.get("kind", "other")
        self.cover = Cover(size, icon=ICONS.get(kind, ICONS["other"]), round_=kind == "artist")
        thumb = fav.get("thumbnail") or ""
        if not thumb and window.session.cache is not None:
            if kind == "album" and fav.get("albumid"):
                al = window.session.cache.album(fav["albumid"])
                thumb = al.thumbnail if al else ""
            elif kind == "artist" and fav.get("artistid"):
                ar = window.session.cache.artist(fav["artistid"])
                thumb = ar.thumbnail if ar else ""
        self.cover.set_art(window.session, thumb, "small")
        self.append(self.cover)
        title = Gtk.Label(label=fav.get("title") or "Favorite", xalign=0, max_width_chars=1,
                          hexpand=True, ellipsize=Pango.EllipsizeMode.END, visible=labels)
        title.add_css_class("tile-title")
        sub = Gtk.Label(label=describe(fav), xalign=0, max_width_chars=1, hexpand=True,
                        ellipsize=Pango.EllipsizeMode.END, visible=labels)
        sub.add_css_class("dim-label")
        sub.add_css_class("caption")
        self.append(title)
        self.append(sub)
        self.set_size_request(size, -1)
        self.set_halign(Gtk.Align.CENTER)   # titles no wider than the cover
        self.set_valign(Gtk.Align.START)
        attach_context_menu(self, lambda: favourite_menu(window, fav))


def favourites_flow(window, favs: list, size: int, labels: bool = True,
                    max_per_line: int = 16) -> Gtk.FlowBox:
    flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, valign=Gtk.Align.START, homogeneous=True,
                       min_children_per_line=1, max_children_per_line=max_per_line,
                       column_spacing=6, row_spacing=6, activate_on_single_click=True)
    flow.add_css_class("album-flow")
    flow.connect("child-activated",
                 lambda fb, child: open_favourite(window, child.get_child().fav))
    for fav in favs:
        flow.append(FavouriteTile(window, fav, size, labels))
    return flow


class FavouritesPage(Adw.NavigationPage):
    def __init__(self, window):
        super().__init__(title="Favorites", tag="favourites")
        self.window = window
        self.session = window.session
        self.dirty = True
        tv = Adw.ToolbarView()
        tv.add_top_bar(window.root_header())
        self.banner = Adw.Banner(title="Offline — showing the last known favorites")
        tv.add_top_bar(self.banner)
        self.slot = Adw.Bin(margin_top=12, margin_bottom=24, margin_start=12, margin_end=12)
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.stack.add_named(scrolled(self.slot), "grid")
        self.stack.add_named(status_page(
            "tonearm-starred-symbolic", "No Favorites",
            "Add albums and artists from their menus (★ on their pages), or pick "
            "favorites in Kodi itself — they show up here."), "empty")
        tv.set_content(self.stack)
        self.set_child(tv)
        register_other(self)
        follow(self.session, "favourites-changed", self, lambda page: page._render())
        # back online (or gone): fetch again, so the banner never outlives the outage
        follow(self.session, "notify::connected", self, lambda page: page._on_connected())
        self.connect("showing", lambda *_: self._on_showing())

    def refresh(self):
        self.dirty = False
        self.session.fetch_favourites()

    def _on_showing(self):
        # (a section page may have been off screen — and unhooked — during a change)
        if self.dirty or (self.session.favourites_stale and self.session.props.connected):
            self.refresh()
        else:
            self._render()

    def _on_connected(self):
        if self.session.props.connected and self.session.favourites_stale:
            if self.get_mapped():
                self.refresh()
            else:
                self.dirty = True
        self._sync_banner()

    def _sync_banner(self):
        """Offline banner only while really offline and showing the cached copy."""
        self.banner.set_revealed(self.session.favourites_stale
                                 and not self.session.props.connected
                                 and bool(self.session.favourites))

    def apply_album_prefs(self):
        self._render()

    def _render(self):
        """One section per kind: Artists, Albums, Songs, Playlists."""
        favs = self.session.favourites
        self._sync_banner()
        groups = grouped(favs)          # music only
        if not groups:
            self.stack.set_visible_child_name("empty")
            return
        p = album_prefs(self.window)
        size = TILE_SIZES.get(p["album_size"], TILE_SIZES["medium"])
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=28)
        for key, heading, items in groups:
            section = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
            head = Gtk.Box(spacing=10)
            title = Gtk.Label(label=heading, xalign=0)
            title.add_css_class("title-3")
            count = Gtk.Label(label=str(len(items)), valign=Gtk.Align.CENTER)
            count.add_css_class("dim-label")
            count.add_css_class("caption")
            head.append(title)
            head.append(count)
            section.append(head)
            if key == "song":
                section.append(self._song_list(items))
            else:
                section.append(favourites_flow(self.window, items, size, p["album_titles"]))
            box.append(section)
        self.slot.set_child(box)
        self.stack.set_visible_child_name("grid")

    def _song_list(self, songs: list) -> Gtk.Widget:
        """Songs read best as a list: title, what it is from, ★ to remove; a tap plays."""
        lst = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        lst.add_css_class("boxed-list")
        cache = self.session.cache
        for fav in songs:
            song = cache.song(fav["songid"]) if cache and fav.get("songid") else None
            sub = " · ".join(x for x in ((song.artist if song else ""),) if x) or "Song"
            row = plain_row(title=fav.get("title") or (song.title if song else "Song"),
                            subtitle=sub, activatable=True)
            row.add_prefix(Gtk.Image(icon_name="tonearm-song-symbolic"))
            star = Gtk.Button(icon_name="tonearm-starred-symbolic", valign=Gtk.Align.CENTER,
                              tooltip_text="Remove from Favorites")
            star.add_css_class("flat")
            star.add_css_class("track-star")
            star.add_css_class("on")
            star.connect("clicked", lambda _b, f=fav: self.session.remove_favourite(f))
            row.add_suffix(star)
            row.fav = fav
            attach_context_menu(row, lambda f=fav: favourite_menu(self.window, f))
            lst.append(row)
        lst.connect("row-activated", lambda _l, row: open_favourite(self.window, row.fav))
        return lst


__all__ = ["FavouritesPage", "FavouriteTile", "open_favourite", "fav_key"]
