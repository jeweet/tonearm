"""Album and artist detail pages."""
from __future__ import annotations

from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango

from ..sorting import (ARTIST_ALBUM_DESCENDING, ARTIST_ALBUM_KEYS, ARTIST_ALBUM_SORTS,
                       sort_artist_albums)
from ..util import fmt_time, fmt_total, plural, run_async
from .albumview import prefs as album_prefs, register_other, view_menu, view_options_button
from .columns import Column, ColumnRow, ColumnSet, column_box
from .widgets import (LIST_WIDTH, TILE_SIZES, AlbumTile, Cover, RatingStars, Spinner, album_menu,
                      attach_context_menu, button_row, detail_scaffold, follow, pill, plain_row,
                      set_menu, song_menu)


class AlbumPage(Adw.NavigationPage):
    def __init__(self, window, albumid: int):
        self.window = window
        self.session = window.session
        self.albumid = albumid
        album = self.session.cache.album(albumid) if self.session.cache else None
        super().__init__(title=album.title if album else "Album", tag=f"album-{albumid}")
        self.dirty = False
        self.rows: dict[int, Adw.ActionRow] = {}
        self._fetched = False

        # as wide as the album list on artist pages (LIST_WIDTH)
        box, _, self.backdrop = detail_scaffold(self, album_menu(albumid), window,
                                                width=LIST_WIDTH)
        # the ⋯ menu says "Add to" or "Remove from Kodi Favourites": keep it current
        follow(self.session, "favourites-changed", self, lambda page: page._sync_fav())
        follow(self.session, "playlists-changed", self,
               lambda page: set_menu(page.menu_button, album_menu(page.albumid)))
        self.cover = Cover(240, css="big-cover")
        self.cover.set_margin_top(6)
        self.title = Gtk.Label(wrap=True, justify=Gtk.Justification.CENTER,
                               wrap_mode=Pango.WrapMode.WORD_CHAR)
        self.title.add_css_class("title-1")
        self.artist_btn = Gtk.Button(halign=Gtk.Align.CENTER)
        self.artist_btn.add_css_class("flat")
        self.artist_btn.add_css_class("artist-link")
        self.artist_btn.connect("clicked", self._go_artist)
        self.meta = Gtk.Label(wrap=True, justify=Gtk.Justification.CENTER)
        self.meta.add_css_class("dim-label")
        # Kodi's rating of the album (5 stars = 10)
        self.rating = RatingStars(lambda v: self.session.rate("album", albumid, v),
                                  halign=Gtk.Align.CENTER)
        follow(self.session, "ratings-changed", self, lambda page: page._sync_rating())
        self._sync_rating()

        buttons = Gtk.Box(spacing=8, halign=Gtk.Align.CENTER, margin_top=6, margin_bottom=6)
        play = pill("Play", "tonearm-play-symbolic", True)
        play.connect("clicked", lambda *_: self.session.play_album(albumid))
        shuffle = pill("Shuffle", "tonearm-shuffle-symbolic")
        shuffle.connect("clicked", lambda *_: self.session.play_album(albumid, shuffle=True))
        buttons.append(play)
        buttons.append(shuffle)
        # ★ Kodi Favourite on the cover, as on album tiles and the artist picture
        # (always shown here: a phone has no hover to reveal it)
        self.fav_star = Gtk.Button(icon_name="tonearm-starred-symbolic", halign=Gtk.Align.END,
                                   valign=Gtk.Align.START, margin_top=8, margin_end=8)
        self.fav_star.add_css_class("cover-star")
        self.fav_star.connect("clicked", lambda *_: self.session.toggle_album_favourite(albumid))
        self.cover.add_overlay(self.fav_star)
        self._sync_fav()

        self.tracks = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        self.spinner = Spinner(spinning=True, visible=False, margin_top=24, size=32)

        for w in (self.cover, self.title, self.artist_btn, self.meta, self.rating, buttons,
                  self.tracks, self.spinner):
            box.append(w)
        self.connect("showing", lambda *_: self.dirty and self.refresh())
        self.refresh()

    def _sync_fav(self):
        """Star and ⋯ menu say whether this album is a Kodi Favourite."""
        fav = self.albumid in self.session.fav_album_ids()
        (self.fav_star.add_css_class if fav else self.fav_star.remove_css_class)("on")
        self.fav_star.set_tooltip_text("Remove from Favorites" if fav else "Add to Favorites")
        set_menu(self.menu_button, album_menu(self.albumid))
        for sid, row in self.rows.items():
            self._sync_track_star(sid, row)

    def _sync_rating(self):
        self.rating.set_value(self.session.rating("album", self.albumid))
        set_menu(self.menu_button, album_menu(self.albumid))

    def _go_artist(self, *_):
        ids = self.session.cache.album_artist_ids(self.albumid) if self.session.cache else []
        if ids:
            self.window.show_artist(ids[0])

    def refresh(self):
        self.dirty = False
        cache = self.session.cache
        album = cache.album(self.albumid) if cache else None
        if album is None:
            self.title.set_label("Album not found")
            return
        self.set_title(album.title)
        self.title.set_label(album.title)
        self.artist_btn.set_label(album.artist or "Unknown Artist")
        self.cover.set_art(self.session, album.thumbnail, "large")
        # Backdrop: the album artist's fanart, else a blurred cover.
        fanart = cache.album_fanart(self.albumid)
        self.backdrop.set_art(self.session, fanart or album.thumbnail, soft=not fanart)
        if not fanart and not getattr(self, "_art_requested", False):
            # Not in the cache (older sync / Kodi gave none): ask Kodi for the artist's art once.
            self._art_requested = True
            ids = cache.album_artist_ids(self.albumid)
            if ids:
                self.session.fetch_artist_art(
                    ids[0], lambda art: art and art[1] and self.backdrop.set_art(self.session, art[1]))
        songs = cache.songs_for_album(self.albumid)
        if not songs and not self._fetched:
            self._fetched = True
            self.spinner.set_visible(True)

            def got(_songs):
                self.spinner.set_visible(False)
                if _songs:
                    self.refresh()

            self.session.fetch_album_songs(self.albumid, got)
        bits = []
        if album.year:
            bits.append(str(album.year))
        if album.genre:
            bits.append(album.genre)
        if songs:
            bits.append(f"{len(songs)} song{'s' if len(songs) != 1 else ''}")
            bits.append(fmt_total(sum(s.duration for s in songs)))
        self.meta.set_label(" · ".join(bits))
        self._build_tracks(album, songs)
        self.update_playing()

    def _build_tracks(self, album, songs):
        # file paths let file-based song Favorites light up their ★
        if songs and not getattr(self, "_files_requested", False):
            self._files_requested = True
            self.session.fetch_song_files(
                self.albumid, lambda _f: (self.window._on_favourites_changed(), self._sync_fav()))
        while (child := self.tracks.get_first_child()) is not None:
            self.tracks.remove(child)
        self.rows.clear()
        discs = sorted({s.disc for s in songs})
        multi = len(discs) > 1
        index = 0
        for disc in discs:
            group = Adw.PreferencesGroup(title=f"Disc {disc}" if multi else "")
            for s in [x for x in songs if x.disc == disc]:
                group.add(self._track_row(album, s, index))
                index += 1
            self.tracks.append(group)

    def _track_row(self, album, song, index) -> Adw.ActionRow:
        row = plain_row(title=song.title, activatable=True, use_markup=False)
        if song.artist and song.artist != album.artist:
            row.set_subtitle(song.artist)
        row.set_title_lines(2)
        num = Gtk.Stack(hhomogeneous=True, valign=Gtk.Align.CENTER)
        lbl = Gtk.Label(label=str(song.track) if song.track else "·", width_chars=3, xalign=1)
        lbl.add_css_class("dim-label")
        lbl.add_css_class("numeric")
        num.add_named(lbl, "num")
        num.add_named(Gtk.Image(icon_name="tonearm-play-symbolic"), "playing")
        row.add_prefix(num)
        row.num_stack = num
        dur = Gtk.Label(label=fmt_time(song.duration))
        dur.add_css_class("dim-label")
        dur.add_css_class("numeric")
        row.add_suffix(dur)
        # the ⋯ menu; for a Favorite track its icon is the ★ (the menu still opens, with
        # "Remove from Favorites" in it)
        more = Gtk.MenuButton(icon_name="tonearm-more-symbolic", valign=Gtk.Align.CENTER)
        more.add_css_class("flat")
        more.add_css_class("track-more")
        # built when opened, so "Add to" / "Remove from Favorites" is current
        more.set_create_popup_func(lambda b, sid=song.id: set_menu(b, song_menu(sid)))
        row.add_suffix(more)
        row.more = more
        row.connect("activated", lambda *_: self.session.play_album(self.albumid, index))
        attach_context_menu(row, lambda: song_menu(song.id))
        self.rows[song.id] = row
        self._sync_track_star(song.id, row)
        return row

    def _sync_track_star(self, songid, row):
        fav = songid in self.session.fav_song_ids()
        row.more.set_icon_name("tonearm-starred-symbolic" if fav else "tonearm-more-symbolic")
        (row.more.add_css_class if fav else row.more.remove_css_class)("fav")
        row.more.set_tooltip_text("Favorite · More" if fav else "More")

    def update_playing(self):
        current = self.session.state.songid if self.session.state.active else 0
        for sid, row in self.rows.items():
            playing = sid == current
            row.num_stack.set_visible_child_name("playing" if playing else "num")
            if playing:
                row.add_css_class("now-playing")
            else:
                row.remove_css_class("now-playing")



def artist_album_columns() -> ColumnSet:
    """An artist's albums (no header row, so the values say what they are)."""
    return ColumnSet([
        Column("genre", "Genre", 150, 4, lambda a: a.genre or ""),
        Column("year", "Year", 48, 1, lambda a: str(a.year) if a.year else "", xalign=1.0,
               numeric=True),
        Column("songs", "Songs", 76, 3, lambda a: plural(a.song_count, "song") if a.song_count
               else "", xalign=1.0, numeric=True),
        Column("duration", "Length", 80, 2, lambda a: fmt_total(a.duration) if a.duration else "",
               xalign=1.0, numeric=True),
    ], reserve=56 + 180 + 34 + 2 * 12 + 40 + 24)   # cover, title, ⋯, spacing, padding


class ArtistPage(Adw.NavigationPage):
    def __init__(self, window, artistid: int):
        self.window = window
        self.session = window.session
        self.artistid = artistid
        artist = self.session.cache.artist(artistid) if self.session.cache else None
        super().__init__(title=artist.name if artist else "Artist", tag=f"artist-{artistid}")
        self.dirty = False
        self._desc_requested = False

        box, header, self.backdrop = detail_scaffold(self, window=window, wide=True)
        self.avatar = Adw.Avatar(size=144, show_initials=True, halign=Gtk.Align.CENTER, margin_top=6)
        # WORD_CHAR everywhere here: a wrapping label is as wide as its longest word,
        # and one long name or a link in a biography made a phone's window wider than
        # its screen
        self.name = Gtk.Label(wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR,
                              justify=Gtk.Justification.CENTER)
        self.name.add_css_class("title-1")
        self.meta = Gtk.Label(wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR,
                              justify=Gtk.Justification.CENTER)
        self.meta.add_css_class("dim-label")

        # Lidarr (when it knows this artist): a round icon button beside Play / Shuffle,
        # as tall as them — with a "12/40" label the three didn't fit one line on a
        # phone; the tracks on disk are in its tooltip. It opens the artist in Lidarr.
        self._lidarr_item = None
        self.lidarr_btn = Gtk.Button(icon_name="tonearm-lidarr-symbolic", visible=False,
                                     valign=Gtk.Align.CENTER)
        self.lidarr_btn.add_css_class("circular")
        self.lidarr_btn.add_css_class("lidarr-round")
        self.lidarr_btn.connect("clicked", lambda *_: self._lidarr_item and
                                window.show_lidarr_artist(self._lidarr_item))

        # Play / Shuffle (/ Lidarr) wrap onto a second line rather than make a phone's
        # window wider than its screen
        buttons = button_row(margin_top=6, margin_bottom=18)
        play = pill("Play", "tonearm-play-symbolic", True)
        play.connect("clicked", lambda *_: self.session.play_artist(artistid))
        shuffle = pill("Shuffle", "tonearm-shuffle-symbolic")
        shuffle.connect("clicked", lambda *_: self.session.play_artist(artistid, shuffle=True))
        buttons.append(play)
        buttons.append(shuffle)
        buttons.append(self.lidarr_btn)
        # ★ Kodi Favourite
        # ★ on the artist's picture, as on album covers (always shown here: a phone has
        # no hover to reveal it)
        self.fav_star = Gtk.Button(icon_name="tonearm-starred-symbolic", halign=Gtk.Align.END,
                                   valign=Gtk.Align.START, margin_top=6, margin_end=6)
        self.fav_star.add_css_class("cover-star")
        self.fav_star.connect("clicked",
                              lambda *_: self.session.toggle_artist_favourite(self.artistid))
        avatar_box = Gtk.Overlay(child=self.avatar, halign=Gtk.Align.CENTER)
        avatar_box.add_overlay(self.fav_star)
        follow(self.session, "favourites-changed", self, lambda page: page._sync_fav())
        self._sync_fav()

        # the biography isn't on the page: a tap on the artist's picture shows it
        self._bio = ""
        tap = Gtk.GestureClick()
        tap.connect("released", lambda *_: self._show_bio())
        self.avatar.add_controller(tap)
        self.avatar.set_cursor(Gdk.Cursor.new_from_name("pointer"))

        # in the top bar, as on the Albums and Artists pages: click = grid / list, the
        # arrow = cover size, titles and the order of this artist's albums
        self._install_sort_actions()
        self.view_btn = view_options_button(window)
        self.view_btn.set_menu_model(self._view_menu())
        self.view_btn.set_dropdown_tooltip("View and Sort")
        header.pack_end(self.view_btn)
        register_other(self)
        self.album_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.album_list.add_css_class("boxed-list")
        self.album_list.connect("row-activated", lambda lb, row: window.show_album(row.album.id))
        self.album_columns = artist_album_columns()
        self.album_columns.guess(window)
        list_box = column_box(self.album_columns, vexpand=False)
        list_box.append(self.album_list)
        self._albums = []
        self.flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, valign=Gtk.Align.START, homogeneous=True,
                                min_children_per_line=1, max_children_per_line=8,
                                column_spacing=6, row_spacing=6, activate_on_single_click=True)
        self.flow.add_css_class("album-flow")
        self.flow.connect("child-activated",
                          lambda fb, child: window.show_album(child.get_child().album.id))

        self.album_stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE,
                                     vhomogeneous=False)
        self.album_stack.add_named(self.flow, "grid")    # the grid: the full width
        # the list as wide as an album page's track list
        self.album_stack.add_named(Adw.Clamp(maximum_size=LIST_WIDTH, child=list_box), "list")
        # Artist info stays a readable width; the albums below use the whole width.
        top = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for w in (avatar_box, self.name, self.meta, buttons):
            top.append(w)
        # room at the top so the artist's fanart shows above the picture and name:
        # about half the band (taller on bigger screens); only with real fanart
        self.fanart_space = Gtk.Box()
        self.fanart_gap = Gtk.Revealer(child=self.fanart_space, reveal_child=False,
                                       transition_type=Gtk.RevealerTransitionType.SLIDE_DOWN,
                                       transition_duration=300)
        self.backdrop.on_height(lambda h: self.fanart_space.set_size_request(-1, int(h * 0.5)))
        box.append(self.fanart_gap)
        box.append(Adw.Clamp(maximum_size=760, child=top))
        box.append(self.album_stack)
        self._update_view()
        self.connect("showing", lambda *_: self.dirty and self.refresh())
        self.refresh()

    # -- sort ------------------------------------------------------------
    def _install_sort_actions(self):
        p = self.window.config.prefs
        self.sort_key = p.get("artist_album_sort", "year")
        if self.sort_key not in ARTIST_ALBUM_KEYS:
            self.sort_key = "year"
        self.sort_reverse = bool(p.get("artist_album_sort_reverse", False))
        group = Gio.SimpleActionGroup()
        s = GLib.VariantType("s")
        self._sort_action = Gio.SimpleAction.new_stateful(
            "sort", s, GLib.Variant("s", self.sort_key))
        self._sort_action.connect("change-state", lambda a, v: self._set_sort(v.unpack(), False))
        self._dir_action = Gio.SimpleAction.new_stateful(
            "sort-direction", s, GLib.Variant("s", self._direction()))
        self._dir_action.connect("change-state", lambda a, v: self._set_sort(
            self.sort_key, (v.unpack() == "desc") != (self.sort_key in ARTIST_ALBUM_DESCENDING)))
        group.add_action(self._sort_action)
        group.add_action(self._dir_action)
        self.insert_action_group("artist", group)

    def _view_menu(self) -> Gio.Menu:
        menu = view_menu(False)     # cover size + titles
        sort = Gio.Menu()
        for key, label in ARTIST_ALBUM_SORTS:
            it = Gio.MenuItem.new(label, None)
            it.set_action_and_target_value("artist.sort", GLib.Variant("s", key))
            sort.append_item(it)
        menu.append_section("Sort", sort)
        direction = Gio.Menu()
        for value, label in (("asc", "Ascending"), ("desc", "Descending")):
            it = Gio.MenuItem.new(label, None)
            it.set_action_and_target_value("artist.sort-direction", GLib.Variant("s", value))
            direction.append_item(it)
        menu.append_section(None, direction)
        return menu

    def _direction(self) -> str:
        return "desc" if (self.sort_key in ARTIST_ALBUM_DESCENDING) != self.sort_reverse \
            else "asc"

    def _set_sort(self, key, reverse):
        self.sort_key, self.sort_reverse = key, bool(reverse)
        self._sort_action.set_state(GLib.Variant("s", key))
        self._dir_action.set_state(GLib.Variant("s", self._direction()))
        p = self.window.config.prefs
        p["artist_album_sort"], p["artist_album_sort_reverse"] = key, self.sort_reverse
        self.window.config.save()
        self._albums = sort_artist_albums(self._albums, key, self.sort_reverse)
        self._fill_list()
        self._fill_flow()

    # -- albums as grid or list -----------------------------------------
    def _update_view(self):
        p = album_prefs(self.window)
        as_list = p["album_view"] == "list"
        self.album_stack.set_visible_child_name("list" if as_list else "grid")

    def apply_album_prefs(self):
        """View options changed (here or on another page)."""
        self._update_view()
        self._fill_flow()

    def _fill_flow(self):
        p = album_prefs(self.window)
        size = TILE_SIZES.get(p["album_size"], TILE_SIZES["medium"])
        while (child := self.flow.get_first_child()) is not None:
            self.flow.remove(child)
        for a in self._albums:
            tile = AlbumTile(size, p["album_titles"])
            tile.bind(self.session, a, str(a.year) if a.year else "")
            attach_context_menu(tile, lambda a=a: album_menu(a.id, with_artist=False,
                                                             with_fav=False))
            self.flow.append(tile)

    def _fill_list(self):
        """List rows: columns as room allows, the rest in the subtitle."""
        while (row := self.album_list.get_first_child()) is not None:
            self.album_list.remove(row)
        for a in self._albums:
            cover = Cover(56)
            cover.set_art(self.session, a.thumbnail, "small")
            more = Gtk.MenuButton(icon_name="tonearm-more-symbolic", valign=Gtk.Align.CENTER)
            set_menu(more, album_menu(a.id, with_artist=False))
            more.add_css_class("flat")
            content = ColumnRow(self.album_columns, cover, more, margins=6)
            content.set_margin_start(12)     # a little air inside the boxed list
            content.set_margin_end(12)
            content.set_item(a.title, a)
            row = Gtk.ListBoxRow(child=content, activatable=True)
            row.album = a
            attach_context_menu(row, lambda a=a: album_menu(a.id, with_artist=False))
            self.album_list.append(row)

    def _sync_fav(self):
        fav = self.artistid in self.session.fav_artist_ids()
        (self.fav_star.add_css_class if fav else self.fav_star.remove_css_class)("on")
        self.fav_star.set_tooltip_text("Remove from Favorites" if fav else "Add to Favorites")

    def _set_desc(self, text):
        self._bio = (text or "").strip()
        self.avatar.set_tooltip_text("Biography" if self._bio else None)

    def _show_bio(self):
        """The artist's biography in a dialog (a tap on the picture)."""
        name = self.name.get_label()
        if not self._bio:
            self.window.toast(f"No biography of {name} in Kodi")
            return
        dlg = Adw.Dialog(title=name, content_width=560, content_height=640)
        tv = Adw.ToolbarView()
        tv.add_top_bar(Adw.HeaderBar())
        text = Gtk.Label(label=self._bio, wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR,
                         xalign=0, yalign=0)
        text.add_css_class("body")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, margin_top=6, margin_bottom=24,
                      margin_start=18, margin_end=18)
        box.append(text)
        tv.set_content(Gtk.ScrolledWindow(child=box, hscrollbar_policy=Gtk.PolicyType.NEVER,
                                          propagate_natural_height=True))
        dlg.set_child(tv)
        dlg.present(self.window)

    def refresh(self):
        self.dirty = False
        cache = self.session.cache
        artist = cache.artist(self.artistid) if cache else None
        if artist is None:
            self.name.set_label("Artist not found")
            return
        self.set_title(artist.name)
        self.name.set_label(artist.name)
        self.avatar.set_text(artist.name)
        if not artist.fanart and not getattr(self, "_art_requested", False):
            self._art_requested = True
            self.session.fetch_artist_art(self.artistid, self._got_fanart)
        self.backdrop.set_art(self.session, artist.fanart or artist.thumbnail,
                              soft=not artist.fanart)
        self.fanart_gap.set_reveal_child(bool(artist.fanart))
        if artist.thumbnail and self.session.artwork:
            self.session.artwork.load(artist.thumbnail, "large",
                                      lambda tex: tex and self.avatar.set_custom_image(tex))
        self._set_desc(artist.description)
        self.lidarr_btn.set_visible(False)
        if self.session.lidarr is not None:
            self.session.lidarr_artist_for_kodi(artist.name, self._on_lidarr)
        if not artist.description and not self._desc_requested:
            self._desc_requested = True
            self.session.fetch_artist_description(self.artistid, self._set_desc)

        def done(albums):
            self._albums = sort_artist_albums(albums, self.sort_key, self.sort_reverse)
            self._fill_list()
            self._fill_flow()
            n = len(albums)
            songs = sum(a.song_count for a in albums)
            # albums, years, genres first (as everywhere an artist is described)
            bits = [plural(n, 'album')]
            if artist.years:
                bits.append(artist.years)
            if artist.genre:
                bits.append(artist.genre)
            if songs:
                bits.append(f"{songs} songs")
                bits.append(fmt_total(sum(a.duration for a in albums)))
            self.meta.set_label(" · ".join(bits))

        run_async(cache.albums_for_artist, self.artistid, done=done)

    def _got_fanart(self, art):
        if art and art[1]:
            self.backdrop.set_art(self.session, art[1])
            self.fanart_gap.set_reveal_child(True)

    def _on_lidarr(self, item):
        self._lidarr_item = item
        if not item:
            return
        st = item.get("statistics") or {}
        have, total = st.get("trackFileCount", 0), st.get("totalTrackCount", 0)
        mon = "monitored" if item.get("monitored", True) else "not monitored"
        self.lidarr_btn.set_tooltip_text(
            f"Open in Lidarr — {have} of {total} tracks on disk, {mon}")
        self.lidarr_btn.set_visible(True)

    def update_playing(self):
        pass
