"""Now-playing bar and the full player (a bottom sheet on phones)."""
from __future__ import annotations

import time

from gi.repository import Adw, Gdk, Gio, GLib, GObject, Graphene, Gtk, Pango

from ..sleeptimer import CHOICES as SLEEP_CHOICES
from ..util import fmt_time
from .widgets import Cover, RatingStars, plain_row

REPEAT_ICON = {"off": "tonearm-consecutive-symbolic",
               "all": "tonearm-repeat-symbolic",
               "one": "tonearm-repeat-one-symbolic"}
REPEAT_TIP = {"off": "Repeat: Off", "all": "Repeat: All", "one": "Repeat: One"}


def star_state(button, fav):
    """★ button for the playing track: hidden without a library track (fav None)."""
    button.set_visible(fav is not None)
    if fav:
        button.add_css_class("on")
    else:
        button.remove_css_class("on")
    button.set_tooltip_text("Remove from Favorites" if fav else "Add to Favorites")


def control(icon: str, tooltip: str, kind: str = "ctl", toggle: bool = False) -> Gtk.Button:
    """A player control, the same in the player bar and the player: ``kind="play"`` is
    the round accent-filled button of the Jump Back In cards; the rest are plain round
    icons that light up on hover. Their sizes come from where they sit (style.css)."""
    cls = Gtk.ToggleButton if toggle else Gtk.Button
    b = cls(icon_name=icon, tooltip_text=tooltip, valign=Gtk.Align.CENTER,
            halign=Gtk.Align.CENTER)
    b.add_css_class("np-play" if kind == "play" else "np-ctl")
    return b


class GlassBin(Adw.Bin):
    """Frosted glass: draws the page behind it, blurred, then its (translucent) child on
    top. GTK's CSS has no backdrop-filter, so the page is drawn again through a
    WidgetPaintable — only the strip under this widget, blurred."""

    __gtype_name__ = "TonearmGlassBin"
    BLUR = 50.0

    def __init__(self, **kw):
        super().__init__(**kw)
        # an opaque base (style.css .glass) under the blurred page: where the page ends
        # (the window's bottom edge) the blur fades into the bar's colour, never into
        # see-through
        self.add_css_class("glass")
        self._source = None
        self._paintable = None
        # the blur reaches past our edges (see do_snapshot): clip it to the bar
        self.set_overflow(Gtk.Overflow.HIDDEN)

    def set_source(self, widget: Gtk.Widget):
        """``widget``: what lies behind (the page area, which extends under the bar)."""
        self._source = widget
        self._paintable = Gtk.WidgetPaintable.new(widget)
        self._paintable.connect("invalidate-contents", lambda *_: self.queue_draw())

    def do_snapshot(self, snapshot):
        w, h = self.get_width(), self.get_height()
        src = self._source
        if src is not None and w > 0 and h > 0 and src.get_mapped():
            ok, pt = src.compute_point(self, Graphene.Point().init(0, 0))
            if ok:
                # Blur a piece of the page larger than the bar by twice the radius on
                # every side: clipping to just the bar first left the blur nothing to
                # sample past the edges, so the top and bottom faded to transparent.
                m = self.BLUR * 2
                snapshot.push_blur(self.BLUR)
                snapshot.push_clip(Graphene.Rect().init(-m, -m, w + 2 * m, h + 2 * m))
                snapshot.translate(pt)
                self._paintable.snapshot(snapshot, src.get_width(), src.get_height())
                snapshot.pop()
                snapshot.pop()
        child = self.get_child()
        if child is not None:
            self.snapshot_child(child, snapshot)


class NowPlayingBar(Gtk.Revealer):
    def __init__(self, window):
        super().__init__(transition_type=Gtk.RevealerTransitionType.SLIDE_UP)
        self.window = window
        self.session = window.session

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        outer.add_css_class("now-playing-bar")
        outer.add_css_class("np-controls")
        self.outer = outer
        # Phones: a thin full-width line at the bottom. Wider windows: elapsed ━━━━━ total.
        self.progress = Gtk.ProgressBar(hexpand=True, valign=Gtk.Align.CENTER)
        self.progress.add_css_class("np-progress")
        self.pos_label = Gtk.Label(xalign=1, width_chars=5)
        self.dur_label = Gtk.Label(xalign=0, width_chars=5)
        for lbl in (self.pos_label, self.dur_label):
            lbl.add_css_class("caption")
            lbl.add_css_class("numeric")
            lbl.add_css_class("dim-label")
        self.progress_row = Gtk.Box(spacing=8)
        self.progress_row.append(self.pos_label)
        self.progress_row.append(self.progress)
        self.progress_row.append(self.dur_label)
        self._compact = None

        row = Gtk.Box(spacing=10, margin_start=10, margin_end=6, margin_top=6, margin_bottom=6)
        self.cover = Cover(44, icon="tonearm-song-symbolic")
        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER, hexpand=True)
        self.title = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)
        self.title.add_css_class("heading")
        self.artist = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)
        self.artist.add_css_class("dim-label")
        self.artist.add_css_class("caption")
        texts.append(self.title)
        texts.append(self.artist)

        self.prev_btn = control("tonearm-previous-symbolic", "Previous")
        self.prev_btn.connect("clicked", lambda *_: self.session.previous())
        self.play_btn = control("tonearm-play-symbolic", "Play/Pause", "play")
        self.play_btn.connect("clicked", lambda *_: self.session.play_pause())
        self.next_btn = control("tonearm-next-symbolic", "Next")
        self.next_btn.connect("clicked", lambda *_: self.session.next())

        # ★ the playing track as a Favorite
        self.star = Gtk.Button(icon_name="tonearm-starred-symbolic", valign=Gtk.Align.CENTER,
                               tooltip_text="Add to Favorites")
        self.star.add_css_class("flat")
        self.star.add_css_class("np-star")
        self.star.connect("clicked", lambda *_: self.session.state.songid and
                          self.window.activate_action("win.fav-song",
                                                      GLib.Variant("i", self.session.state.songid)))
        self.session.connect("favourites-changed", lambda *_: self._sync_star())
        for w in (self.cover, texts, self.star, self.prev_btn, self.play_btn, self.next_btn):
            row.append(w)
        outer.append(row)
        outer.append(self.progress_row)   # wide windows: along the bottom (phones: top)
        self.row = row

        click = Gtk.GestureClick()
        click.connect("released", lambda *_: window.show_player())
        texts.add_controller(click)
        click2 = Gtk.GestureClick()
        click2.connect("released", lambda *_: window.show_player())
        self.cover.add_controller(click2)
        # swipe sideways to skip, up to open the player — touch only, and only after a
        # real stroke: a quick click on a button (play / pause) can measure as a fast
        # "swipe" of a few pixels, which skipped to the next track
        swipe = Gtk.GestureSwipe(touch_only=True)
        swipe.connect("swipe", self._on_swipe)
        row.add_controller(swipe)
        drag = Gtk.GestureDrag(touch_only=True)
        drag.connect("drag-begin", lambda *_: setattr(self, "_drag", (0.0, 0.0)))
        drag.connect("drag-update", lambda g, dx, dy: setattr(self, "_drag", (dx, dy)))
        row.add_controller(drag)
        self._drag = (0.0, 0.0)

        # glass: the page shows through, blurred (window.py gives it the page to show)
        self.glass = GlassBin(child=outer)
        self.set_child(self.glass)
        self.session.connect("player-changed", lambda *_: self.update())
        self.session.connect("track-changed", lambda *_: self.update(track=True))
        GLib.timeout_add(1000, self._tick)

    SWIPE_MIN = 48     # px the finger must travel for a swipe to count

    def _on_swipe(self, gesture, vx, vy):
        dx, dy = self._drag
        self._drag = (0.0, 0.0)
        if vy < -400 and abs(vy) > abs(vx) and -dy >= self.SWIPE_MIN:
            self.window.show_player()
        elif abs(vx) > 600 and abs(vx) > abs(vy) and abs(dx) >= self.SWIPE_MIN:
            (self.session.next if vx < 0 else self.session.previous)()

    def set_compact(self, compact: bool):
        if compact == self._compact:
            return
        self._compact = compact
        self.prev_btn.set_visible(not compact)
        self.pos_label.set_visible(not compact)
        self.dur_label.set_visible(not compact)
        for side in ("start", "end"):
            getattr(self.progress_row, f"set_margin_{side}")(0 if compact else 12)
        self.progress_row.set_margin_bottom(0 if compact else 6)
        # phones: a thin line along the top edge, above the track; wider: under it
        if compact:
            self.outer.reorder_child_after(self.progress_row, None)
        else:
            self.outer.reorder_child_after(self.progress_row, self.row)
        if compact:
            self.outer.remove_css_class("wide")
        else:
            self.outer.add_css_class("wide")
        self._show_progress()

    def _show_progress(self):
        st = self.session.state
        if not st.duration:
            self.progress.set_fraction(0)
            self.pos_label.set_label("")
            self.dur_label.set_label("")
            return
        now = st.now()
        self.progress.set_fraction(min(1.0, now / st.duration))
        if not self._compact:
            self.pos_label.set_label(fmt_time(now))
            self.dur_label.set_label(fmt_time(st.duration))

    def update(self, track=False):
        st = self.session.state
        self.set_reveal_child(st.active)
        if not st.active:
            return
        self.title.set_label(st.title or "Unknown")
        self.artist.set_label(st.artist)
        self.play_btn.set_icon_name("tonearm-pause-symbolic" if st.playing
                                    else "tonearm-play-symbolic")
        if track or self.cover._key != st.thumbnail:
            self.cover.set_art(self.session, st.thumbnail, "small")
        if track and st.albumid:   # file-based song Favorites need the album's file paths
            self.session.fetch_song_files(st.albumid, lambda _f: self._sync_star())
        self._sync_star()
        self._show_progress()

    def _sync_star(self):
        star_state(self.star, self.session.state.songid in self.session.fav_song_ids()
                   if self.session.state.songid else None)

    def _tick(self):
        st = self.session.state
        if st.active and st.playing and st.duration and self.get_mapped():
            self._show_progress()
        return GLib.SOURCE_CONTINUE


class PlayerDialog(Adw.Dialog):
    def __init__(self, window):
        # (a minimum size: required by its breakpoint — a phone is 360 px wide)
        super().__init__(title="Now Playing", content_width=440, content_height=760,
                         width_request=360, height_request=300)
        self.window = window
        self.session = window.session
        self._seek_hold_until = 0.0
        self._seek_id = 0
        self._vol_id = 0
        self._handlers = []

        tv = Adw.ToolbarView()
        header = Adw.HeaderBar()
        self.queue_btn = Gtk.ToggleButton(icon_name="tonearm-list-symbolic", tooltip_text="Queue")
        self.queue_btn.connect("toggled", self._toggle_queue)
        header.pack_end(self.queue_btn)
        # the title, with "Party Mode" under it while Kodi's Party Mode runs
        title = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2,
                        valign=Gtk.Align.CENTER)
        name = Gtk.Label(label="Now Playing", ellipsize=Pango.EllipsizeMode.END)
        name.add_css_class("title")
        self.party_badge = Gtk.Label(label="Party Mode", visible=False, halign=Gtk.Align.CENTER,
                                     tooltip_text="Kodi keeps adding random songs")
        self.party_badge.add_css_class("party-badge")
        title.append(name)
        title.append(self.party_badge)
        header.set_title_widget(title)
        # sleep timer: stop after some minutes, this track or this album
        sleep_menu = Gio.Menu()
        section = Gio.Menu()
        for key, label in SLEEP_CHOICES:
            if key == "track":
                sleep_menu.append_section(None, section)
                section = Gio.Menu()
            it = Gio.MenuItem.new(label, None)
            it.set_action_and_target_value("win.sleep", GLib.Variant("s", key))
            section.append_item(it)
        sleep_menu.append_section(None, section)
        self.sleep_btn = Gtk.MenuButton(icon_name="tonearm-sleep-symbolic", menu_model=sleep_menu,
                                        tooltip_text="Sleep Timer")
        header.pack_end(self.sleep_btn)
        self.star = Gtk.Button(icon_name="tonearm-starred-symbolic", tooltip_text="Add to Favorites")
        self.star.add_css_class("flat")
        self.star.add_css_class("np-star")
        self.star.connect("clicked", lambda *_: self.session.state.songid and
                          window.activate_action("win.fav-song",
                                                 GLib.Variant("i", self.session.state.songid)))
        header.pack_start(self.star)

        tv.add_top_bar(header)

        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE, vexpand=True)
        self.stack.add_named(self._build_player(), "player")
        self.stack.add_named(self._build_queue(), "queue")
        tv.set_content(self.stack)
        self.set_child(tv)
        self._compact_breakpoint()

        for sig, cb in (("player-changed", lambda *_: self.update()),
                        ("track-changed", lambda *_: self.update(track=True)),
                        ("queue-changed", lambda *_: self._load_queue()),
                        ("favourites-changed", lambda *_: self.update()),
                        ("ratings-changed", lambda *_: self.update())):
            self._handlers.append(self.session.connect(sig, cb))
        self._tick_id = GLib.timeout_add(250, self._tick)
        self.connect("closed", self._on_closed)
        self.update(track=True)

    def _on_closed(self, *_):
        for h in self._handlers:
            self.session.disconnect(h)
        self._handlers = []
        if self._tick_id:
            GLib.source_remove(self._tick_id)
            self._tick_id = 0
        if self._format_id:
            GLib.source_remove(self._format_id)
            self._format_id = 0

    # -- player view -----------------------------------------------------
    def _build_player(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10,
                      margin_start=24, margin_end=24, margin_bottom=24, valign=Gtk.Align.CENTER)
        self.cover = Cover(280, icon="tonearm-song-symbolic", css="big-cover")
        self.cover.set_margin_bottom(12)
        self.title = Gtk.Label(wrap=True, justify=Gtk.Justification.CENTER, lines=2,
                               ellipsize=Pango.EllipsizeMode.END)
        self.title.add_css_class("title-2")
        # labels that ellipsize: a long artist or album name must not widen the dialog
        self.artist_btn = Gtk.Button(halign=Gtk.Align.CENTER,
                                     child=Gtk.Label(ellipsize=Pango.EllipsizeMode.END))
        self.artist_btn.add_css_class("flat")
        self.artist_btn.add_css_class("artist-link")
        self.artist_btn.connect("clicked", self._go_artist)
        self.album_btn = Gtk.Button(halign=Gtk.Align.CENTER,
                                    child=Gtk.Label(ellipsize=Pango.EllipsizeMode.END))
        self.album_btn.add_css_class("flat")
        self.album_btn.add_css_class("dim-label")
        self.album_btn.connect("clicked", self._go_album)

        # progress as in the player bar (elapsed ━━━━ total, a slim rounded line), but
        # draggable to seek: the knob shows while hovered or dragged
        self.scale = Gtk.Scale(orientation=Gtk.Orientation.HORIZONTAL, draw_value=False,
                               hexpand=True, valign=Gtk.Align.CENTER)
        self.scale.add_css_class("np-seek")
        self.scale.set_range(0, 1)
        self.scale.connect("change-value", self._on_seek)
        self.pos_label = Gtk.Label(xalign=1, width_chars=5)
        self.dur_label = Gtk.Label(xalign=0, width_chars=5)
        for lbl in (self.pos_label, self.dur_label):
            lbl.add_css_class("dim-label")
            lbl.add_css_class("numeric")
            lbl.add_css_class("caption")
        times = Gtk.Box(spacing=8, margin_top=6)
        times.append(self.pos_label)
        times.append(self.scale)
        times.append(self.dur_label)

        # the player bar's controls, bigger (style.css .player-controls)
        # (six buttons: 12 px apart they fit the 440 px dialog; a phone's breakpoint
        # makes them smaller and closer)
        controls = Gtk.Box(spacing=12, halign=Gtk.Align.CENTER, margin_top=14, margin_bottom=8)
        controls.add_css_class("np-controls")
        controls.add_css_class("player-controls")
        self.shuffle_btn = control("tonearm-shuffle-symbolic", "Shuffle", toggle=True)
        self.shuffle_btn.add_css_class("side")
        self._shuffle_hid = self.shuffle_btn.connect(
            "toggled", lambda b: self.session.set_shuffle(b.get_active()))
        prev = control("tonearm-previous-symbolic", "Previous")
        prev.connect("clicked", lambda *_: self.session.previous())
        self.play_btn = control("tonearm-play-symbolic", "Play/Pause", "play")
        self.play_btn.connect("clicked", lambda *_: self.session.play_pause())
        # Stop, right of Play / Pause: Kodi stops, the queue stays
        self.stop_btn = control("tonearm-stop-symbolic", "Stop")
        self.stop_btn.connect("clicked", lambda *_: self.session.stop())
        nxt = control("tonearm-next-symbolic", "Next")
        nxt.connect("clicked", lambda *_: self.session.next())
        self.repeat_btn = control(REPEAT_ICON["off"], "Repeat")
        self.repeat_btn.add_css_class("side")
        self.repeat_btn.connect("clicked", lambda *_: self.session.cycle_repeat())
        for w in (self.shuffle_btn, prev, self.play_btn, self.stop_btn, nxt, self.repeat_btn):
            controls.append(w)

        vol = Gtk.Box(spacing=6, margin_top=12)
        self.mute_btn = Gtk.Button(icon_name="tonearm-volume-symbolic", tooltip_text="Mute")
        self.mute_btn.add_css_class("flat")
        self.mute_btn.connect("clicked", lambda *_: self.session.toggle_mute())
        self.vol_scale = Gtk.Scale(orientation=Gtk.Orientation.HORIZONTAL, draw_value=False, hexpand=True)
        self.vol_scale.set_range(0, 100)
        self.vol_scale.set_increments(5, 10)
        self.vol_scale.connect("change-value", self._on_volume)
        vol.append(self.mute_btn)
        vol.append(self.vol_scale)

        # "Sleep in 23 min" while the sleep timer runs
        self.sleep_label = Gtk.Label(visible=False, margin_top=6)
        self.sleep_label.add_css_class("dim-label")
        self.sleep_label.add_css_class("caption")

        # the stream's format: "FLAC · 24-bit / 96 kHz"
        self.format_label = Gtk.Label(visible=False, ellipsize=Pango.EllipsizeMode.END,
                                      margin_top=2)
        self.format_label.add_css_class("dim-label")
        self.format_label.add_css_class("caption")
        self.format_label.add_css_class("numeric")
        self._format_id = 0

        # Kodi's rating of the song (5 stars = 10)
        self.rating = RatingStars(lambda v: self.session.state.songid and self.session.rate(
            "song", self.session.state.songid, v), halign=Gtk.Align.CENTER)

        for w in (self.cover, self.title, self.artist_btn, self.album_btn, self.format_label,
                  self.rating, times, controls, vol, self.sleep_label):
            box.append(w)
        clamp = Adw.Clamp(maximum_size=420, child=box)
        self._player_box, self._controls = box, controls
        return Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, child=clamp)

    def _compact_breakpoint(self):
        """Phone: the dialog is a bottom sheet as wide as the screen (360 px and up).
        The roomy controls (≈ 400 px) and margins would make it wider than that, so
        narrower than 420 px: tighter spacing, smaller buttons and cover."""
        if not hasattr(self, "add_breakpoint"):     # libadwaita < 1.5
            return
        bp = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 420px"))
        box, controls = self._player_box, self._controls
        bp.add_setter(controls, "spacing", 8)
        bp.add_setter(box, "margin-start", 12)
        bp.add_setter(box, "margin-end", 12)
        bp.add_setter(self.cover, "width-request", 220)
        bp.add_setter(self.cover, "height-request", 220)
        bp.connect("apply", lambda *_: controls.add_css_class("compact"))
        bp.connect("unapply", lambda *_: controls.remove_css_class("compact"))
        self.add_breakpoint(bp)

    def _go_artist(self, *_):
        ids = self.session.state.artistid
        if ids:
            self.close()
            self.window.show_artist(ids[0])

    def _go_album(self, *_):
        if self.session.state.albumid:
            self.close()
            self.window.show_album(self.session.state.albumid)

    def _on_seek(self, scale, scroll, value):
        st = self.session.state
        if not st.duration:
            return False
        self._seek_hold_until = time.monotonic() + 1.5
        target = max(0.0, min(1.0, value)) * st.duration
        self.pos_label.set_label(fmt_time(target))
        if self._seek_id:
            GLib.source_remove(self._seek_id)
        self._seek_id = GLib.timeout_add(250, self._do_seek, target)
        return False

    def _do_seek(self, target):
        self._seek_id = 0
        self.session.seek(target)
        return GLib.SOURCE_REMOVE

    def _on_volume(self, scale, scroll, value):
        if self._vol_id:
            GLib.source_remove(self._vol_id)
        self._vol_id = GLib.timeout_add(120, self._do_volume, value)
        return False

    def _do_volume(self, value):
        self._vol_id = 0
        self.session.set_volume(int(value))
        return GLib.SOURCE_REMOVE

    def update(self, track=False):
        st = self.session.state
        self.title.set_label(st.title or ("Nothing playing" if not st.active else "Unknown"))
        self.artist_btn.get_child().set_label(st.artist or " ")
        self.artist_btn.set_tooltip_text(st.artist or None)
        self.artist_btn.set_sensitive(bool(st.artistid))
        self.album_btn.get_child().set_label(st.album or " ")
        self.album_btn.set_tooltip_text(st.album or None)
        self.album_btn.set_sensitive(bool(st.albumid))
        self.play_btn.set_icon_name("tonearm-pause-symbolic" if st.playing
                                    else "tonearm-play-symbolic")
        self.shuffle_btn.handler_block(self._shuffle_hid)
        self.shuffle_btn.set_active(st.shuffle)
        self.shuffle_btn.handler_unblock(self._shuffle_hid)
        self.repeat_btn.set_icon_name(REPEAT_ICON.get(st.repeat, REPEAT_ICON["off"]))
        (self.repeat_btn.add_css_class if st.repeat != "off" else
         self.repeat_btn.remove_css_class)("on")
        self.repeat_btn.set_tooltip_text(REPEAT_TIP.get(st.repeat, ""))
        if st.repeat != "off":
            self.repeat_btn.add_css_class("accent")
        else:
            self.repeat_btn.remove_css_class("accent")
        if not self._vol_id:
            self.vol_scale.set_value(st.volume)
        self.mute_btn.set_icon_name("tonearm-volume-muted-symbolic" if st.muted or st.volume == 0
                                    else "tonearm-volume-symbolic")
        if track or self.cover._key != st.thumbnail:
            self.cover.set_art(self.session, st.thumbnail, "large")
        if track and self.stack.get_visible_child_name() == "queue":
            self._load_queue()
        if track:
            self._fetch_format(400 if self.format_label.get_visible() else 0)
        star_state(self.star, st.songid in self.session.fav_song_ids() if st.songid else None)
        self.stop_btn.set_sensitive(st.active)
        self.rating.set_visible(bool(st.songid))
        party = st.active and st.partymode
        self.party_badge.set_visible(party)
        self.party_box.set_visible(party)
        self.rating.set_value(self.session.rating("song", st.songid) if st.songid else 0)
        self._tick()

    def _fetch_format(self, delay: int, retries: int = 2):
        """Ask Kodi for the stream's format — again a moment later while it's empty:
        right after a track starts Kodi hasn't read it yet."""
        if self._format_id:
            GLib.source_remove(self._format_id)

        def show(text):
            if not self._handlers:            # closed meanwhile
                return
            self.format_label.set_label(text)
            self.format_label.set_visible(bool(text))
            if not text and retries and self.session.state.active:
                self._fetch_format(1500, retries - 1)

        def go():
            self._format_id = 0
            if self.session.state.active:
                self.session.fetch_stream_info(show)
            else:
                show("")
            return GLib.SOURCE_REMOVE

        self._format_id = GLib.timeout_add(max(1, delay), go)

    def _tick(self):
        st = self.session.state
        if time.monotonic() >= self._seek_hold_until:
            now = st.now()
            self.scale.set_value(now / st.duration if st.duration else 0)
            self.pos_label.set_label(fmt_time(now))
        self.dur_label.set_label(fmt_time(st.duration))
        self.scale.set_sensitive(st.active and st.duration > 0)
        sleep = self.session.sleep
        text = sleep.label(time.monotonic()) if sleep.active else ""
        self.sleep_label.set_visible(bool(text))
        if text:
            self.sleep_label.set_label(text)
        (self.sleep_btn.add_css_class if sleep.active else self.sleep_btn.remove_css_class)("accent")
        return GLib.SOURCE_CONTINUE

    # -- queue view ------------------------------------------------------
    def _build_queue(self):
        self.queue_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.queue_list.add_css_class("boxed-list")
        self.queue_list.connect("row-activated", lambda lb, row: self.session.goto(row.position))
        clear = Gtk.Button(label="Clear Queue", halign=Gtk.Align.CENTER, margin_top=12)
        clear.add_css_class("pill")
        clear.connect("clicked", lambda *_: self.session.clear_queue())
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, margin_start=12, margin_end=12,
                      margin_top=6, margin_bottom=18)
        self.queue_empty = Gtk.Label(label="The queue is empty", margin_top=48, visible=False)
        self.queue_empty.add_css_class("dim-label")
        box.append(self.queue_empty)
        # Kodi's Party Mode: it fills the queue itself
        self.party_box = Gtk.Box(spacing=8, margin_bottom=10, visible=False)
        note = Gtk.Label(label="Party Mode — Kodi adds songs as it goes", xalign=0,
                         hexpand=True, wrap=True)
        note.add_css_class("dim-label")
        note.add_css_class("caption")
        stop = Gtk.Button(label="Stop Party Mode", valign=Gtk.Align.CENTER)
        stop.add_css_class("flat")
        stop.connect("clicked", lambda *_: self.session.stop_partymode())
        self.party_box.append(note)
        self.party_box.append(stop)
        box.append(self.party_box)
        box.append(self.queue_list)
        box.append(clear)
        return Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER,
                                  child=Adw.Clamp(maximum_size=600, child=box))

    # -- drag to reorder ------------------------------------------------------
    def _make_draggable(self, row):
        """A drag handle at the row's start (only it starts a drag, so the list still
        scrolls by touch); dropping on another row moves the song there."""
        handle = Gtk.Image(icon_name="tonearm-drag-symbolic", tooltip_text="Drag to Reorder")
        handle.add_css_class("drag-handle")
        row.add_prefix(handle)
        src = Gtk.DragSource(actions=Gdk.DragAction.MOVE)
        src.connect("prepare", lambda *_: Gdk.ContentProvider.new_for_value(
            GObject.Value(GObject.TYPE_INT, row.position)))
        src.connect("drag-begin", lambda s, drag: (
            s.set_icon(Gtk.WidgetPaintable.new(row), 24, 24), row.add_css_class("dragging")))
        src.connect("drag-end", lambda *_: row.remove_css_class("dragging"))
        handle.add_controller(src)
        drop = Gtk.DropTarget.new(GObject.TYPE_INT, Gdk.DragAction.MOVE)
        drop.set_preload(True)     # know where the song comes from while hovering

        def enter(t, _x, _y):
            src_pos = t.get_value()
            # a line where it will land: under this row moving down, above it moving up
            below = isinstance(src_pos, int) and src_pos < row.position
            row.add_css_class("drop-below" if below else "drop-above")
            return Gdk.DragAction.MOVE

        def leave(*_):
            row.remove_css_class("drop-above")
            row.remove_css_class("drop-below")

        drop.connect("enter", enter)
        drop.connect("leave", leave)

        def dropped(_t, value, _x, _y):
            leave()
            if isinstance(value, int) and value != row.position:
                self.session.move_in_queue(value, row.position)
                return True
            return False

        drop.connect("drop", dropped)
        row.add_controller(drop)

    def _toggle_queue(self, btn):
        show = btn.get_active()
        self.stack.set_visible_child_name("queue" if show else "player")
        self.set_title("Queue" if show else "Now Playing")
        if show:
            self._load_queue()

    def _load_queue(self):
        if self.stack.get_visible_child_name() != "queue":
            return
        self.session.get_queue(self._fill_queue)

    def _fill_queue(self, items):
        while (row := self.queue_list.get_first_child()) is not None:
            self.queue_list.remove(row)
        current = self.session.state.queue_position
        self.queue_empty.set_visible(not items)
        self.queue_list.set_visible(bool(items))
        target = None
        for pos, it in enumerate(items):
            row = plain_row(title=it.get("title") or it.get("label") or "", use_markup=False,
                                subtitle=", ".join(it.get("artist") or []), activatable=True)
            row.position = pos
            self._make_draggable(row)
            dur = Gtk.Label(label=fmt_time(it.get("duration")))
            dur.add_css_class("dim-label")
            dur.add_css_class("numeric")
            row.add_suffix(dur)
            rm = Gtk.Button(icon_name="tonearm-remove-symbolic", valign=Gtk.Align.CENTER,
                            tooltip_text="Remove")
            rm.add_css_class("flat")
            rm.connect("clicked", lambda b, p=pos: self.session.remove_from_queue(p))
            row.add_suffix(rm)
            if pos == current:
                row.add_css_class("now-playing")
                row.add_prefix(Gtk.Image(icon_name="tonearm-play-symbolic"))
                target = row
            self.queue_list.append(row)
        if target is not None:
            GLib.idle_add(lambda: (target.grab_focus(), False)[1])
