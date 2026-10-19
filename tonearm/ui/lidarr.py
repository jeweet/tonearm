"""Lidarr section, Lidarr artist info / add page."""
from __future__ import annotations

from gi.repository import Adw, Gio, GObject, Gtk, Pango

from ..lidarr import MONITOR_OPTIONS, album_progress, best_image
from ..util import run_async
from .widgets import (Cover, JumpView, Spinner, detail_scaffold, initial, plain_row, scrolled,
                      status_page)

TABS = [("artists", "Artists"), ("queue", "Queue"), ("upcoming", "Upcoming"), ("missing", "Missing")]


def _year(date: str | None) -> str:
    return (date or "")[:4]


def _row(title, subtitle="", activatable=False) -> Adw.ActionRow:
    r = plain_row(title=title or "", subtitle=subtitle or "", use_markup=False,
                      activatable=activatable)
    r.set_subtitle_lines(2)
    return r


def _badge(text: str, css: str = "dim-label") -> Gtk.Label:
    lbl = Gtk.Label(label=text, valign=Gtk.Align.CENTER)
    lbl.add_css_class("caption")
    lbl.add_css_class("badge")
    lbl.add_css_class(css)
    return lbl


def open_uri(widget, uri: str):
    try:
        Gtk.UriLauncher.new(uri).launch(widget.get_root(), None, None, None)
    except Exception:  # noqa: BLE001 — GTK < 4.10
        Gtk.show_uri(widget.get_root(), uri, 0)


class LidarrItem(GObject.Object):
    __gtype_name__ = "TonearmLidarrItem"

    def __init__(self, data: dict):
        super().__init__()
        self.data = data


class LidarrArtistRow(Gtk.Box):
    """Recycled row for the (possibly long) Lidarr artist list."""

    def __init__(self, session):
        super().__init__(spacing=12, margin_top=6, margin_bottom=6, margin_start=6, margin_end=6)
        self.session = session
        self.avatar = Adw.Avatar(size=40, show_initials=True)
        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER, hexpand=True)
        self.name = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)
        self.name.add_css_class("heading")
        self.sub = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)
        self.sub.add_css_class("caption")
        self.sub.add_css_class("dim-label")
        texts.append(self.name)
        texts.append(self.sub)
        self.badge = _badge("In Kodi", "success")
        for w in (self.avatar, texts, self.badge, Gtk.Image(icon_name="tonearm-go-next-symbolic")):
            self.append(w)
        self._ticket = None
        self._key = None

    def bind(self, a: dict):
        st = a.get("statistics") or {}
        have, total = int(st.get("trackFileCount") or 0), int(st.get("totalTrackCount") or 0)
        sub = f"{st.get('albumCount', 0)} albums · {have}/{total} tracks"
        if not a.get("monitored", True):
            sub += " · unmonitored"
        name = a.get("artistName") or ""
        self.name.set_label(name)
        self.sub.set_label(sub)
        self.avatar.set_text(name)
        self.avatar.set_custom_image(None)
        self.badge.set_visible(bool(self.session.kodi_artist_id(name)))
        art = self.session.artwork
        if art is not None:
            art.cancel(self._ticket)
            self._ticket = None
            key = self._key = best_image(a)
            if key:
                def done(tex, k=key):
                    if self._key == k and tex is not None:
                        self.avatar.set_custom_image(tex)
                self._ticket = art.load(key, "small", done)


class LidarrPage(Adw.NavigationPage):
    """Root page: Lidarr library, download queue, upcoming and missing albums."""

    def __init__(self, window):
        super().__init__(title="Lidarr", tag="lidarr")
        self.window = window
        self.session = window.session
        self.tab = "artists"
        self.dirty = True

        tv = Adw.ToolbarView()
        header = window.root_header()
        refresh = Gtk.Button(icon_name="tonearm-refresh-symbolic", tooltip_text="Refresh")
        refresh.connect("clicked", lambda *_: self.refresh(force=True))
        header.pack_end(refresh)
        tv.add_top_bar(header)

        tabs = Gtk.Box(halign=Gtk.Align.CENTER, margin_top=6, margin_bottom=6)
        tabs.add_css_class("linked")
        first = None
        for key, label in TABS:
            b = Gtk.ToggleButton(label=label, group=first, active=first is None)
            first = first or b
            # connect after the initial state is set: toggled must not fire during __init__
            b.connect("toggled", self._on_tab, key)
            tabs.append(b)
        self.tabs = tabs
        tv.add_top_bar(tabs)

        self.list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.list.add_css_class("boxed-list")
        self.list.connect("row-activated", self._on_row)
        self.spinner = Spinner(spinning=True, halign=Gtk.Align.CENTER, margin_top=24, size=32)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_top=6,
                      margin_bottom=24, margin_start=12, margin_end=12)
        hint = Gtk.Label(wrap=True, xalign=0,
                         label="Search finds artists that aren't in Kodi yet; open one to add it here.")
        hint.add_css_class("dim-label")
        hint.add_css_class("caption")
        self.hint = hint
        box.append(hint)
        box.append(self.spinner)
        box.append(self.list)
        content = scrolled(Adw.Clamp(maximum_size=760, child=box))

        unconfigured = status_page("tonearm-lidarr-symbolic", "Lidarr Isn't Set Up",
                                   "Connect Lidarr to add artists that are missing from Kodi.")
        btn = Gtk.Button(label="Open Preferences", halign=Gtk.Align.CENTER)
        btn.add_css_class("pill")
        btn.add_css_class("suggested-action")
        btn.connect("clicked", lambda *_: window.show_preferences("lidarr"))
        unconfigured.set_child(btn)

        # Artists can number in the thousands: a recycling ListView instead of a ListBox.
        self.artist_store = Gio.ListStore(item_type=LidarrItem)
        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", lambda f, li: li.set_child(LidarrArtistRow(self.session)))
        factory.connect("bind", lambda f, li: li.get_child().bind(li.get_item().data))
        self.artist_list = Gtk.ListView(model=Gtk.NoSelection(model=self.artist_store),
                                        factory=factory, single_click_activate=True)
        self.artist_list.add_css_class("navigation-sidebar")
        self.artist_list.connect("activate", lambda lv, pos: window.show_lidarr_artist(
            self.artist_store.get_item(pos).data))
        artists_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        artists_hint = Gtk.Label(wrap=True, xalign=0, margin_top=6, margin_start=18, margin_end=18,
                                 label="Search finds artists that aren't in Kodi yet; "
                                       "open one to add it here.")
        artists_hint.add_css_class("dim-label")
        artists_hint.add_css_class("caption")
        artists_box.append(artists_hint)
        artists_sw = scrolled(Adw.ClampScrollable(maximum_size=760, child=self.artist_list))
        artists_box.append(JumpView(artists_sw, self.artist_list))
        self.artists_box = artists_box

        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.stack.add_named(content, "content")
        self.stack.add_named(artists_box, "artists")
        self.stack.add_named(unconfigured, "setup")
        self.empty = status_page("tonearm-lidarr-symbolic", "Nothing Here")
        self.stack.add_named(self.empty, "empty")
        tv.set_content(self.stack)
        self.set_child(tv)
        self.connect("showing", lambda *_: self.dirty and self.refresh())
        self.session.connect("lidarr-changed", lambda *_: self._mark())

    def _mark(self):
        self.dirty = True
        if self.get_mapped():
            self.refresh()

    def _on_tab(self, button, key):
        if button.get_active():
            self.tab = key
            self.refresh()

    def _clear(self):
        while (row := self.list.get_first_child()) is not None:
            self.list.remove(row)

    def refresh(self, force: bool = False):
        """Show the saved list straight away, then fetch a fresh one from Lidarr."""
        self.dirty = False
        client = self.session.lidarr
        self.tabs.set_visible(client is not None)
        if client is None:
            self.stack.set_visible_child_name("setup")
            return
        self.stack.set_visible_child_name("content")
        self.hint.set_visible(self.tab == "artists")
        tab = self.tab
        sort = (lambda items: sorted(items, key=lambda a: (a.get("sortName") or a.get("artistName")
                                                           or "").lower())) if tab == "artists" else list
        cached = client.cached(tab)
        shown = cached is not None
        if shown:
            self._render(tab, sort(cached))
        else:
            self._clear()
        self.spinner.set_visible(True)
        fn = {
            "artists": lambda: client.artists(max_age=0),
            "queue": client.queue,
            "upcoming": client.upcoming,
            "missing": client.missing,
        }[tab]

        def done(items):
            if tab != self.tab:
                return
            self.spinner.set_visible(False)
            if shown and items == cached:
                return  # nothing new; keep the rows (and their loaded images)
            self._render(tab, sort(items or []))

        def failed(exc):
            if tab != self.tab:
                return
            self.spinner.set_visible(False)
            if shown:
                self.window.toast(f"Lidarr unreachable — showing the saved list ({exc})")
                return
            self.empty.set_title("Lidarr Unreachable")
            self.empty.set_description(str(exc))
            self.stack.set_visible_child_name("empty")

        run_async(fn, done=done, error=failed)

    def _render(self, tab, items):
        self._clear()
        if not items:
            self.empty.set_description("")
            self.empty.set_title({"artists": "No Artists in Lidarr", "queue": "Nothing Downloading",
                                  "upcoming": "No Upcoming Releases", "missing": "Nothing Missing"}[tab])
            self.stack.set_visible_child_name("empty")
            return
        if tab == "artists":
            self.artist_store.splice(0, self.artist_store.get_n_items(),
                                     [LidarrItem(a) for a in items])
            jump = self.artists_box.get_last_child()
            jump.set_index([initial(a.get("sortName") or a.get("artistName") or "") for a in items])
            self.stack.set_visible_child_name("artists")
            return
        self.stack.set_visible_child_name("content")
        for it in items:
            self.list.append(getattr(self, f"_row_{tab}")(it))

    # -- row builders ----------------------------------------------------
    def _row_queue(self, q):
        size, left = q.get("size") or 0, q.get("sizeleft") or 0
        pct = int(100 * (size - left) / size) if size else 0
        artist = (q.get("artist") or {}).get("artistName", "")
        album = (q.get("album") or {}).get("title") or q.get("title", "")
        row = _row(album, f"{artist} · {q.get('status', '').capitalize()}"
                   + (f" · {q['timeleft']} left" if q.get("timeleft") else ""))
        bar = Gtk.LevelBar(min_value=0, max_value=100, value=pct, valign=Gtk.Align.CENTER,
                           width_request=80)
        row.add_suffix(bar)
        row.add_suffix(_badge(f"{pct}%"))
        row.item = None
        return row

    def _album_row(self, al, activatable=True):
        artist = (al.get("artist") or {})
        row = _row(al.get("title"), f"{artist.get('artistName', '')} · {(al.get('releaseDate') or '')[:10]}",
                   activatable=activatable and bool(artist))
        row.item = artist or None
        if row.item:
            row.add_suffix(Gtk.Image(icon_name="tonearm-go-next-symbolic"))
        return row

    _row_upcoming = _album_row
    _row_missing = _album_row

    def _on_row(self, _lb, row):
        item = getattr(row, "item", None)
        if item:
            self.window.show_lidarr_artist(item)


class LidarrArtistPage(Adw.NavigationPage):
    """Info about an artist from Lidarr/MusicBrainz, with Add / manage actions."""

    def __init__(self, window, item: dict):
        self.window = window
        self.session = window.session
        self.item = dict(item)
        super().__init__(title=item.get("artistName") or "Artist",
                         tag=f"lidarr-{item.get('foreignArtistId', '')}")
        self.dirty = False
        self._options = {}

        box, _, self.backdrop = detail_scaffold(self, window=window)

        self.cover = Cover(200, icon="tonearm-artist-symbolic", round_=True, css="big-cover")
        self.name = Gtk.Label(wrap=True, justify=Gtk.Justification.CENTER)
        self.name.add_css_class("title-1")
        self.meta = Gtk.Label(wrap=True, justify=Gtk.Justification.CENTER)
        self.meta.add_css_class("dim-label")
        self.tags = Gtk.Label(wrap=True, justify=Gtk.Justification.CENTER)
        self.tags.add_css_class("caption")

        self.kodi_btn = Gtk.Button(halign=Gtk.Align.CENTER, visible=False,
                                   child=Adw.ButtonContent(label="Open in Kodi Library",
                                                           icon_name="tonearm-album-symbolic"))
        self.kodi_btn.add_css_class("pill")
        self.kodi_btn.connect("clicked", self._open_kodi)

        self.overview = Gtk.Label(wrap=True, xalign=0, lines=5, ellipsize=Pango.EllipsizeMode.END)
        self.more = Gtk.Button(label="Show More", halign=Gtk.Align.START, visible=False)
        self.more.add_css_class("flat")
        self.more.connect("clicked", self._toggle_overview)
        self.links = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, max_children_per_line=6,
                                 column_spacing=6, row_spacing=6)

        self.action_area = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        for w in (self.cover, self.name, self.meta, self.tags, self.kodi_btn, self.overview,
                  self.more, self.links, self.action_area):
            box.append(w)
        self._render_info()
        self._render_actions()
        self._reload()

    # -- info --------------------------------------------------------------
    def _render_info(self):
        it = self.item
        self.set_title(it.get("artistName") or "Artist")
        self.name.set_label(it.get("artistName") or "")
        bits = [x for x in (it.get("disambiguation"), it.get("artistType"),
                            (it.get("status") or "").capitalize()) if x]
        rating = (it.get("ratings") or {}).get("value")
        if rating:
            bits.append(f"★ {rating:.1f}")
        self.meta.set_label(" · ".join(bits))
        self.tags.set_label("  ·  ".join(it.get("genres") or []))
        self.tags.set_visible(bool(it.get("genres")))
        self.cover.set_art(self.session, best_image(it), "large")
        fanart = best_image(it, kinds=("fanart", "banner"))
        self.backdrop.set_art(self.session, fanart or best_image(it), soft=not fanart)
        text = (it.get("overview") or "").strip()
        self.overview.set_label(text or "No description available.")
        if not text:
            self.overview.add_css_class("dim-label")
        self.more.set_visible(len(text) > 300)
        while (c := self.links.get_first_child()) is not None:
            self.links.remove(c)
        for link in (it.get("links") or [])[:8]:
            url = link.get("url")
            if not url:
                continue
            b = Gtk.Button(label=(link.get("name") or "link").capitalize())
            b.add_css_class("pill")
            b.add_css_class("small")
            b.connect("clicked", lambda _b, u=url: open_uri(self, u))
            self.links.append(b)
        self.kodi_id = self.session.kodi_artist_id(it.get("artistName", ""))
        self.kodi_btn.set_visible(bool(self.kodi_id))

    def _toggle_overview(self, *_):
        expanded = self.overview.get_lines() == -1
        self.overview.set_lines(5 if expanded else -1)
        self.overview.set_ellipsize(Pango.EllipsizeMode.END if expanded else Pango.EllipsizeMode.NONE)
        self.more.set_label("Show More" if expanded else "Show Less")

    def _open_kodi(self, *_):
        if self.kodi_id:
            self.window.show_artist(self.kodi_id)

    def _reload(self):
        """Refresh from Lidarr: full lookup info + whether it's in the Lidarr library."""
        client = self.session.lidarr
        mbid = self.item.get("foreignArtistId")
        if client is None or not mbid:
            return

        def work():
            mine = client.artist_by_mbid(mbid)
            info = None
            if not self.item.get("overview") or mine is None:
                try:
                    info = client.lookup_mbid(mbid)
                except Exception:  # noqa: BLE001
                    info = None
            return mine, info

        def done(res):
            mine, info = res
            if info:
                self.item.update({k: v for k, v in info.items() if v})
            if mine:
                self.item.update(mine)
            else:
                self.item["id"] = 0
            self._render_info()
            self._render_actions()

        self.session.lidarr_run(work, done=done)

    # -- actions -----------------------------------------------------------
    def _clear_actions(self):
        while (c := self.action_area.get_first_child()) is not None:
            self.action_area.remove(c)

    def _render_actions(self):
        self._clear_actions()
        if self.session.lidarr is None:
            g = Adw.PreferencesGroup(description="Set up Lidarr in Preferences to add this artist.")
            self.action_area.append(g)
            return
        if self.item.get("id"):
            self._render_managed()
        else:
            self._render_add()

    def _combo(self, title, options, current):
        """options: list of (value, label)."""
        model = Gtk.StringList.new([lbl for _, lbl in options])
        row = Adw.ComboRow(title=title, model=model)
        values = [v for v, _ in options]
        if current in values:
            row.set_selected(values.index(current))
        row.values = values
        return row

    def _render_add(self):
        st = self.session.config.lidarr
        g = Adw.PreferencesGroup(title="Add to Lidarr",
                                 description="This artist isn't in your Kodi library. Lidarr can "
                                             "download it; Kodi picks it up after its next scan.")
        monitor = self._combo("Monitor", MONITOR_OPTIONS, st.monitor)
        search = Adw.SwitchRow(title="Search for missing albums", active=st.search_on_add)
        quality = self._combo("Quality profile", [(st.quality_profile, "…")], st.quality_profile)
        root = self._combo("Root folder", [(st.root_folder, st.root_folder or "…")], st.root_folder)
        for r in (monitor, search, quality, root):
            g.add(r)
        self.action_area.append(g)

        client = self.session.lidarr

        def opts():
            return client.quality_profiles(), client.root_folders()

        def got(res):
            if not res:
                return
            qps, roots = res
            q_opts = [(p["id"], p["name"]) for p in qps]
            r_opts = [(r["path"], r["path"]) for r in roots]
            for row, options, cur in ((quality, q_opts, st.quality_profile),
                                      (root, r_opts, st.root_folder)):
                if not options:
                    continue
                row.set_model(Gtk.StringList.new([lbl for _, lbl in options]))
                row.values = [v for v, _ in options]
                row.set_selected(row.values.index(cur) if cur in row.values else 0)

        self.session.lidarr_run(opts, done=got)

        add = Gtk.Button(halign=Gtk.Align.CENTER,
                         child=Adw.ButtonContent(label="Add to Lidarr", icon_name="tonearm-add-symbolic"))
        add.add_css_class("pill")
        add.add_css_class("suggested-action")
        spinner = Spinner(visible=False, halign=Gtk.Align.CENTER)

        def do_add(*_):
            from dataclasses import replace
            settings = replace(
                st,
                quality_profile=quality.values[quality.get_selected()] if quality.values else 0,
                root_folder=root.values[root.get_selected()] if root.values else "",
                metadata_profile=st.metadata_profile,
            )
            mon = monitor.values[monitor.get_selected()]
            add.set_sensitive(False)
            spinner.set_visible(True)
            spinner.start()

            def work():
                if not settings.metadata_profile:
                    mps = client.metadata_profiles()
                    if mps:
                        settings.metadata_profile = mps[0]["id"]
                return client.add_artist(self.item, settings, monitor=mon, search=search.get_active())

            def done(res):
                spinner.stop()
                spinner.set_visible(False)
                add.set_sensitive(True)
                if res:
                    self.item.update(res)
                    self.window.toast(f"Added {self.item.get('artistName')} to Lidarr"
                                      + (" — searching for albums" if search.get_active() else ""))
                    self._render_actions()

            def failed(exc):
                spinner.stop()
                spinner.set_visible(False)
                add.set_sensitive(True)
                self.window.toast(f"Lidarr: {exc}")

            run_async(work, done=done, error=failed)

        add.connect("clicked", do_add)
        self.action_area.append(add)
        self.action_area.append(spinner)

    def _render_managed(self):
        it = self.item
        client = self.session.lidarr
        g = Adw.PreferencesGroup(title="In Lidarr")
        st = it.get("statistics") or {}
        status = _row("Library", f"{st.get('albumCount', 0)} albums · "
                                 f"{st.get('trackFileCount', 0)}/{st.get('totalTrackCount', 0)} tracks on disk")
        g.add(status)
        mon = Adw.SwitchRow(title="Monitored", subtitle="Lidarr grabs new and missing releases",
                            active=bool(it.get("monitored", True)))

        def on_mon(row, _p):
            want = row.get_active()
            if want == bool(self.item.get("monitored", True)):
                return

            def work():
                return client.set_monitored(client.artist(self.item["id"]), want)

            def done(res):
                if res:
                    self.item.update(res)

            self.session.lidarr_run(work, done=done)

        mon.connect("notify::active", on_mon)
        g.add(mon)
        self.action_area.append(g)

        buttons = Gtk.Box(spacing=12, halign=Gtk.Align.CENTER)
        search = Gtk.Button(child=Adw.ButtonContent(label="Search Missing",
                                                    icon_name="tonearm-search-symbolic"))
        search.add_css_class("pill")
        search.connect("clicked", lambda *_: self.session.lidarr_run(
            client.search_artist, it["id"],
            done=lambda r: self.window.toast("Lidarr is searching for missing albums")))
        web = Gtk.Button(child=Adw.ButtonContent(label="Open Lidarr", icon_name="tonearm-web-symbolic"))
        web.add_css_class("pill")
        web.connect("clicked", lambda *_: open_uri(self, f"{client.base}/artist/{it.get('foreignArtistId')}"))
        buttons.append(search)
        buttons.append(web)
        self.action_area.append(buttons)

        albums = Adw.PreferencesGroup(title="Albums")
        spinner = Spinner(spinning=True)
        albums.set_header_suffix(spinner)
        self.action_area.append(albums)

        def got(items):
            spinner.set_visible(False)
            for al in sorted(items or [], key=lambda a: a.get("releaseDate") or "", reverse=True):
                have, total = album_progress(al)
                r = _row(al.get("title"), " · ".join(x for x in (_year(al.get("releaseDate")),
                                                                 al.get("albumType")) if x))
                if not al.get("monitored"):
                    r.add_suffix(_badge("unmonitored"))
                done_ = total and have >= total
                r.add_suffix(_badge(f"{have}/{total}", "success" if done_ else "dim-label"))
                albums.add(r)

        self.session.lidarr_run(client.albums, it["id"], done=got)

    def refresh(self):
        self.dirty = False
        self._render_info()


__all__ = ["LidarrPage", "LidarrArtistPage", "TABS"]
