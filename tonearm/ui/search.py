"""fzf-style search across artists, albums and songs."""
from __future__ import annotations

import html

from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk, Pango

from ..lidarr import best_image

from ..util import highlight
from .widgets import Cover, Spinner, album_menu, attach_context_menu, song_menu

KIND_ICON = {"artist": "tonearm-artist-symbolic", "album": "tonearm-album-symbolic",
             "song": "tonearm-song-symbolic", "lidarr": "tonearm-lidarr-symbolic"}
KIND_LABEL = {"artist": "Artist", "album": "Album", "song": "Song",
              "lidarr": "Not in Kodi — open to add with Lidarr"}
LIDARR_DELAY_MS = 450
SCOPES = [("all", "All"), ("artist", "Artists"), ("album", "Albums"), ("song", "Songs")]

HINT = ("Fuzzy search like fzf: type a few letters in order.\n"
        "<tt>'exact</tt>  <tt>^prefix</tt>  <tt>suffix$</tt>  <tt>!exclude</tt>  <tt>a | b</tt>")


class ResultObj(GObject.Object):
    __gtype_name__ = "TonearmResultObj"

    def __init__(self, entry, positions, data=None):
        super().__init__()
        self.kind, self.id, self.title, self.subtitle, self.thumb = entry
        self.positions = positions
        self.data = data  # Lidarr lookup dict for kind == "lidarr"

    @classmethod
    def header(cls, text):
        return cls(("header", 0, text, "", ""), [])

    @classmethod
    def from_lidarr(cls, item):
        bits = [x for x in (item.get("disambiguation"), item.get("artistType")) if x]
        bits.append("In Lidarr" if item.get("id") else "Add with Lidarr")
        return cls(("lidarr", 0, item.get("artistName") or "", " · ".join(bits), best_image(item)),
                   [], item)


class ResultRow(Gtk.Box):
    def __init__(self, session):
        super().__init__(spacing=12, margin_top=4, margin_bottom=4, margin_start=6, margin_end=6)
        self.session = session
        self.cover = Cover(44)
        self.avatar = Adw.Avatar(size=44, show_initials=True)
        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER, hexpand=True)
        self.spinner = Spinner(valign=Gtk.Align.CENTER, visible=False)
        self.title = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END, use_markup=True)
        self.sub = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END, use_markup=True)
        self.sub.add_css_class("dim-label")
        self.sub.add_css_class("caption")
        texts.append(self.title)
        texts.append(self.sub)
        self.kind = Gtk.Image(valign=Gtk.Align.CENTER)
        self.kind.add_css_class("dim-label")
        self.append(self.cover)
        self.append(self.avatar)
        self.append(texts)
        self.append(self.spinner)
        self.append(self.kind)
        self.obj: ResultObj | None = None
        attach_context_menu(self, self._menu)

    def _menu(self):
        o = self.obj
        if o is None:
            return None
        if o.kind == "album":
            return album_menu(o.id)
        if o.kind == "song":
            s = self.session.cache.song(o.id) if self.session.cache else None
            return song_menu(o.id, s.albumid if s else None)
        return None

    def bind(self, obj: ResultObj):
        self.obj = obj
        header = obj.kind == "header"
        self.sub.set_visible(not header)
        self.kind.set_visible(not header)
        self.spinner.set_visible(header and obj.id == 1)
        self.spinner.set_spinning(header and obj.id == 1)
        if header:
            self.avatar.set_visible(False)
            self.cover.set_visible(False)
            self.title.set_markup(f"<small><b>{html.escape(obj.title.upper())}</b></small>")
            self.title.add_css_class("dim-label")
            self.set_margin_top(14)
            return
        self.title.remove_css_class("dim-label")
        self.set_margin_top(4)
        n = len(obj.title)
        self.title.set_markup(highlight(obj.title, obj.positions))
        # haystack is "title  subtitle": shift positions past the separator
        self.sub.set_markup(highlight(obj.subtitle, [p for p in obj.positions if p >= n + 2], n + 2))
        self.kind.set_from_icon_name(KIND_ICON[obj.kind])
        self.kind.set_tooltip_text(KIND_LABEL[obj.kind])
        is_artist = obj.kind in ("artist", "lidarr")
        self.avatar.set_visible(is_artist)
        self.cover.set_visible(not is_artist)
        if is_artist:
            self.avatar.set_text(obj.title)
            self.avatar.set_custom_image(None)
            if obj.thumb and self.session.artwork:
                key = obj.thumb
                self.session.artwork.load(obj.thumb, "small", lambda t: (
                    self.obj is not None and self.obj.thumb == key and t is not None
                    and self.avatar.set_custom_image(t)))
        else:
            self.cover.set_art(self.session, obj.thumb, "small")


class SearchPanel(Gtk.Box):
    """Results card shown over the content while the header search bar has text."""

    def __init__(self, window):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.add_css_class("search-panel")
        self.window = window
        self.session = window.session
        self.scope = "all"
        self.query = ""
        self._debounce = 0
        self._kodi_results: list = []
        self._lidarr_results: list = []
        self._lidarr_state = "off"  # off | pending | done

        scopes = Gtk.Box(halign=Gtk.Align.CENTER, margin_top=8, margin_bottom=6)
        scopes.add_css_class("linked")
        first = None
        for key, label in SCOPES:
            b = Gtk.ToggleButton(label=label, group=first, active=first is None)
            first = first or b
            b.connect("toggled", self._on_scope, key)
            scopes.append(b)
        self.append(scopes)

        self.store = Gio.ListStore(item_type=ResultObj)
        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", lambda f, li: li.set_child(ResultRow(self.session)))
        factory.connect("bind", lambda f, li: li.get_child().bind(li.get_item()))
        self.list = Gtk.ListView(model=Gtk.SingleSelection(model=self.store, autoselect=False),
                                 factory=factory, single_click_activate=True)
        self.list.add_css_class("navigation-sidebar")
        self.list.connect("activate", lambda lv, pos: self._open(self.store.get_item(pos)))
        self.scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER,
                                           propagate_natural_height=True, vexpand=True,
                                           child=self.list)

        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE,
                               vhomogeneous=False, interpolate_size=True)
        empty = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin_top=24,
                        margin_bottom=24, margin_start=12, margin_end=12)
        title = Gtk.Label(label="No Results")
        title.add_css_class("title-4")
        hint = Gtk.Label(use_markup=True, wrap=True, justify=Gtk.Justification.CENTER)
        hint.set_markup(HINT)
        hint.add_css_class("dim-label")
        hint.add_css_class("caption")
        empty.append(title)
        empty.append(hint)
        self.stack.add_named(self.scroller, "results")
        self.stack.add_named(empty, "empty")
        self.append(self.stack)

        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._on_key)
        self.add_controller(keys)

    # -- driven by the window's search bar -------------------------------
    def set_query(self, q: str):
        self.query = q
        self._run()

    def set_max_height(self, h: int):
        self.scroller.set_max_content_height(max(160, h))

    def focus_results(self) -> bool:
        if self.store.get_n_items():
            self.list.grab_focus()
            return True
        return False

    def activate_first(self):
        for i in range(self.store.get_n_items()):
            if self.store.get_item(i).kind != "header":
                self._open(self.store.get_item(i))
                return

    def _on_key(self, ctl, keyval, keycode, state):
        if keyval == Gdk.KEY_Escape:
            self.window.close_search()
            return True
        return False

    def _on_scope(self, button, key):
        if button.get_active():
            self.scope = key
            self._run()

    def _run(self):
        q = self.query
        if self._debounce:
            GLib.source_remove(self._debounce)
            self._debounce = 0
        if not q.strip():
            self.session.lidarr_search("", lambda r: None)  # cancel any pending lookup
            self._kodi_results, self._lidarr_results = [], []
            self._lidarr_state = "off"
            self.store.remove_all()
            return

        def done(results):
            self._kodi_results = [ResultObj(entry, pos) for _, entry, pos in results]
            self._render(scroll=True)

        self.session.search(q, self.scope, done)

        wants_lidarr = (self.session.lidarr is not None and self.scope in ("all", "artist")
                        and len(q.strip()) >= 3 and not any(c in q for c in "'^$!|"))
        if wants_lidarr:
            self._lidarr_state = "pending"
            self._debounce = GLib.timeout_add(LIDARR_DELAY_MS, self._run_lidarr, q)
        else:
            self._lidarr_state = "off"
            self._lidarr_results = []

    def _run_lidarr(self, q):
        self._debounce = 0

        def done(items):
            if q != self.query:
                return
            self._lidarr_results = [ResultObj.from_lidarr(i) for i in items]
            self._lidarr_state = "done"
            self._render()

        self.session.lidarr_search(q, done)
        return GLib.SOURCE_REMOVE

    def _render(self, scroll=False):
        items = list(self._kodi_results)
        if self._lidarr_state == "pending":
            h = ResultObj.header("Searching Lidarr…")
            h.id = 1
            items.append(h)
        elif self._lidarr_state == "done" and self._lidarr_results:
            items.append(ResultObj.header("Not in Kodi · from Lidarr"))
            items += self._lidarr_results
        self.store.splice(0, self.store.get_n_items(), items)
        has = bool(self._kodi_results or self._lidarr_results or self._lidarr_state == "pending")
        self.stack.set_visible_child_name("results" if has else "empty")
        if scroll and items:
            self.list.scroll_to(0, Gtk.ListScrollFlags.NONE, None)

    def _open(self, obj: ResultObj):
        if obj.kind == "header":
            return
        self.window.close_search()
        if obj.kind == "lidarr":
            self.window.show_lidarr_artist(obj.data)
        elif obj.kind == "artist":
            self.window.show_artist(obj.id)
        elif obj.kind == "album":
            self.window.show_album(obj.id)
        else:
            self.session.play_song(obj.id)
            self.window.toast(f"Playing “{obj.title}”")
