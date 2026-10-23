"""Main window: adaptive sidebar ⇄ bottom switcher layout."""
from __future__ import annotations

from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk, Pango

from ..config import APP_ID
from ..kodi import Instance
from ..sync import SyncResult
from .details import AlbumPage, ArtistPage
from .library import AlbumGridPage, ArtistListPage
from .recent import RecentPage
from .favourites import FavouritesPage
from .listview import SizeWatcher
from .widgets import (FAVOURITES, PLAYLISTS, RATINGS, ask_name, refresh_stars,
                      set_bottom_room)
from .player import NowPlayingBar, PlayerDialog
from .preferences import InstanceForm, PreferencesDialog
from .collections import DecadesPage, PlayedPage, PlaylistsPage, TopRatedPage
from .folders import FolderPage
from .genres import GenresPage
from .lidarr import LidarrArtistPage, LidarrPage
from .search import SearchPanel
from .widgets import Spinner
from .radio import radio_button
from .shortcuts import show_shortcuts
from .albumview import install_actions as install_album_actions

SECTIONS = [
    ("recent", "Spotlight", "tonearm-spotlight-symbolic"),
    ("artists", "Artists", "tonearm-artist-symbolic"),
    ("albums", "Albums", "tonearm-album-symbolic"),
    ("recent-added", "Recently Added", "tonearm-recently-added-symbolic"),
    ("recent-played", "Recently Played", "tonearm-played-symbolic"),
    ("most-played", "Most Played", "tonearm-most-played-symbolic"),
    ("top-rated", "Top Rated", "tonearm-star-half-symbolic"),
    ("genres", "Genres", "tonearm-genre-symbolic"),
    ("decades", "Decades", "tonearm-decade-symbolic"),
    ("playlists", "Playlists", "tonearm-playlist-symbolic"),
    ("favourites", "Favorites", "tonearm-starred-symbolic"),
    ("folders", "Folders", "folder-symbolic"),
    ("lidarr", "Lidarr", "tonearm-lidarr-symbolic"),
]
# The phone's bottom bar keeps to the main library views; everything else
# (Lidarr, future sections) is reached from the sidebar.
BOTTOM_BAR = ["recent", "artists", "albums"]


class Window(Adw.ApplicationWindow):
    __gtype_name__ = "TonearmWindow"

    toggle_visible = GObject.Property(type=bool, default=False)
    # 0 = phone, 1 = medium, 2 = wide; set by breakpoints, pages adapt their detail level
    layout = GObject.Property(type=int, default=2)
    # shared by every header search bar (they're all bound to this)
    search_text = GObject.Property(type=str, default="")

    def __init__(self, app):
        super().__init__(application=app, title="Tonearm", default_width=1100, default_height=760)
        self.set_size_request(360, 294)
        self.app = app
        self.session = app.session
        self.config = app.config
        self._player_dialog = None
        self._prefs = None
        self._sync_toast = None
        self._user_sync = False

        self._search_entries: list = []
        self._search_pairs: list = []      # search bar + radio button of every header
        self._search_width = 360           # their width (from the content pane's width)
        self._search_phone = False         # narrow: they fill the bar between its buttons
        self._setup_actions()
        self.instance_menu = Gio.Menu()
        self.primary_menu = self._build_primary_menu()

        # --- content ---------------------------------------------------
        self.view_stack = Adw.ViewStack(vexpand=True)
        self.navs: dict[str, Adw.NavigationView] = {}
        self.roots: dict[str, Adw.NavigationPage] = {}
        for name, title, icon in SECTIONS:
            nav = Adw.NavigationView()
            root = {"recent": lambda: RecentPage(self),
                    "albums": lambda: AlbumGridPage(self, "albums"),
                    "recent-added": lambda: AlbumGridPage(self, "recent"),
                    "artists": lambda: ArtistListPage(self),
                    "recent-played": lambda: PlayedPage(self, "recent"),
                    "most-played": lambda: PlayedPage(self, "most"),
                    "top-rated": lambda: TopRatedPage(self),
                    "genres": lambda: GenresPage(self),
                    "decades": lambda: DecadesPage(self),
                    "playlists": lambda: PlaylistsPage(self),
                    "folders": lambda: FolderPage(self),
                    "favourites": lambda: FavouritesPage(self),
                    "lidarr": lambda: LidarrPage(self)}[name]()
            nav.add(root)
            nav.connect("popped", lambda *_: self._sync_visible_page())
            nav.connect("pushed", lambda *_: self._sync_visible_page())
            self.navs[name] = nav
            self.roots[name] = root
            stack_page = self.view_stack.add_titled_with_icon(nav, name, title, icon)
            if name == "lidarr":
                self._lidarr_stack_page = stack_page

        self.now_playing = NowPlayingBar(self)
        self.switcher_bar = self._build_bottom_bar()

        # Search results float over the pages, below their header bars.
        self.search_panel = SearchPanel(self)
        self.search_clamp = Adw.Clamp(maximum_size=720, child=self.search_panel,
                                      valign=Gtk.Align.START, visible=False)
        self.search_scrim = Gtk.Box(visible=False, hexpand=True, vexpand=True)
        self.search_scrim.add_css_class("search-scrim")
        scrim_click = Gtk.GestureClick()
        scrim_click.connect("released", lambda *_: self.close_search())
        self.search_scrim.add_controller(scrim_click)
        # one surface: the pages' top bars in the page colour (style.css .main-content)
        self.view_stack.add_css_class("main-content")
        search_layer = Gtk.Overlay(child=self.view_stack)
        search_layer.add_overlay(self.search_scrim)
        search_layer.add_overlay(self.search_clamp)
        self.search_layer = search_layer
        self.connect("notify::search-text", lambda *_: self._on_search_text())
        # the search bars' width follows the content pane's width only (not how many
        # buttons a page puts in its top bar)
        content = Adw.ToolbarView(content=SizeWatcher(search_layer, self._on_content_width))
        # one thin line above the player bar (like the line under the list column
        # titles), instead of a soft shadow plus a border
        content.set_bottom_bar_style(Adw.ToolbarStyle.RAISED_BORDER)
        content.add_css_class("main-toolbar")
        # Kodi offline: a banner above the player bar offers to wake its machine
        self.offline_banner = Adw.Banner(revealed=False)
        self.offline_banner.connect("button-clicked", lambda *_: self._on_offline_banner())
        content.add_bottom_bar(self.offline_banner)
        content.add_bottom_bar(self.now_playing)
        content.add_bottom_bar(self.switcher_bar)
        # glass player bar: the pages run on under the bottom bars and show through the
        # player, blurred; the pages' scroll areas get that much room at the end, so
        # nothing stays out of reach behind the bars
        content.set_extend_content_to_bottom_edge(True)
        self.now_playing.glass.set_source(search_layer)
        # An unfocused window redraws only the changed strip; a new track then repainted
        # just the bar and the blur (which samples past the bar's edges) came out empty,
        # leaving it merely see-through. Repaint the whole window on those moments.
        self.connect("notify::is-active", lambda *_: self._redraw_glass())
        self.session.connect("track-changed", lambda *_: self._redraw_glass())
        self._under_bars = Gtk.CssProvider()
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), self._under_bars, Gtk.STYLE_PROVIDER_PRIORITY_USER + 2)
        content.connect("notify::bottom-bar-height", lambda c, _p: self._room_under_bars(
            c.get_bottom_bar_height()))
        self._room_under_bars(0)

        # --- sidebar ---------------------------------------------------
        self.sidebar_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.sidebar_list.add_css_class("navigation-sidebar")
        self._sidebar_rows = {}
        for name, title, icon in SECTIONS:
            row = Gtk.ListBoxRow()
            box = Gtk.Box(spacing=12, margin_start=6, margin_end=6, margin_top=6, margin_bottom=6)
            box.append(Gtk.Image(icon_name=icon))
            box.append(Gtk.Label(label=title, xalign=0, hexpand=True))
            row.set_child(box)
            row.section = name
            self.sidebar_list.append(row)
            self._sidebar_rows[name] = row
        self.sidebar_list.connect("row-activated", self._on_sidebar_row)
        # Lidarr isn't part of the Kodi library: set it apart with a separator
        self.sidebar_list.set_header_func(
            lambda row, before: row.set_header(
                Gtk.Separator(margin_top=6, margin_bottom=6, margin_start=6, margin_end=6)
                if row.section == "lidarr" else None))

        self.sidebar_title = Adw.WindowTitle(title="Tonearm")  # state lives in the footer
        sb_header = Adw.HeaderBar(title_widget=self.sidebar_title)
        sb_header.pack_end(Gtk.MenuButton(icon_name="tonearm-menu-symbolic", menu_model=self.primary_menu,
                                          primary=True, tooltip_text="Main Menu"))
        self.sync_box = self._build_sync_footer()
        sidebar = Adw.ToolbarView(content=Gtk.ScrolledWindow(child=self.sidebar_list,
                                                             hscrollbar_policy=Gtk.PolicyType.NEVER))
        sidebar.add_top_bar(sb_header)
        sidebar.add_bottom_bar(self.sync_box)

        self.split = Adw.OverlaySplitView(sidebar=sidebar, content=content,
                                          min_sidebar_width=200, max_sidebar_width=260)
        self.toasts = Adw.ToastOverlay(child=self.split)

        # --- welcome (no instance yet) ----------------------------------
        # not homogeneous: only the page shown sets the window's minimum size (the
        # welcome page alone on a phone)
        self.top = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE,
                             hhomogeneous=False, vhomogeneous=False)
        self.top.add_named(self.toasts, "main")
        self.top.add_named(self._build_welcome(), "welcome")
        self.set_content(self.top)

        # --- adaptive breakpoints --------------------------------------
        medium = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 860sp"))
        medium.add_setter(self.split, "collapsed", True)
        medium.add_setter(self, "layout", 1)
        narrow = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 560sp"))
        narrow.add_setter(self.split, "collapsed", True)
        narrow.add_setter(self.switcher_bar, "reveal-child", True)
        narrow.add_setter(self, "layout", 0)
        self.add_breakpoint(medium)
        self.add_breakpoint(narrow)
        for obj, prop in ((self.split, "collapsed"), (self.switcher_bar, "reveal-child")):
            obj.connect(f"notify::{prop}", lambda *_: self._update_toggle())
        self._update_toggle()

        # --- wiring -----------------------------------------------------
        self.view_stack.connect("notify::visible-child-name", self._on_section_changed)
        s = self.session
        s.connect("library-changed", self._on_library_changed)
        s.connect("player-changed", lambda *_: self._on_player_changed())
        s.connect("track-changed", lambda *_: self._on_track_changed())
        s.connect("instance-changed", lambda *_: self._on_instance_changed())
        s.connect("sync-progress", self._on_sync_progress)
        s.connect("sync-finished", self._on_sync_finished)
        s.connect("toast", lambda _s, msg: self.toast(msg))
        s.connect("problem", lambda _s, msg, details: self.toast(msg, details))
        s.connect("lidarr-changed", lambda *_: self._update_lidarr_visibility())
        self._update_lidarr_visibility()
        s.connect("notify::syncing", lambda *_: self._on_syncing())
        s.connect("notify::connected", lambda *_: self._on_instance_changed())
        s.connect("notify::connected", lambda *_: self._on_connected_changed())
        s.connect("notify::offline", lambda *_: self._update_offline())
        s.connect("instance-changed", lambda *_: self._update_offline())
        s.connect("instance-changed", lambda *_: self._update_power())
        s.connect("notify::connected", lambda *_: self._update_power())
        s.connect("notify::live", lambda *_: self._update_sync_label())
        s.connect("ratings-changed", lambda *_: self._on_ratings_changed())
        self._on_ratings_changed()

        self.connect("close-request", self._on_close_request)
        self.connect("notify::is-active", lambda *_: s.set_foreground(self.get_visible()))

        self.rebuild_instance_menu()
        start = "recent"
        self.view_stack.set_visible_child_name(start)
        self._sync_bottom_bar()
        self.sidebar_list.select_row(self._sidebar_rows[start])
        self._on_instance_changed()

    # ------------------------------------------------------------------
    # building blocks
    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    # header search bar + results overlay
    # ------------------------------------------------------------------
    def search_header(self) -> Adw.HeaderBar:
        """A page header with the search bar (and radio button) as its title, always in
        the middle of the bar — however many buttons the page puts at either end."""
        return Adw.HeaderBar(title_widget=self.make_search_entry(),
                             centering_policy=Adw.CenteringPolicy.STRICT)

    def make_search_entry(self) -> Gtk.Widget:
        """A search bar for a page header; all of them share one query."""
        entry = Gtk.SearchEntry(placeholder_text="Search artists, albums, songs", hexpand=True,
                                search_delay=80)
        # A text field asks for ~150 px by default and grows with its text; here it is
        # just as wide as its box, which _fit_search sizes (on a phone: all the bar allows).
        entry.set_width_chars(6)
        entry.set_max_width_chars(6)   # its width comes from _fit_search instead
        self.bind_property("search-text", entry, "text",
                           GObject.BindingFlags.SYNC_CREATE | GObject.BindingFlags.BIDIRECTIONAL)
        entry.connect("activate", lambda *_: self.search_panel.activate_first())
        focus = Gtk.EventControllerFocus()
        focus.connect("enter", lambda *_: self.session.warm_index())  # build the index on first use
        entry.add_controller(focus)
        entry.connect("stop-search", lambda *_: self.close_search())
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", lambda c, kv, kc, st: kv == Gdk.KEY_Down
                     and self.search_panel.focus_results())
        entry.add_controller(keys)
        self._search_entries.append(entry)
        # search bar + "What should I play?" (radio) directly to its right: both inside
        # the clamp, so the button sits against the field instead of at the far end of
        # the header's title area
        pair = Gtk.Box(spacing=6, halign=Gtk.Align.CENTER)
        pair.append(entry)
        pair.append(radio_button(self))
        pair.connect("map", lambda p: self._fit_search(p))
        self._search_pairs.append(pair)
        self._fit_search(pair)
        return pair

    def _redraw_glass(self):
        def go():
            self.queue_draw()
            self.now_playing.glass.queue_draw()
            return GLib.SOURCE_REMOVE
        GLib.idle_add(go)

    def _fit_search(self, pair: Gtk.Widget):
        """Desktop: one fixed width on every page, centred in the bar. Phone: as wide as
        the bar allows — everything between its buttons (the header then centres it
        loosely, so a page with more buttons simply leaves it a little less room)."""
        phone = self._search_phone
        pair.set_size_request(self.SEARCH_MIN if phone else self._search_width, -1)
        pair.set_hexpand(phone)
        pair.set_halign(Gtk.Align.FILL if phone else Gtk.Align.CENTER)
        # A header bar gives its title its *natural* width (expanding does nothing
        # there), and that was the 140 px minimum. On a phone the field asks for far
        # more than any screen has, so the bar hands it all the room between its
        # buttons and shrinks it (down to the minimum) when a button appears.
        entry = pair.get_first_child()
        if entry is not None:
            entry.set_max_width_chars(80 if phone else 6)
        header = pair.get_ancestor(Adw.HeaderBar)
        if header is not None:
            header.set_centering_policy(Adw.CenteringPolicy.LOOSE if phone
                                        else Adw.CenteringPolicy.STRICT)

    # The search bar keeps one width on every page: a share of the content pane, less
    # room for the top bar's buttons on both sides (the busiest page's, so it never
    # has to give way), between a phone's 140 px and 520 px.
    SEARCH_SIDE = 132           # per side: sidebar toggle | view button + more
    SEARCH_MIN, SEARCH_MAX = 140, 520

    def _room_under_bars(self, height: int):
        """Room at the end of every page scroller, as tall as the bottom bars."""
        # only the pages' own scrollers: a menu's popover (which lives inside the page
        # too) has a scrolled window of its own, and got that room as empty rows
        # boxes get a margin, lists and grids padding: a margin on a list shrank its
        # visible area, so nothing ever scrolled behind the glass player bar (on a
        # phone's Albums / Artists pages it looked solid)
        css = (".main-content scrolledwindow.page-scroller > viewport > *"
               f"{{ margin-bottom: {int(height)}px; }}"
               ".main-content scrolledwindow.page-scroller > gridview,"
               ".main-content scrolledwindow.page-scroller > listview,"
               ".main-content scrolledwindow.page-scroller > columnview > listview"
               f"{{ padding-bottom: {int(height)}px; }}")
        set_bottom_room(height)    # the A–Z rails and back-to-top buttons
        if hasattr(self._under_bars, "load_from_string"):
            self._under_bars.load_from_string(css)
        else:
            self._under_bars.load_from_data(css.encode(), -1)

    def _on_content_width(self, width: int):
        phone = width < 600
        w = max(self.SEARCH_MIN, min(self.SEARCH_MAX, width - 2 * self.SEARCH_SIDE))
        if w == self._search_width and phone == self._search_phone:
            return
        self._search_width, self._search_phone = w, phone
        # pages not on screen catch up when they map again; forget them here
        self._search_pairs = [p for p in self._search_pairs if p.get_root() is not None]
        for p in self._search_pairs:
            self._fit_search(p)

    def _visible_entry(self) -> Gtk.SearchEntry | None:
        self._search_entries = [e for e in self._search_entries if e.get_root() is not None]
        return next((e for e in self._search_entries if e.get_mapped()), None)

    def _on_search_text(self):
        q = self.props.search_text
        showing = bool(q.strip())
        if showing and not self.search_clamp.get_visible():
            self._place_search_panel()
        self.search_clamp.set_visible(showing)
        self.search_scrim.set_visible(showing)
        self.search_panel.set_query(q)

    def _place_search_panel(self):
        """Put the card just under the visible header bar; full-bleed on phones."""
        entry = self._visible_entry()
        header = entry.get_ancestor(Adw.HeaderBar) if entry else None
        top = header.get_height() if header is not None and header.get_height() > 0 else 47
        phone = self.props.layout == 0
        for w in (self.search_scrim, self.search_clamp):
            w.set_margin_top(top)
        self.search_clamp.set_margin_start(0 if phone else 12)
        self.search_clamp.set_margin_end(0 if phone else 12)
        self.search_clamp.set_valign(Gtk.Align.FILL if phone else Gtk.Align.START)
        if phone:
            self.search_panel.add_css_class("phone")
        else:
            self.search_panel.remove_css_class("phone")
        avail = self.search_layer.get_height() - top
        self.search_panel.set_max_height(int(avail * (0.95 if phone else 0.75)) - 60)

    def close_search(self):
        self.props.search_text = ""
        entry = self._visible_entry()
        if entry is not None and entry.has_focus():
            self.view_stack.grab_focus()

    def root_header(self, with_title: bool = True) -> Adw.HeaderBar:
        """Header for section root pages: sidebar toggle, search bar.
        The main menu lives in the sidebar header (the toggle reveals it when collapsed),
        sync progress only in the sidebar's footer."""
        header = self.search_header()
        toggle = Gtk.ToggleButton(icon_name="tonearm-sidebar-symbolic", tooltip_text="Show Sidebar")
        self.bind_property("toggle-visible", toggle, "visible", GObject.BindingFlags.SYNC_CREATE)
        GLib.idle_add(lambda: (self.split.bind_property(
            "show-sidebar", toggle, "active",
            GObject.BindingFlags.SYNC_CREATE | GObject.BindingFlags.BIDIRECTIONAL), False)[1])
        header.pack_start(toggle)
        return header

    def _update_toggle(self):
        collapsed = self.split.get_collapsed()
        # Collapsed (medium and phone): the toggle opens the sidebar as an overlay, which
        # holds the main menu and the Kodi status.
        self.props.toggle_visible = collapsed
        if collapsed:
            self.split.set_show_sidebar(False)
        self.now_playing.set_compact(self.switcher_bar.get_reveal_child())

    def _build_bottom_bar(self) -> Gtk.Revealer:
        """Phone navigation: like Adw.ViewSwitcherBar, but only for BOTTOM_BAR sections."""
        box = Gtk.Box(homogeneous=True, spacing=4)
        box.add_css_class("bottom-switcher")
        self._bottom_buttons = {}
        titles = {name: (title, icon) for name, title, icon in SECTIONS}
        self._syncing_bottom = False
        for name in BOTTOM_BAR:
            title, icon = titles[name]
            inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            inner.append(Gtk.Image(icon_name=icon))
            lbl = Gtk.Label(label=title, ellipsize=Pango.EllipsizeMode.END)
            lbl.add_css_class("caption")
            inner.append(lbl)
            # Not a toggle *group*: when Lidarr (sidebar-only) is shown, none is active.
            b = Gtk.ToggleButton(child=inner)
            b.add_css_class("flat")
            b.connect("toggled", self._on_bottom_toggled, name)
            box.append(b)
            self._bottom_buttons[name] = b
        bar = Gtk.Revealer(child=box, reveal_child=False,
                           transition_type=Gtk.RevealerTransitionType.SLIDE_UP)
        box.add_css_class("tonearm-tabbar")  # not "bottom-bar": libadwaita uses that name itself
        return bar

    def _on_bottom_toggled(self, button, name):
        if self._syncing_bottom:
            return
        if self.view_stack.get_visible_child_name() == name:
            # tapping the current section again: back to its first page
            self.navs[name].pop_to_tag(name)
        else:
            self.view_stack.set_visible_child_name(name)
        self._sync_bottom_bar()

    def _sync_bottom_bar(self):
        current = self.view_stack.get_visible_child_name()
        self._syncing_bottom = True
        try:
            for name, b in self._bottom_buttons.items():
                b.set_active(name == current)  # none active for sidebar-only sections
        finally:
            self._syncing_bottom = False

    def _build_primary_menu(self) -> Gio.Menu:
        menu = Gio.Menu()
        menu.append_submenu("Kodi Instance", self.instance_menu)
        lib = Gio.Menu()
        lib.append("Sync Changes", "win.sync")
        lib.append("Rebuild Cache", "win.full-sync")
        menu.append_section(None, lib)
        app = Gio.Menu()
        app.append("Preferences", "win.preferences")
        app.append("Keyboard Shortcuts", "win.shortcuts")
        app.append("About Tonearm", "app.about")
        app.append("Quit", "app.quit")
        menu.append_section(None, app)
        return menu

    def rebuild_instance_menu(self):
        self.instance_menu.remove_all()
        sec = Gio.Menu()
        for inst in self.config.instances:
            it = Gio.MenuItem.new(inst.name, None)
            it.set_action_and_target_value("win.instance", GLib.Variant("s", inst.id))
            sec.append_item(it)
        self.instance_menu.append_section(None, sec)
        wake = Gio.MenuItem.new("Wake Up Kodi", "win.wake")
        wake.set_attribute_value("hidden-when", GLib.Variant("s", "action-disabled"))
        self.instance_menu.append_item(wake)
        self.instance_menu.append("Manage Instances…", "win.preferences")
        act = self.lookup_action("instance")
        if act and self.config.active:
            act.set_state(GLib.Variant("s", self.config.active.id))

    def _build_sync_footer(self) -> Gtk.Widget:
        """Bottom of the sidebar, kept simple: a status dot, the Kodi's name and one line
        of state; a spinner while the library syncs. A click opens its settings."""
        # lined up with the sidebar's rows: same side margin, same inset, the dot centred
        # where the section icons are (16 px wide, 12 px before the text)
        inner = Gtk.Box(spacing=12)
        self.status_dot = Gtk.Image(icon_name="tonearm-status-symbolic", pixel_size=10,
                                    valign=Gtk.Align.CENTER, width_request=16)
        self.status_dot.add_css_class("status-dot")
        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True, spacing=2)
        # max_width_chars=1: take only the width the sidebar gives (no widening it)
        self.instance_label = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END,
                                        max_width_chars=1, hexpand=True)
        self.instance_label.add_css_class("heading")
        self.sync_label = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END,
                                    max_width_chars=1, hexpand=True)
        self.sync_label.add_css_class("caption")
        self.sync_label.add_css_class("dim-label")
        texts.append(self.instance_label)
        texts.append(self.sync_label)
        self.sync_spinner = Spinner(size=16, valign=Gtk.Align.CENTER, visible=False)
        inner.append(self.status_dot)
        inner.append(texts)
        inner.append(self.sync_spinner)
        # a menu: Kodi's settings, waking it up, and its power options
        btn = Gtk.MenuButton(child=inner, tooltip_text="Kodi", direction=Gtk.ArrowType.UP,
                             menu_model=self._kodi_menu(),
                             margin_start=6, margin_end=6, margin_top=6, margin_bottom=6)
        btn.add_css_class("flat")
        btn.add_css_class("instance-status")
        return btn

    POWER_ITEMS = [("quit", "Quit Kodi"), ("suspend", "Suspend"),
                   ("hibernate", "Hibernate"), ("reboot", "Restart"),
                   ("shutdown", "Shut Down")]

    def _kodi_menu(self) -> Gio.Menu:
        """Settings; Wake Up (offline); Quit Kodi / Suspend / … (what its system
        allows, while connected). Unavailable items are hidden, not greyed out."""
        def item(label, action):
            it = Gio.MenuItem.new(label, action)
            it.set_attribute_value("hidden-when", GLib.Variant("s", "action-disabled"))
            return it

        menu = Gio.Menu()
        top = Gio.Menu()
        top.append("Kodi Settings", "win.kodi-settings")
        top.append_item(item("Wake Up Kodi", "win.wake"))
        menu.append_section(None, top)
        power = Gio.Menu()
        for key, label in self.POWER_ITEMS:
            power.append_item(item(label, f"win.power-{key}"))
        menu.append_section(None, power)
        return menu

    def _update_power(self):
        s = self.session
        for key, _label in self.POWER_ITEMS:
            act = self.lookup_action(f"power-{key}")
            if act is not None:
                act.set_enabled(bool(s.props.connected) and key in s.power_options)

    def _confirm_power(self, key: str):
        s = self.session
        name = s.instance.name if s.instance else "Kodi"
        heading, body, verb = {
            "quit": (f"Quit Kodi on {name}?", "Tonearm can't start it again.", "Quit"),
            "suspend": (f"Suspend {name}?", "Wake-on-LAN can wake it again.", "Suspend"),
            "hibernate": (f"Hibernate {name}?", "Wake-on-LAN can wake it again.", "Hibernate"),
            "reboot": (f"Restart {name}?", "Tonearm reconnects once it's back.", "Restart"),
            "shutdown": (f"Shut down {name}?", "Wake-on-LAN can start it again, if "
                                               "its machine supports that.", "Shut Down"),
        }[key]
        dlg = Adw.AlertDialog(heading=heading, body=body)
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("go", verb)
        dlg.set_response_appearance("go", Adw.ResponseAppearance.DESTRUCTIVE
                                    if key in ("shutdown", "reboot", "quit")
                                    else Adw.ResponseAppearance.SUGGESTED)
        dlg.set_default_response("cancel")
        dlg.connect("response", lambda _d, r: r == "go" and s.power(key))
        dlg.present(self)

    def _build_welcome(self) -> Gtk.Widget:
        tv = Adw.ToolbarView()
        # the header bar only carries the window buttons (desktop); it's see-through and
        # the page runs under it, so on a phone (no window buttons) no empty bar shows
        header = Adw.HeaderBar(show_title=False)
        header.add_css_class("welcome-header")
        tv.add_top_bar(header)
        tv.set_extend_content_to_top_edge(True)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24,
                      margin_start=12, margin_end=12, margin_bottom=24)
        # the app icon, a title and a line of text — built here rather than an
        # Adw.StatusPage: that scrolls by itself, and inside this scrolling page it got
        # squeezed to a sliver (the icon showed only its top half)
        intro = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin_top=36)
        intro.append(Gtk.Image(icon_name=APP_ID, pixel_size=96, margin_bottom=8))
        title = Gtk.Label(label="Connect to Kodi", wrap=True, justify=Gtk.Justification.CENTER)
        title.add_css_class("title-1")
        text = Gtk.Label(label="Browse and play your Kodi music library from here.",
                         wrap=True, justify=Gtk.Justification.CENTER)
        text.add_css_class("dim-label")
        intro.append(title)
        intro.append(text)
        box.append(intro)
        form = InstanceForm()
        box.append(form)
        btn = Gtk.Button(label="Connect", halign=Gtk.Align.CENTER)
        btn.add_css_class("pill")
        btn.add_css_class("suggested-action")

        def connect(*_):
            if not form.valid():
                return

            def ok(_info):
                inst = self.config.new_instance(**form.values())
                self.rebuild_instance_menu()
                self.switch_instance(inst.id)

            form.test(on_ok=ok)

        btn.connect("clicked", connect)
        box.append(btn)
        tv.set_content(Gtk.ScrolledWindow(child=Adw.Clamp(maximum_size=520, child=box),
                                          hscrollbar_policy=Gtk.PolicyType.NEVER))
        return tv

    # ------------------------------------------------------------------
    # actions
    # ------------------------------------------------------------------
    def _setup_space_key(self):
        """Space plays / pauses anywhere — except while typing in a text field. Caught
        before focused buttons or list rows would use it to activate themselves."""
        keys = Gtk.EventControllerKey(propagation_phase=Gtk.PropagationPhase.CAPTURE)

        def pressed(_c, keyval, _code, state):
            if keyval != Gdk.KEY_space:
                return False
            mods = Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.ALT_MASK | \
                Gdk.ModifierType.SHIFT_MASK | Gdk.ModifierType.SUPER_MASK
            if state & mods:
                return False
            focus = self.get_focus()
            if isinstance(focus, Gtk.Editable):
                return False  # a space in the search box or a settings field
            if not self.session.state.active:
                return False  # nothing to pause or resume
            self.session.play_pause()
            return True

        keys.connect("key-pressed", pressed)
        self.add_controller(keys)

    def _setup_actions(self):
        s = self.session
        self._setup_space_key()

        def add(name, cb, ptype=None):
            act = Gio.SimpleAction.new(name, GLib.VariantType(ptype) if ptype else None)
            act.connect("activate", lambda a, p: cb(p.unpack() if p is not None else None))
            self.add_action(act)
            return act

        # sleep timer: a radio menu (player dialog), kept in step with the session
        sleep = Gio.SimpleAction.new_stateful("sleep", GLib.VariantType("s"),
                                              GLib.Variant("s", s.sleep_choice))
        sleep.connect("change-state", lambda a, v: s.set_sleep(v.unpack()))
        s.connect("sleep-changed", lambda *_: sleep.set_state(GLib.Variant("s", s.sleep_choice)))
        self.add_action(sleep)
        add("wake", lambda _p: s.wake()).set_enabled(False)   # Kodi offline + MAC known
        add("kodi-settings", lambda _p: self.show_preferences("kodi"))
        for key, _label in self.POWER_ITEMS:
            add(f"power-{key}", lambda _p, k=key: self._confirm_power(k)).set_enabled(False)
        add("play-album", lambda i: s.play_album(i), "i")
        add("shuffle-album", lambda i: s.play_album(i, shuffle=True), "i")
        add("queue-album", lambda i: s.enqueue({"albumid": i}), "i")
        add("next-album", lambda i: s.play_next({"albumid": i}), "i")
        add("play-song", lambda i: s.play_song(i), "i")
        add("queue-song", lambda i: s.enqueue({"songid": i}), "i")
        add("next-song", lambda i: s.play_next({"songid": i}), "i")
        add("play-artist", lambda i: s.play_artist(i), "i")
        add("shuffle-artist", lambda i: s.play_artist(i, shuffle=True), "i")
        add("show-album", self.show_album_id, "i")
        add("show-artist", self.show_artist, "i")
        add("fav-album", lambda i: s.toggle_album_favourite(i), "i")
        add("fav-artist", lambda i: s.toggle_artist_favourite(i), "i")
        add("fav-song", lambda i: s.toggle_song_favourite(i), "i")
        add("rate-song", lambda p: s.rate("song", p[0], p[1]), "(ii)")
        # Kodi's Party Mode: (smart playlist path or "" for the library, its name)
        add("partymode", lambda p: s.start_partymode(p[0], p[1]), "(ss)")
        add("stop-partymode", lambda _p: s.stop_partymode())
        add("play-kodi-playlist", lambda f: s.play_playlist(f), "s")
        # Folders: a folder (with everything under it) or one file
        add("folder-play", lambda f: s.play_folder(f), "s")
        add("folder-shuffle", lambda f: s.play_folder(f, shuffle=True), "s")
        add("folder-next", lambda f: s.play_next({"directory": f, "recursive": True}), "s")
        add("folder-queue", lambda f: s.enqueue({"directory": f, "recursive": True}), "s")
        add("folder-scan", lambda f: s.scan_library(f), "s")
        add("file-play", lambda f: s.play_files([f]), "s")
        add("file-next", lambda f: s.play_next({"file": f}), "s")
        add("file-queue", lambda f: s.enqueue({"file": f}), "s")
        add("shuffle-kodi-playlist", lambda f: s.play_playlist(f, shuffle=True), "s")
        add("rate-album", lambda p: s.rate("album", p[0], p[1]), "(ii)")
        add("playlist-add", self._playlist_add, "s")
        add("playlist-new", lambda _: ask_name(self, "New Playlist", "", "Create",
                                                lambda n: s.create_playlist(n)))
        s.connect("playlists-changed", lambda *_: self._on_playlists_changed())
        self._on_playlists_changed()     # the session may have loaded them already
        # your playlists (by id) — from the Playlists page's long-press menu and a
        # playlist's own page
        add("myplaylist-play", lambda pid: s.play_my_playlist(pid), "s")
        add("myplaylist-shuffle", lambda pid: s.play_my_playlist(pid, shuffle=True), "s")
        add("myplaylist-rename", self.rename_playlist, "s")
        add("myplaylist-duplicate", lambda pid: s.duplicate_playlist(pid), "s")
        add("myplaylist-delete", self.delete_playlist, "s")
        add("myplaylist-sort", lambda p: s.sort_playlist(*p.split(":", 1)), "s")
        # a song in one of your playlists: "pid:index"
        add("myplaylist-top", lambda p: self._playlist_move(p, "top"), "s")
        add("myplaylist-bottom", lambda p: self._playlist_move(p, "bottom"), "s")
        add("myplaylist-remove", lambda p: s.remove_from_playlist(
            p.rsplit(":", 1)[0], int(p.rsplit(":", 1)[1])), "s")
        add("save-queue", lambda _: ask_name(self, "Save Queue as Playlist", "", "Save",
                                             s.save_queue_as_playlist))
        add("copy-kodi-playlist", lambda p: s.copy_kodi_playlist(p[0], p[1]), "(ss)")
        add("queue-artist", lambda i: s.enqueue({"artistid": i}), "i")
        add("next-artist", lambda i: s.play_next({"artistid": i}), "i")
        add("fav-remove", self._remove_favourite, "s")
        add("fav-play", self._play_favourite, "s")
        s.connect("favourites-changed", lambda *_: self._on_favourites_changed())
        add("artist-of-album", self._artist_of_album, "i")
        add("sync", lambda _: self._user_sync_start(False))
        add("full-sync", lambda _: self._user_sync_start(True))
        add("preferences", lambda _: self.show_preferences())
        add("shortcuts", lambda _: show_shortcuts(self))
        add("search", lambda _: self.focus_search())
        add("play-pause", lambda _: s.play_pause())
        add("next", lambda _: s.next())
        add("previous", lambda _: s.previous())
        add("show-player", lambda _: self.show_player())
        add("volume-up", lambda _: s.set_volume(s.state.volume + 5))
        add("volume-down", lambda _: s.set_volume(s.state.volume - 5))

        install_album_actions(self)

        inst = Gio.SimpleAction.new_stateful(
            "instance", GLib.VariantType("s"),
            GLib.Variant("s", self.config.active.id if self.config.active else ""))
        inst.connect("change-state", lambda a, v: self.switch_instance(v.unpack()))
        self.add_action(inst)

        app = self.app
        for accel, action in (("<Ctrl>f", "win.search"), ("<Ctrl>k", "win.search"),
                              ("<Ctrl>comma", "win.preferences"), ("<Ctrl>r", "win.sync"),
                              ("<Ctrl>space", "win.play-pause"), ("<Ctrl>Right", "win.next"),
                              ("<Ctrl>Left", "win.previous"), ("<Ctrl>p", "win.show-player"),
                              ("<Ctrl>Up", "win.volume-up"), ("<Ctrl>Down", "win.volume-down"),
                              ("<Ctrl>question", "win.shortcuts")):
            existing = list(app.get_accels_for_action(action))
            if accel not in existing:
                app.set_accels_for_action(action, existing + [accel])

    def _on_favourites_changed(self):
        FAVOURITES["albums"] = self.session.fav_album_ids()
        FAVOURITES["artists"] = self.session.fav_artist_ids()
        FAVOURITES["songs"] = self.session.fav_song_ids()
        FAVOURITES["playlists"] = self.session.fav_playlist_paths()
        refresh_stars()

    def _on_ratings_changed(self):
        RATINGS["song"] = dict(self.session.ratings.get("song", {}))
        RATINGS["album"] = dict(self.session.ratings.get("album", {}))
        # the Albums page: in "Rating" order load it again; otherwise just redraw the
        # albums whose rating changed (the list's Rating column)
        albums = self.roots.get("albums")
        if albums is None:
            return
        if self.config.prefs.get("album_sort") == "rating":
            if albums.get_mapped():
                albums.refresh()
            else:
                albums.dirty = True
            return
        store, rated = albums.store, RATINGS["album"]
        for i in range(store.get_n_items()):
            obj = store.get_item(i)
            new = rated.get(obj.album.id, 0)
            if obj.album.rating != new:
                obj.album.rating = new
                store.items_changed(i, 1, 1)

    def _on_playlists_changed(self):
        PLAYLISTS[:] = [(p["id"], p["name"]) for p in self.session.my_playlists()]

    def _playlist_add(self, target: str):
        """"pid:kind:id" — pid "new" asks for a name and creates the playlist first."""
        pid, kind, item = target.split(":", 2)
        if pid == "new":
            def create(name):
                pl = self.session.create_playlist(name)
                if pl is not None:
                    self.session.add_to_playlist(pl["id"], kind, int(item))
            ask_name(self, "New Playlist", "", "Create", create)
        else:
            self.session.add_to_playlist(pid, kind, int(item))

    def _playlist_move(self, target: str, where: str):
        pid, index = target.rsplit(":", 1)
        pl = self.session.playlists.get(pid) if self.session.playlists else None
        if pl is not None:
            self.session.move_in_playlist(pid, int(index),
                                          0 if where == "top" else len(pl["songs"]) - 1)

    def rename_playlist(self, pid: str):
        pl = self.session.playlists.get(pid) if self.session.playlists else None
        if pl is not None:
            ask_name(self, "Rename Playlist", pl["name"], "Rename",
                     lambda name: self.session.rename_playlist(pid, name))

    def delete_playlist(self, pid: str):
        pl = self.session.playlists.get(pid) if self.session.playlists else None
        if pl is None:
            return
        dlg = Adw.AlertDialog(heading=f"Delete “{pl['name']}”?",
                              body="The playlist goes; the music stays in your library.")
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("delete", "Delete")
        dlg.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dlg.set_close_response("cancel")
        dlg.connect("response", lambda d, r: r == "delete" and
                    self.session.delete_playlist(pid))
        dlg.present(self)

    def _remove_favourite(self, key: str):
        from .favourites import fav_key
        fav = next((f for f in self.session.favourites if fav_key(f) == key), None)
        if fav is not None:
            self.session.remove_favourite(fav)

    def _play_favourite(self, key: str):
        from .favourites import fav_key
        fav = next((f for f in self.session.favourites if fav_key(f) == key), None)
        if fav is not None:
            self.session.open_favourite(fav)

    def _artist_of_album(self, albumid):
        ids = self.session.cache.album_artist_ids(albumid) if self.session.cache else []
        if ids:
            self.show_artist(ids[0])

    def _user_sync_start(self, full):
        self._user_sync = True
        self.session.sync(full=full, force=True)

    # ------------------------------------------------------------------
    # navigation
    # ------------------------------------------------------------------
    def _current_nav(self) -> Adw.NavigationView:
        return self.navs[self.view_stack.get_visible_child_name() or "albums"]

    def _visible_page(self):
        return self._current_nav().get_visible_page()

    def show_album(self, album):
        self.show_album_id(album.id if hasattr(album, "id") else int(album))

    def show_album_id(self, albumid: int):
        self._open_page(f"album-{albumid}", lambda: AlbumPage(self, albumid))

    def push_page(self, page: Adw.NavigationPage):
        """Open a page in the current section (genre, decade, playlist…)."""
        self._open_page(page.get_tag(), lambda: page)

    def _open_page(self, tag: str | None, make):
        """Push a page — or, when one with that tag is already on the way back (artist →
        album → that artist again), go back to it: a navigation view allows each tag
        once (pushing it twice was a critical warning, and two copies of a page)."""
        self._close_player()
        nav = self._current_nav()
        existing = nav.find_page(tag) if tag else None
        if existing is not None:
            if existing is not nav.get_visible_page():
                nav.pop_to_page(existing)
            return
        nav.push(make())

    def _on_track_changed(self):
        # Play counts / last-played moved on: refresh the Played pages when next shown
        # (the one on screen a moment later, once Kodi has counted the play).
        for name in ("recent-played", "most-played"):
            page = self.roots.get(name)
            if page is None:
                continue
            page.dirty = True
            if self.view_stack.get_visible_child_name() == name and \
                    self._current_nav().get_visible_page() is page:
                GLib.timeout_add_seconds(3, lambda p=page: (p.refresh(), False)[1])

    def show_artist(self, artistid: int):
        self._open_page(f"artist-{artistid}", lambda: ArtistPage(self, artistid))

    def show_lidarr_artist(self, item: dict):
        self._open_page(f"lidarr-{item.get('foreignArtistId', '')}",
                        lambda: LidarrArtistPage(self, item))

    def _update_lidarr_visibility(self):
        """The Lidarr section only shows once Lidarr is enabled in Preferences."""
        visible = self.config.lidarr.enabled
        self._lidarr_stack_page.set_visible(visible)
        self._sidebar_rows["lidarr"].set_visible(visible)
        if not visible and self.view_stack.get_visible_child_name() == "lidarr":
            self.view_stack.set_visible_child_name("recent")

    def focus_search(self):
        entry = self._visible_entry()
        if entry is not None:
            entry.grab_focus()
            entry.select_region(0, -1)

    def show_player(self):
        if not self.session.state.active:
            self.toast("Nothing is playing")
            return
        if self._player_dialog is None:
            self._player_dialog = PlayerDialog(self)
            self._player_dialog.connect("closed", self._on_player_closed)
            self._player_dialog.present(self)

    def _on_player_closed(self, *_):
        self._player_dialog = None

    def _close_player(self):
        if self._player_dialog is not None:
            self._player_dialog.close()

    # -- Kodi offline / Wake-on-LAN -------------------------------------------
    def _update_offline(self):
        s = self.session
        offline = bool(s.instance and s.props.offline)
        wake = self.lookup_action("wake")
        if wake is not None:
            wake.set_enabled(offline and s.can_wake())
        if not offline:
            self.offline_banner.set_revealed(False)
            return
        self.offline_banner.set_title(f"{GLib.markup_escape_text(s.instance.name)} is offline")
        # the MAC address is learnt while connected; a Kodi never reached yet needs it
        # typed in once
        self.offline_banner.set_button_label("Wake Up" if s.can_wake() else "Set Up Wake-Up")
        self.offline_banner.set_revealed(True)

    def _on_offline_banner(self):
        s = self.session
        if s.can_wake():
            s.wake()
            return
        self.show_preferences("kodi")
        if self._prefs is not None and s.instance is not None:
            self._prefs.edit_instance(s.instance)

    def show_preferences(self, page: str | None = None):
        if self._prefs is None:
            self._prefs = PreferencesDialog(self)
            self._prefs.connect("closed", lambda *_: setattr(self, "_prefs", None))
            self._prefs.present(self)
        if page:
            self._prefs.set_visible_page_name(page)

    def _on_sidebar_row(self, _lb, row):
        if self.view_stack.get_visible_child_name() == row.section:
            self.navs[row.section].pop_to_tag(row.section)
        self.view_stack.set_visible_child_name(row.section)
        if self.split.get_collapsed():
            self.split.set_show_sidebar(False)

    def _on_section_changed(self, *_):
        self._sync_bottom_bar()
        name = self.view_stack.get_visible_child_name()
        row = self._sidebar_rows.get(name)
        if row is not None and self.sidebar_list.get_selected_row() is not row:
            self.sidebar_list.select_row(row)
        self._sync_visible_page()

    def _sync_visible_page(self):
        page = self._visible_page()
        if page is not None and getattr(page, "dirty", False):
            page.refresh()
        if hasattr(page, "update_playing"):
            page.update_playing()

    # ------------------------------------------------------------------
    # instances
    # ------------------------------------------------------------------
    def switch_instance(self, iid: str | None, force: bool = False):
        if not iid:
            self.session.activate(None)
            return
        if not force and self.session.instance and self.session.instance.id == iid:
            return
        self.config.active_id = iid
        self.config.save()
        inst: Instance | None = next((i for i in self.config.instances if i.id == iid), None)
        for name, nav in self.navs.items():
            nav.pop_to_tag(name)
        act = self.lookup_action("instance")
        if act:
            act.set_state(GLib.Variant("s", iid))
        self.session.activate(inst)

    def _on_instance_changed(self):
        inst = self.session.instance
        self.top.set_visible_child_name("main" if self.config.instances else "welcome")
        name = inst.name if inst else "Tonearm"
        self.set_title(f"Tonearm — {name}" if inst else "Tonearm")
        self._update_sync_label()

    # ------------------------------------------------------------------
    # session events
    # ------------------------------------------------------------------
    def _on_library_changed(self, *_):
        for nav in self.navs.values():
            stack = nav.get_navigation_stack()
            for i in range(stack.get_n_items()):
                page = stack.get_item(i)
                if hasattr(page, "dirty"):
                    page.dirty = True
        self._sync_visible_page()
        self._update_sync_label()

    def _on_connected_changed(self):
        """Back online: pages still showing the "Offline — last known …" banner fetch
        again (the visible one now, the rest when shown)."""
        if not self.session.props.connected:
            return
        for nav in self.navs.values():
            stack = nav.get_navigation_stack()
            for i in range(stack.get_n_items()):
                page = stack.get_item(i)
                banner = getattr(page, "banner", None)
                if banner is not None and banner.get_revealed() and hasattr(page, "dirty"):
                    banner.set_revealed(False)
                    page.dirty = True
        self._sync_visible_page()

    def _on_player_changed(self):
        page = self._visible_page()
        if hasattr(page, "update_playing"):
            page.update_playing()

    def _on_syncing(self):
        syncing = self.session.props.syncing
        self.sync_spinner.set_visible(syncing)
        if syncing:
            self.sync_spinner.start()
            self.sync_label.set_label("Checking for changes…")
        else:
            self.sync_spinner.stop()
        self._update_sync_label()

    def _on_sync_progress(self, _s, stage, frac):
        text = f"Syncing {stage.lower()}" if frac >= 0 else stage
        self.sync_label.set_label(f"{text} · {int(frac * 100)}%" if frac >= 0 else text + "…")

    def _on_sync_finished(self, _s, result):
        user = self._user_sync
        self._user_sync = False
        if isinstance(result, SyncResult):
            msg = result.summary()
            if user or result.changed:
                self.toast(f"{msg} ({result.seconds:.1f} s)")
        self._update_sync_label()

    def _update_sync_label(self):
        s = self.session
        inst = s.instance
        self.instance_label.set_label(inst.name if inst else "No Kodi")
        for c in ("online", "offline", "busy"):
            self.status_dot.remove_css_class(c)
        if s.props.syncing:
            self.status_dot.add_css_class("busy")
            return  # the progress handler owns the text while syncing
        if not inst:
            self.sync_label.set_label("Add a Kodi instance")
            self.status_dot.add_css_class("offline")
            return
        if s.props.connected:
            self.status_dot.add_css_class("online")
            state = "Connected"
        else:
            self.status_dot.add_css_class("offline")
            state = "Offline"
        c = s.cache
        n = c.count("album") if c else 0
        self.sync_label.set_label(f"{state} · {n} albums" if n else state)

    def toast(self, message: str, details: str | None = None):
        """Short toast; with ``details`` it gets a button that shows the full text
        (toasts truncate long messages, especially on a phone)."""
        t = Adw.Toast(title=GLib.markup_escape_text(message), timeout=3 if not details else 8)
        if details:
            t.set_button_label("Details")
            t.connect("button-clicked", lambda *_: self._show_details(message, details))
        self.toasts.add_toast(t)

    def _show_details(self, title: str, details: str):
        dlg = Adw.AlertDialog(heading=title)
        label = Gtk.Label(label=details, wrap=True, selectable=True, xalign=0,
                          wrap_mode=Pango.WrapMode.WORD_CHAR)
        label.add_css_class("monospace")
        dlg.set_extra_child(label)
        dlg.add_response("close", "Close")
        dlg.present(self)

    # ------------------------------------------------------------------
    # background mode
    # ------------------------------------------------------------------
    def _on_close_request(self, *_):
        if self.config.prefs.get("background", True) and self.session.instance is not None:
            self.set_visible(False)
            self.session.set_foreground(False)
            self.app.enter_background()
            return True
        return False
