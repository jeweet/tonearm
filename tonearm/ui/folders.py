"""Folders: Kodi's music sources, browsed folder by folder like a file manager's list
view (Nautilus-style columns, GNOME's own file icons) — also music that isn't in the
library yet."""
from __future__ import annotations

from gi.repository import Adw, Gio, GLib, GObject, Gtk, Pango

from ..folders import human_size, normalise, sort_entries
from ..listlayout import Spec
from ..util import fmt_time, fmt_total, plural
from .listview import TableView
from .widgets import Spinner, follow, scrolled, set_menu, status_page

ICONS = {"folder": "folder-symbolic", "song": "audio-x-generic-symbolic",
         "playlist": "view-list-symbolic"}


def _source_icon(path: str) -> str:
    p = (path or "").lower()
    if "://" in p and not p.startswith(("special://", "multipath://")):
        return "folder-remote-symbolic"            # smb://, nfs://, upnp://…
    return "folder-music-symbolic"


class EntryObj(GObject.Object):
    __gtype_name__ = "TonearmFolderEntry"

    def __init__(self, entry: dict):
        super().__init__()
        self.entry = entry


def file_specs() -> list[Spec]:
    return [
        Spec("icon", 24),
        Spec("name", title="Name"),
        Spec("artist", need=520, title="Artist", value=lambda e: e["artist"],
             meta=lambda e: e["artist"]),
        Spec("album", need=700, title="Album", value=lambda e: e["album"],
             meta=lambda e: ""),
        Spec("duration", 64, need=380, title="Length",
             value=lambda e: fmt_time(e["duration"]) if e["duration"] else "",
             meta=lambda e: fmt_time(e["duration"]) if e["duration"] else "",
             xalign=1.0, numeric=True),
        Spec("size", 80, need=820, title="Size", value=lambda e: human_size(e["size"]),
             meta=lambda e: "", xalign=1.0, numeric=True),
        Spec("modified", 128, need=960, title="Modified", value=lambda e: e["modified"],
             meta=lambda e: "", numeric=True),
        Spec("more", 34),
    ]


def folder_menu(path: str) -> Gio.Menu:
    m = Gio.Menu()
    sec = Gio.Menu()
    for label, action in (("Play", "win.folder-play"), ("Shuffle", "win.folder-shuffle"),
                          ("Play Next", "win.folder-next"),
                          ("Add to Queue", "win.folder-queue")):
        it = Gio.MenuItem.new(label, None)
        it.set_action_and_target_value(action, GLib.Variant("s", path))
        sec.append_item(it)
    m.append_section(None, sec)
    it = Gio.MenuItem.new("Scan into Library", None)
    it.set_action_and_target_value("win.folder-scan", GLib.Variant("s", path))
    m.append_item(it)
    return m


def file_menu(entry: dict) -> Gio.Menu:
    m = Gio.Menu()
    for label, action in (("Play", "win.file-play"), ("Play Next", "win.file-next"),
                          ("Add to Queue", "win.file-queue")):
        it = Gio.MenuItem.new(label, None)
        it.set_action_and_target_value(action, GLib.Variant("s", entry["path"]))
        m.append_item(it)
    return m


def entry_menu(entry: dict) -> Gio.Menu:
    return folder_menu(entry["path"]) if entry["kind"] == "folder" else file_menu(entry)


class FolderPage(Adw.NavigationPage):
    """``path`` None: the sources (the section's root); else one folder."""

    def __init__(self, window, path: str | None = None, label: str = ""):
        super().__init__(title=label or "Folders",
                         tag="folders" if path is None else f"folder-{path}")
        self.window = window
        self.session = window.session
        self.path = path
        # (not "dirty": the window marks those on every library change — a folder
        # listing doesn't follow the library, and reloading it lost the scroll place)
        self._stale = True
        self.entries: list[dict] = []
        self.sort_key, self.sort_desc = "name", False

        tv = Adw.ToolbarView()
        header = window.root_header() if path is None else window.search_header()
        tv.add_top_bar(header)
        if path is not None:
            # the folder's name, what's in it, Shuffle / Play — and ⋯ for the rest
            head = Gtk.Box(spacing=8, margin_top=12, margin_bottom=6, margin_start=18,
                           margin_end=18)
            texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True,
                            valign=Gtk.Align.CENTER)
            name = Gtk.Label(label=label, xalign=0, ellipsize=Pango.EllipsizeMode.MIDDLE)
            name.add_css_class("title-2")
            self.meta = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)
            self.meta.add_css_class("dim-label")
            texts.append(name)
            texts.append(self.meta)
            more = Gtk.MenuButton(icon_name="view-more-symbolic", valign=Gtk.Align.CENTER,
                                  tooltip_text="More")
            more.add_css_class("circular")
            set_menu(more, folder_menu(path))
            shuffle = Gtk.Button(icon_name="media-playlist-shuffle-symbolic",
                                 tooltip_text="Shuffle", valign=Gtk.Align.CENTER)
            shuffle.add_css_class("circular")
            shuffle.connect("clicked", lambda *_: self.session.play_folder(path, shuffle=True))
            play = Gtk.Button(icon_name="media-playback-start-symbolic", tooltip_text="Play",
                              valign=Gtk.Align.CENTER)
            play.add_css_class("circular")
            play.add_css_class("suggested-action")
            play.add_css_class("playlist-play")
            play.connect("clicked", lambda *_: self.session.play_folder(path))
            for w in (texts, more, shuffle, play):
                head.append(w)
            tv.add_top_bar(Adw.Clamp(maximum_size=1400, child=head))

        self.store = Gio.ListStore(item_type=EntryObj)
        self.table = TableView(
            file_specs(), self.store, prefix_key="icon", title_key="name", suffix_key="more",
            get=lambda o: o.entry, title=lambda e: e["name"],
            prefix=(lambda: Gtk.Image(valign=Gtk.Align.CENTER),
                    lambda w, o: w.set_from_icon_name(
                        _source_icon(o.entry["path"]) if path is None
                        else ICONS.get(o.entry["kind"], "text-x-generic-symbolic"))),
            suffix=(self._more_button, self._bind_more),
            on_sort=self._sort_clicked, on_activate=self._activate,
            menu=lambda o: entry_menu(o.entry))
        self.table.view.add_css_class("folder-list")
        self.table.set_sort("name", False)

        self.spinner = Spinner(size=32, halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.stack.add_named(self.table.wrap(scrolled(self.table.view)), "list")
        self.stack.add_named(self.spinner, "loading")
        self.empty = status_page("folder-symbolic", "Empty Folder",
                                 "No music or folders in here")
        retry = Gtk.Button(label="Try Again", halign=Gtk.Align.CENTER)
        retry.add_css_class("pill")
        retry.connect("clicked", lambda *_: self.refresh())
        self.empty.set_child(retry)
        self.stack.add_named(self.empty, "empty")
        tv.set_content(self.stack)
        self.set_child(tv)
        # "map", not "showing": a section's first page gets "showing" once, even while
        # another section is on screen — if Kodi wasn't connected yet then, the page
        # kept saying so when opened later.
        # Kodi says nothing when a source or file is added: look again each time the
        # page comes into view (quietly — the list only changes when its contents do)
        self.connect("map", lambda *_: self.refresh())
        follow(self.session, "notify::connected", self, lambda page: page._on_connected())
        if path is None:      # another Kodi: other sources
            follow(self.session, "instance-changed", self, lambda page: page._reload())

    @staticmethod
    def _more_button() -> Gtk.MenuButton:
        b = Gtk.MenuButton(icon_name="view-more-symbolic", valign=Gtk.Align.CENTER)
        b.add_css_class("flat")
        return b

    @staticmethod
    def _bind_more(button, obj):
        button.set_create_popup_func(lambda b, e=obj.entry: set_menu(b, entry_menu(e)))

    def _on_connected(self):
        if self.session.props.connected and self.stack.get_visible_child_name() == "empty":
            self._reload()

    def _reload(self):
        self._stale = True
        if self.get_mapped():
            self.refresh()

    # -- loading ------------------------------------------------------------
    def refresh(self):
        self._stale = False
        if not self.store.get_n_items():
            self.spinner.start()
            self.stack.set_visible_child_name("loading")
        if self.path is None:
            self.session.fetch_sources(self._got_sources)
        else:
            self.session.fetch_directory(self.path, self._got_files)

    def _got_sources(self, sources, error=""):
        self.spinner.stop()
        if sources is None:
            if error == "offline":
                self._show_problem("Kodi Is Offline",
                                   "Its music folders show here once Tonearm is connected")
            else:
                self._show_problem("Couldn't List the Music Sources",
                                   f"Kodi answered: {error}")
            return
        self.entries = [{"path": s.get("file") or "", "name": s.get("label") or s.get("file"),
                         "kind": "folder", "title": "", "artist": "", "album": "",
                         "duration": 0, "size": 0, "modified": "", "songid": 0}
                        for s in sources]
        if not self.entries:
            self._show_problem("No Music Sources",
                               "Add a music source in Kodi (Music › Files) to browse it here")
            return
        self._fill()

    def _got_files(self, files, error):
        self.spinner.stop()
        if files is None:
            self._show_problem("Can't Open This Folder", "Kodi is offline" if error == "offline"
                               else f"Kodi answered: {error or 'nothing'}")
            return
        self.entries = normalise(files)
        self._add_library_tags()
        folders = sum(1 for e in self.entries if e["kind"] == "folder")
        songs = sum(1 for e in self.entries if e["kind"] == "song")
        total = sum(e["duration"] for e in self.entries if e["kind"] == "song")
        bits = []
        if folders:
            bits.append(plural(folders, "folder"))
        if songs:
            bits.append(plural(songs, "song"))
        if total:
            bits.append(fmt_total(total))
        self.meta.set_label(" · ".join(bits) or "Empty")
        if not self.entries:
            self._show_problem("Empty Folder", "No music or folders in here")
            return
        self._fill()

    def _add_library_tags(self):
        """Files in the library: their artist, album and length from the cache where
        Kodi's folder listing left them out."""
        cache = self.session.cache
        if cache is None:
            return
        for e in self.entries:
            if not e["songid"] or (e["artist"] and e["duration"]):
                continue
            song = cache.song(e["songid"])
            if song is None:
                continue
            e["artist"] = e["artist"] or song.artist
            e["duration"] = e["duration"] or song.duration
            if not e["album"]:
                album = cache.album(song.albumid)
                e["album"] = album.title if album else ""

    def _show_problem(self, title, text):
        self.store.remove_all()
        self.empty.set_title(title)
        self.empty.set_description(GLib.markup_escape_text(text))
        self.stack.set_visible_child_name("empty")

    # columns that only show when a file in the folder has something for them: Kodi
    # often gives files without their tags (then a folder is just names)
    OPTIONAL = ("artist", "album", "duration", "size", "modified")

    def _fill(self, force: bool = False):
        self.table.set_hidden([k for k in self.OPTIONAL
                               if not any(e.get(k) for e in self.entries)])
        ordered = sort_entries(self.entries, self.sort_key, self.sort_desc)
        shown = [self.store.get_item(i).entry for i in range(self.store.get_n_items())]
        if force or ordered != shown:        # unchanged: keep the list (and its scroll)
            self.store.splice(0, self.store.get_n_items(), [EntryObj(e) for e in ordered])
        self.stack.set_visible_child_name("list")

    def _sort_clicked(self, key, descending):
        self.sort_key, self.sort_desc = key, descending
        self._fill(force=True)

    # -- opening / playing -------------------------------------------------
    def _activate(self, obj):
        e = obj.entry
        if e["kind"] == "folder":
            self.window.push_page(FolderPage(self.window, e["path"], e["name"]))
        elif e["kind"] == "playlist":
            self.session.play_playlist(e["path"])
        else:
            # this folder's songs in the order shown, from the one tapped
            songs = [x["path"] for x in sort_entries(self.entries, self.sort_key, self.sort_desc)
                     if x["kind"] == "song"]
            self.session.play_files(songs, songs.index(e["path"]) if e["path"] in songs else 0)


__all__ = ["FolderPage"]
