"""Reusable widgets: artwork, album tiles, list items, context menus."""
from __future__ import annotations

import weakref

from gi.repository import Adw, Gdk, Gio, GLib, GObject, Graphene, Gtk, Pango

from ..cache import Album, Artist
from ..logger import log


class Cover(Gtk.Overlay):
    """Square artwork with a placeholder icon; async-loads from the art cache."""

    def __init__(self, size: int = 160, icon: str = "tonearm-album-symbolic",
                 round_: bool = False, css: str | None = None):
        super().__init__()
        self.set_size_request(size, size)
        self.set_halign(Gtk.Align.CENTER)
        self.set_valign(Gtk.Align.CENTER)
        self.set_overflow(Gtk.Overflow.HIDDEN)
        self.add_css_class("cover")
        if round_:
            self.add_css_class("round")
        if css:
            self.add_css_class(css)
        self.placeholder = Gtk.Image(icon_name=icon, pixel_size=max(16, size // 3))
        self.placeholder.add_css_class("dim-label")
        self.set_child(self.placeholder)
        self.picture = Gtk.Picture(content_fit=Gtk.ContentFit.COVER, can_shrink=True)
        self.add_overlay(self.picture)
        self._key = None
        self._ticket = None

    def set_art(self, session, thumb: str, size: str = "small"):
        if thumb == self._key and self.picture.get_paintable() is not None:
            return
        art = session.artwork if session is not None else None
        if art is not None:
            art.cancel(self._ticket)  # tile was recycled: drop the old request
        self._ticket = None
        self._key = thumb
        self.picture.set_paintable(None)
        if not thumb or art is None:
            return

        def done(tex, key=thumb):
            if self._key == key:
                self._ticket = None
                self.picture.set_paintable(tex)

        self._ticket = art.load(thumb, size, done)


class ZoomBin(Adw.Bin):
    """Draws its child scaled about its centre — no relayout, cheap per frame. The zoom
    follows a target on a spring (``set_target``), so it overshoots and settles like a
    rubber band instead of jumping."""

    __gtype_name__ = "TonearmZoomBin"

    STIFFNESS = 170.0     # spring: higher = snappier
    DAMPING = 11.0        # lower = more bounce

    def __init__(self, **kw):
        super().__init__(**kw)
        self._zoom = 1.0
        self._target = 1.0
        self._velocity = 0.0
        self._tick = 0
        self._last = 0

    def set_target(self, zoom: float):
        self._target = max(1.0, zoom)
        if not self._tick:
            self._last = 0
            self._tick = self.add_tick_callback(self._step)

    def kick(self, velocity: float):
        """A push on the spring (an overscroll at the top)."""
        self._velocity = min(1.5, self._velocity + velocity)   # repeated pulls: capped
        self.set_target(self._target)

    def _step(self, _widget, clock):
        now = clock.get_frame_time() / 1e6
        dt = min(0.05, now - self._last) if self._last else 1 / 60
        self._last = now
        force = (self._target - self._zoom) * self.STIFFNESS - self._velocity * self.DAMPING
        self._velocity += force * dt
        self._zoom = max(0.9, self._zoom + self._velocity * dt)
        self.queue_draw()
        if abs(self._target - self._zoom) < 0.0005 and abs(self._velocity) < 0.002:
            self._zoom, self._velocity, self._tick = self._target, 0.0, 0
            return GLib.SOURCE_REMOVE
        return GLib.SOURCE_CONTINUE

    def do_snapshot(self, snapshot):
        child = self.get_child()
        if child is None:
            return
        zoom = max(1.0, self._zoom)       # never smaller than the band (no gaps)
        if zoom == 1.0:
            self.snapshot_child(child, snapshot)
            return
        w, h = self.get_width() / 2, self.get_height() / 2
        snapshot.save()
        snapshot.translate(Graphene.Point().init(w, h))
        snapshot.scale(zoom, zoom)
        snapshot.translate(Graphene.Point().init(-w, -h))
        self.snapshot_child(child, snapshot)
        snapshot.restore()


class Backdrop(Adw.Bin):
    """Full-width artwork behind a page header that fades into the window background.

    The height is fixed (an Adw.Clamp caps it, the picture's size request floors it):
    a wide window crops the image instead of stretching the band down the page.
    """

    HEIGHT = 360

    def __init__(self):
        super().__init__(valign=Gtk.Align.START, can_target=False, can_focus=False)
        self.add_css_class("backdrop")
        self.picture = Gtk.Picture(content_fit=Gtk.ContentFit.COVER, can_shrink=True, opacity=0)
        self.picture.set_size_request(-1, self.HEIGHT)
        self.picture.add_css_class("backdrop-picture")
        self.zoomer = ZoomBin(child=self.picture)     # zoom (artist page: at the top)
        layers = Gtk.Overlay(child=self.zoomer)
        layers.set_overflow(Gtk.Overflow.HIDDEN)
        fade = Gtk.Box()
        fade.add_css_class("backdrop-fade")
        layers.add_overlay(fade)
        self.clamp = Adw.Clamp(orientation=Gtk.Orientation.VERTICAL, maximum_size=self.HEIGHT,
                               tightening_threshold=self.HEIGHT, child=layers)
        self.set_child(self.clamp)
        self._height = self.HEIGHT
        self._height_listeners: list = []   # fn(band height) — pages that size to it
        self._key = None
        self._anim = Adw.TimedAnimation(
            widget=self.picture, value_from=0.0, value_to=1.0, duration=450,
            target=Adw.PropertyAnimationTarget.new(self.picture, "opacity"))

    @staticmethod
    def height_for(width: int) -> int:
        """Taller band on bigger screens, so wide fanart reaches further down the page."""
        if width >= 1400:
            return 500
        if width >= 900:
            return 430
        return Backdrop.HEIGHT

    def fit_width(self, width: int):
        h = self.height_for(width)
        if h != self._height:
            self._height = h
            GLib.idle_add(self._apply_height, h)  # not while GTK is allocating

    def _apply_height(self, h):
        if h == self._height:
            self.picture.set_size_request(-1, h)
            self.clamp.set_maximum_size(h)
            self.clamp.set_tightening_threshold(h)
            if h >= self.height_for(1400):   # very wide: the fade starts higher
                self.add_css_class("wide")
            else:
                self.remove_css_class("wide")
            for fn in self._height_listeners:
                fn(h)
        return False

    def follow_scroll(self, scrolled: Gtk.ScrolledWindow, zoom: float = 0.25,
                      distance: float = 320.0):
        """Zoom the picture in as the page scrolls to the top (``1 + zoom`` there), back
        to its normal size ``distance`` px down — on a spring, so it overshoots and
        settles; pulling past the top (overscroll) pushes it in further for a moment."""
        adj = scrolled.get_vadjustment()

        def update(*_):
            t = max(0.0, 1.0 - adj.get_value() / distance)
            self.zoomer.set_target(1.0 + zoom * t * t)

        def overshot(_sw, pos):
            if pos == Gtk.PositionType.TOP:
                self.zoomer.kick(0.6)

        adj.connect("value-changed", update)
        scrolled.connect("edge-overshot", overshot)
        update()

    def on_height(self, fn):
        """Call ``fn(height)`` now and whenever the band's height changes."""
        self._height_listeners.append(fn)
        fn(self._height)

    def set_art(self, session, key: str, soft: bool = False):
        """Show ``key`` (artwork-cache key). ``soft`` blurs low-res fallbacks like covers —
        by stretching a tiny copy, which costs nothing per frame (a CSS blur filter
        re-renders the whole band on every scroll step)."""
        if key == self._key:
            return
        self._key = key
        log.debug("Backdrop %skey=%r", "soft " if soft else "", key)
        if soft:
            self.picture.add_css_class("soft")
        else:
            self.picture.remove_css_class("soft")
        if not key or session is None or session.artwork is None:
            self.picture.set_paintable(None)
            self.picture.set_opacity(0)
            return

        def done(tex, k=key):
            log.debug("Backdrop loaded=%s key=%r", tex is not None, k)
            if self._key != k or tex is None:
                return
            self.picture.set_paintable(tex)
            self._anim.play()

        session.artwork.load(key, "tiny" if soft else "wide", done)


LIST_WIDTH = 960   # track lists (album page) and the artist page's album list: one width


def detail_scaffold(page: Adw.NavigationPage, menu=None, window=None, wide=False,
                    width: int = 760):
    """ToolbarView + scrolling, clamped content laid over a full-width Backdrop.
    ``wide``: the content may use the whole width (the page clamps parts itself).

    Returns (content_box, header, backdrop).
    """
    tv = Adw.ToolbarView()
    header = Adw.HeaderBar()
    if window is not None:
        header.set_title_widget(window.make_search_entry())
        header.set_centering_policy(Adw.CenteringPolicy.STRICT)
    if menu is not None:
        page.menu_button = Gtk.MenuButton(icon_name="tonearm-more-symbolic",
                                          tooltip_text="More")
        set_menu(page.menu_button, menu)
        header.pack_end(page.menu_button)
    tv.add_top_bar(header)
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                  margin_top=24, margin_bottom=24, margin_start=12, margin_end=12)
    clamp = Adw.Clamp(maximum_size=100_000 if wide else width, child=box)
    backdrop = Backdrop()
    from .columns import WidthBox  # noqa: PLC0415  (columns imports nothing from here)
    band = WidthBox(backdrop.fit_width)
    band.set_vexpand(False)
    band.set_valign(Gtk.Align.START)
    band.append(backdrop)
    layers = Gtk.Overlay(child=band)
    layers.add_overlay(clamp)
    layers.set_measure_overlay(clamp, True)
    sw = scrolled(layers)
    # safety net: whatever on the page is too wide gets cut off at the edge instead of
    # widening the whole window past a phone's screen (header, player bar and all)
    sw.set_policy(Gtk.PolicyType.EXTERNAL, Gtk.PolicyType.AUTOMATIC)
    backdrop.follow_scroll(sw)       # the fanart zooms in towards the top (rubber band)
    tv.set_content(sw)
    page.set_child(tv)
    return box, header, backdrop


class Spinner(Adw.Bin):
    """A plain spinning circle (Adw.Spinner, libadwaita ≥ 1.6). Gtk.Spinner draws the
    theme's "process-working" icon, which some themes make a cog wheel.

    Keeps Gtk.Spinner's API: ``spinning`` property, start(), stop()."""

    __gtype_name__ = "TonearmSpinner"

    def __init__(self, spinning: bool = False, size: int = 16, **kw):
        super().__init__(**kw)
        self._inner = Adw.Spinner() if hasattr(Adw, "Spinner") else Gtk.Spinner(spinning=True)
        self._inner.set_size_request(size, size)
        self.set_child(self._inner)
        self._spinning = False
        self.set_spinning(spinning)

    @GObject.Property(type=bool, default=False)
    def spinning(self):
        return self._spinning

    @spinning.setter
    def spinning(self, value):
        self._spinning = bool(value)
        self._inner.set_visible(self._spinning)

    def set_spinning(self, value: bool):
        self.props.spinning = value

    def start(self):
        self.set_spinning(True)

    def stop(self):
        self.set_spinning(False)


LETTERS = ["#"] + [chr(c) for c in range(ord("A"), ord("Z") + 1)]


def initial(text: str) -> str:
    """Index letter for a sort key: A–Z (accents folded), everything else '#'."""
    import unicodedata
    t = unicodedata.normalize("NFKD", (text or "").strip())
    c = "".join(ch for ch in t[:2] if not unicodedata.combining(ch))[:1].upper()
    return c if "A" <= c <= "Z" else "#"


_jump_views: "weakref.WeakSet" = weakref.WeakSet()
_bottom_room = [0]     # px the bottom bars cover (the glass player bar runs over pages)


def set_bottom_room(height: int):
    """Keep the A–Z rails and back-to-top buttons clear of the bottom bars. (Their
    margins, not CSS ones: a CSS margin taller than the button squeezed its icon to a
    negative height.)"""
    _bottom_room[0] = max(0, int(height))
    for jv in list(_jump_views):
        jv.apply_bottom_room()


class JumpView(Gtk.Overlay):
    """A scrolled list/grid with an A–Z rail to jump by letter and a back-to-top button."""

    def __init__(self, scrolled: Gtk.ScrolledWindow, view: Gtk.Widget):
        super().__init__(child=scrolled)
        _jump_views.add(self)
        self.scrolled = scrolled
        self.view = view
        self._first: dict[str, int] = {}
        self._bubble_id = 0
        self._last_letter = None
        self._anim = None

        # The rail spans the whole height of the view; letters spread out evenly.
        self.rail = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, halign=Gtk.Align.END,
                            valign=Gtk.Align.FILL, homogeneous=True, margin_end=2,
                            margin_top=8, margin_bottom=8, visible=False)
        self.rail.add_css_class("letter-rail")
        self.rail.set_cursor_from_name("pointer")
        self.letters = {}
        for letter in LETTERS:
            lbl = Gtk.Label(label=letter, vexpand=True)
            lbl.add_css_class("letter")
            self.rail.append(lbl)
            self.letters[letter] = lbl
        click = Gtk.GestureClick()
        click.connect("pressed", lambda g, n, x, y: self._jump_at(y))
        click.connect("released", lambda *_: self._hide_bubble_soon())
        self.rail.add_controller(click)
        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", self._drag_begin)
        drag.connect("drag-update", self._drag_update)
        drag.connect("drag-end", lambda *_: self._hide_bubble_soon())
        self.rail.add_controller(drag)
        self.add_overlay(self.rail)

        self.bubble = Gtk.Label(halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER, visible=False,
                                can_target=False)
        self.bubble.add_css_class("letter-bubble")
        self.add_overlay(self.bubble)

        self.top_btn = Gtk.Button(icon_name="tonearm-top-symbolic", tooltip_text="Back to Top",
                                  halign=Gtk.Align.END, valign=Gtk.Align.END,
                                  margin_end=30, margin_bottom=18)
        self.top_btn.add_css_class("osd")
        self.top_btn.add_css_class("circular")
        self.top_btn.add_css_class("to-top")
        # the round button itself bigger (a size request beats the theme's circular size)
        self.top_btn.set_size_request(54, 54)
        self.top_revealer = Gtk.Revealer(child=self.top_btn, halign=Gtk.Align.END,
                                         valign=Gtk.Align.END,
                                         transition_type=Gtk.RevealerTransitionType.CROSSFADE)
        self.top_btn.connect("clicked", lambda *_: self.scroll_to_top())
        self.add_overlay(self.top_revealer)
        scrolled.get_vadjustment().connect("value-changed", self._on_scroll)
        self.apply_bottom_room()

    def apply_bottom_room(self):
        room = _bottom_room[0]
        self.rail.set_margin_bottom(8 + room)
        self.top_revealer.set_margin_bottom(room)

    # -- index -----------------------------------------------------------
    def set_index(self, keys: list[str] | None):
        """``keys``: index letter of each item in display order, or None to hide the rail."""
        self._first = {}
        if keys:
            for pos, k in enumerate(keys):
                self._first.setdefault(k, pos)
        show = bool(keys) and len(keys) > 30
        self.rail.set_visible(show)
        for letter, lbl in self.letters.items():
            if letter in self._first:
                lbl.remove_css_class("empty")
            else:
                lbl.add_css_class("empty")

    def _letter_at(self, y: float) -> str:
        h = max(1, self.rail.get_height())
        i = int(max(0, min(len(LETTERS) - 1, y / h * len(LETTERS))))
        return LETTERS[i]

    def _drag_begin(self, gesture, x, y):
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        self._drag_y = y
        self._jump_at(y)

    def _drag_update(self, gesture, dx, dy):
        self._jump_at(self._drag_y + dy)

    def _jump_at(self, y):
        letter = self._letter_at(y)
        if letter == self._last_letter and self.bubble.get_visible():
            return
        self._last_letter = letter
        self.bubble.set_label(letter)
        self.bubble.set_visible(True)
        if self._bubble_id:
            GLib.source_remove(self._bubble_id)
            self._bubble_id = 0
        # nearest letter that has items, looking forward first
        idx = LETTERS.index(letter)
        for cand in LETTERS[idx:] + LETTERS[:idx][::-1]:
            if cand in self._first:
                self.scroll_to(self._first[cand])
                return

    def _hide_bubble_soon(self):
        if self._bubble_id:
            GLib.source_remove(self._bubble_id)

        def hide():
            self._bubble_id = 0
            self.bubble.set_visible(False)
            self._last_letter = None
            return GLib.SOURCE_REMOVE

        self._bubble_id = GLib.timeout_add(500, hide)

    # -- scrolling -------------------------------------------------------
    def _stop_fling(self):
        """Cancel kinetic (touchpad/touch) deceleration, which would otherwise keep moving
        the view after we jump and undo it."""
        if self._anim is not None:
            self._anim.skip()
            self._anim = None
        if self.scrolled.get_kinetic_scrolling():
            self.scrolled.set_kinetic_scrolling(False)
            self.scrolled.set_kinetic_scrolling(True)

    def scroll_to(self, pos: int):
        """Bring item ``pos`` to the top: scroll past it, then back (views align upward jumps
        to the top edge)."""
        self._stop_fling()
        adj = self.scrolled.get_vadjustment()
        adj.set_value(adj.get_upper())
        self._scroll_view(pos)

    def _scroll_view(self, pos: int):
        if isinstance(self.view, Gtk.ColumnView):     # (pos, column, flags, scroll)
            self.view.scroll_to(pos, None, Gtk.ListScrollFlags.NONE, None)
        else:
            self.view.scroll_to(pos, Gtk.ListScrollFlags.NONE, None)

    def scroll_to_top(self):
        """Glide back to the top (instant when the page is huge), even mid-fling."""
        self._stop_fling()
        adj = self.scrolled.get_vadjustment()
        start = adj.get_value()
        if start <= adj.get_lower():
            return
        if start > adj.get_page_size() * 40:  # very long list: animating would just blur
            adj.set_value(adj.get_lower())
            self._scroll_view(0)
            return
        target = Adw.CallbackAnimationTarget.new(lambda v: adj.set_value(v))
        self._anim = Adw.TimedAnimation(widget=self.scrolled, value_from=start,
                                        value_to=adj.get_lower(), duration=350, target=target,
                                        easing=Adw.Easing.EASE_OUT_CUBIC)

        def finished(*_):
            adj.set_value(adj.get_lower())  # land exactly, even if items resized meanwhile
            self._anim = None

        self._anim.connect("done", finished)
        self._anim.play()

    def _on_scroll(self, adj):
        self.top_revealer.set_reveal_child(adj.get_value() > max(300, adj.get_page_size() * 0.8))


class AlbumObj(GObject.Object):
    __gtype_name__ = "TonearmAlbumObj"

    def __init__(self, album: Album, subtitle: str | None = None):
        super().__init__()
        self.album = album
        self.subtitle = subtitle


class ArtistObj(GObject.Object):
    __gtype_name__ = "TonearmArtistObj"

    def __init__(self, artist: Artist):
        super().__init__()
        self.artist = artist


TILE_SIZES = {"small": 92, "medium": 148, "large": 196}  # 3 / 2 / 1 columns on a 360 px phone


class AlbumTile(Gtk.Box):
    TILE = TILE_SIZES["medium"]  # 2 × (148 + 24 padding) + 4 = 348 px: fits a 360 px phone

    def __init__(self, size: int | None = None, labels: bool = True):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.add_css_class("album-tile")
        size = size or self.TILE
        self.cover = Cover(size)
        self.title = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END, max_width_chars=1, hexpand=True)
        self.title.add_css_class("tile-title")
        self.subtitle = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END, max_width_chars=1, hexpand=True)
        self.subtitle.add_css_class("dim-label")
        self.subtitle.add_css_class("caption")
        self.append(self.cover)
        self.append(self.title)
        self.append(self.subtitle)
        self.title.set_visible(labels)
        self.subtitle.set_visible(labels)
        self.set_size_request(size, -1)
        # as wide as the cover, not the grid cell: titles end where the cover ends
        self.set_halign(Gtk.Align.CENTER)
        # …and only as tall as cover + titles: a lone tile in a tall grid cell
        # (one favourite on a page) must not stretch down the screen
        self.set_valign(Gtk.Align.START)
        self.album: Album | None = None
        # ★ in the cover's top-right corner: lit for a Favorite, faint on hover
        # otherwise; a click toggles it (the album menu in grids leaves it out)
        self.star = Gtk.Button(icon_name="tonearm-starred-symbolic", halign=Gtk.Align.END,
                               valign=Gtk.Align.START, margin_top=6, margin_end=6,
                               visible=False, can_focus=False)
        self.star.add_css_class("cover-star")
        self.star.connect("clicked", self._star_clicked)
        self.cover.add_overlay(self.star)
        self._hover = False
        motion = Gtk.EventControllerMotion()
        motion.connect("enter", lambda *_: self._set_hover(True))
        motion.connect("leave", lambda *_: self._set_hover(False))
        self.add_controller(motion)
        _star_tiles.add(self)

    def _set_hover(self, hover: bool):
        self._hover = hover
        self.sync_star()

    def _star_clicked(self, *_):
        if self.album is not None:
            self.activate_action("win.fav-album", GLib.Variant("i", self.album.id))

    def sync_star(self):
        fav = self.album is not None and self.album.id in FAVOURITES["albums"]
        # a Favorite always shows its ★; others only while the pointer is over the tile
        # (nothing invisible to tap by accident on a phone)
        self.star.set_visible(self.album is not None and (fav or self._hover))
        if fav:
            self.star.add_css_class("on")
        else:
            self.star.remove_css_class("on")
        self.star.set_tooltip_text("Remove from Favorites" if fav else "Add to Favorites")

    def bind(self, session, album: Album, subtitle: str | None = None):
        self.album = album
        self.sync_star()
        self.title.set_label(album.title)
        self.subtitle.set_label(subtitle if subtitle is not None else album.artist)
        self.cover.set_art(session, album.thumbnail, "small")


def follow(source, signal: str, widget, callback):
    """Connect ``callback(widget)`` to ``source``'s signal for as long as the widget is
    in use: the handler disconnects itself once the widget is gone or was removed from
    the window (a popped page). Holds the widget only weakly."""
    import weakref
    ref = weakref.ref(widget)
    seen_root = [False]
    hid = [0]

    def handler(*_):
        w = ref()
        root = w.get_root() if w is not None else None
        if root is not None:
            seen_root[0] = True
            callback(w)
        elif w is None or seen_root[0]:
            source.disconnect(hid[0])
    hid[0] = source.connect(signal, handler)
    return hid[0]


# Kodi Favourites as the menus should show them; kept current by the window
FAVOURITES = {"albums": set(), "artists": set(), "songs": set(), "playlists": set()}
# Kodi's user ratings (0–10) — kept current by the window, read by menus built on open
RATINGS: dict = {"song": {}, "album": {}}
_star_tiles: "weakref.WeakSet" = weakref.WeakSet()


def stars_text(rating: int) -> str:
    """For list columns and subtitles: 7 → "★★★½" ("" when unrated)."""
    r = max(0, min(10, int(rating or 0)))
    return "★" * (r // 2) + ("½" if r % 2 else "")


def rating_submenu(kind: str, itemid: int) -> Gio.Menu:
    """ "Rating" submenu items for a song / album: five stars … one, and none."""
    current = RATINGS.get(kind, {}).get(itemid, 0)
    m = Gio.Menu()
    sec = Gio.Menu()
    for stars in range(5, 0, -1):
        label = "★" * stars + ("  ✓" if current in (stars * 2, stars * 2 - 1) else "")
        it = Gio.MenuItem.new(label, None)
        it.set_action_and_target_value(f"win.rate-{kind}", GLib.Variant("(ii)", (itemid, stars * 2)))
        sec.append_item(it)
    m.append_section(None, sec)
    if current:
        it = Gio.MenuItem.new("Remove Rating", None)
        it.set_action_and_target_value(f"win.rate-{kind}", GLib.Variant("(ii)", (itemid, 0)))
        m.append_item(it)
    return m


def rating_label(kind: str, itemid: int) -> str:
    text = stars_text(RATINGS.get(kind, {}).get(itemid, 0))
    return f"Rating  {text}" if text else "Rating"


class RatingStars(Gtk.Box):
    """Five stars to rate with (Kodi's 1–10: a star is 2; an odd rating shows a half
    star). Tapping the star that's already the rating clears it. ``on_rate(value)``."""

    __gtype_name__ = "TonearmRatingStars"

    def __init__(self, on_rate, size: int = 20, **kw):
        super().__init__(spacing=0, **kw)
        self.add_css_class("rating-stars")
        self.value = 0
        self._on_rate = on_rate
        self._stars = []
        for i in range(1, 6):
            dim = Gtk.Image(icon_name="tonearm-starred-symbolic", pixel_size=size)
            dim.add_css_class("star-off")
            # the lit star on top: a whole one, or its left half (an odd rating)
            lit = Gtk.Image(icon_name="tonearm-starred-symbolic", pixel_size=size)
            lit.add_css_class("star-on")
            over = Gtk.Overlay(child=dim)
            over.add_overlay(lit)
            btn = Gtk.Button(child=over, tooltip_text=f"{i} star{'s' if i > 1 else ''}")
            btn.add_css_class("flat")
            btn.add_css_class("rating-star")
            btn.connect("clicked", lambda _b, n=i: self._clicked(n))
            self._stars.append(lit)
            self.append(btn)
        self.set_value(0)

    def _clicked(self, n: int):
        value = 0 if self.value in (2 * n, 2 * n - 1) else 2 * n
        self.set_value(value)
        self._on_rate(value)

    def set_value(self, value: int):
        self.value = max(0, min(10, int(value or 0)))
        for i, lit in enumerate(self._stars, start=1):
            full, half = self.value >= 2 * i, self.value == 2 * i - 1
            lit.set_visible(full or half)
            lit.set_from_icon_name("tonearm-star-half-symbolic" if half
                                   else "tonearm-starred-symbolic")


def refresh_stars():
    """Favorites changed: update the ★ on every album tile that exists."""
    for tile in list(_star_tiles):
        tile.sync_star()


# Tonearm's playlists for the "Add to Playlist" menus: [(id, name)], kept by the window
PLAYLISTS: list = []


def playlist_submenu(kind: str, item_id: int) -> Gio.Menu:
    """"Add to Playlist" ▸ each playlist, and "New Playlist…"."""
    sub = Gio.Menu()
    for pid, name in PLAYLISTS:
        it = Gio.MenuItem.new(name, None)
        it.set_action_and_target_value("win.playlist-add", GLib.Variant("s", f"{pid}:{kind}:{item_id}"))
        sub.append_item(it)
    new = Gio.Menu()
    it = Gio.MenuItem.new("New Playlist…", None)
    it.set_action_and_target_value("win.playlist-add", GLib.Variant("s", f"new:{kind}:{item_id}"))
    new.append_item(it)
    sub.append_section(None, new)
    return sub


def _item(label, action, target: GLib.Variant | None = None) -> Gio.MenuItem:
    it = Gio.MenuItem.new(label, None)
    if target is None:
        it.set_detailed_action(action)
    else:
        it.set_action_and_target_value(action, target)
    return it


def my_playlist_menu(pid: str, with_play: bool = True) -> Gio.Menu:
    """One of your playlists: play, rename, sort, duplicate, delete."""
    v = GLib.Variant("s", pid)
    m = Gio.Menu()
    if with_play:
        play = Gio.Menu()
        play.append_item(_item("Play", "win.myplaylist-play", v))
        play.append_item(_item("Shuffle", "win.myplaylist-shuffle", v))
        m.append_section(None, play)
    edit = Gio.Menu()
    edit.append_item(_item("Rename…", "win.myplaylist-rename", v))
    sort = Gio.Menu()
    for key, label in (("artist", "Artist"), ("album", "Album"), ("title", "Title"),
                       ("recent", "Date Added")):
        sort.append_item(_item(label, "win.myplaylist-sort", GLib.Variant("s", f"{pid}:{key}")))
    edit.append_submenu("Sort by", sort)
    edit.append_item(_item("Duplicate", "win.myplaylist-duplicate", v))
    m.append_section(None, edit)
    m.append_item(_item("Delete Playlist…", "win.myplaylist-delete", v))
    return m


def playlist_song_menu(pid: str, index: int, count: int, songid: int | None,
                       albumid: int | None) -> Gio.Menu:
    """A song in one of your playlists: move it, remove it — and the song's own menu."""
    m = song_menu(songid, albumid) if songid else Gio.Menu()
    v = GLib.Variant("s", f"{pid}:{index}")
    sec = Gio.Menu()
    if index > 0:
        sec.append_item(_item("Move to Top", "win.myplaylist-top", v))
    if index < count - 1:
        sec.append_item(_item("Move to Bottom", "win.myplaylist-bottom", v))
    sec.append_item(_item("Remove from Playlist", "win.myplaylist-remove", v))
    m.prepend_section(None, sec)
    return m


def artist_menu(artistid: int) -> Gio.Menu:
    v = GLib.Variant("i", artistid)
    m = Gio.Menu()
    play = Gio.Menu()
    for label, action in (("Play", "win.play-artist"), ("Shuffle", "win.shuffle-artist"),
                          ("Play Next", "win.next-artist"), ("Add to Queue", "win.queue-artist")):
        play.append_item(_item(label, action, v))
    m.append_section(None, play)
    more = Gio.Menu()
    fav = artistid in FAVOURITES["artists"]
    more.append_item(_item("Remove from Favorites" if fav else "Add to Favorites",
                           "win.fav-artist", v))
    more.append_submenu("Add to Playlist", playlist_submenu("artist", artistid))
    m.append_section(None, more)
    return m


def make_draggable(row, on_move):
    """A drag handle at the row's start (only it starts a drag, so the list still
    scrolls by touch); dropping on another row calls ``on_move(from, to)``.
    ``row.position``: its place in the list."""
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
            on_move(value, row.position)
            return True
        return False

    drop.connect("drop", dropped)
    row.add_controller(drop)


def ask_name(parent, heading: str, initial: str, confirm: str, callback):
    """A small dialog asking for a name; ``callback(name)`` on confirm."""
    dlg = Adw.AlertDialog(heading=heading)
    entry = Gtk.Entry(text=initial, activates_default=True, placeholder_text="Name")
    dlg.set_extra_child(entry)
    dlg.add_response("cancel", "Cancel")
    dlg.add_response("ok", confirm)
    dlg.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)
    dlg.set_default_response("ok")
    dlg.set_close_response("cancel")
    dlg.set_response_enabled("ok", bool(initial.strip()))
    entry.connect("changed", lambda e: dlg.set_response_enabled("ok", bool(e.get_text().strip())))
    dlg.connect("response", lambda d, r: r == "ok" and entry.get_text().strip()
                and callback(entry.get_text().strip()))
    dlg.present(parent)
    entry.grab_focus()


def album_menu(albumid: int, with_artist: bool = True, with_fav: bool = True) -> Gio.Menu:
    m = Gio.Menu()
    sec = Gio.Menu()
    for label, action in (("Play", "win.play-album"), ("Shuffle", "win.shuffle-album"),
                          ("Play Next", "win.next-album"), ("Add to Queue", "win.queue-album")):
        it = Gio.MenuItem.new(label, None)
        it.set_action_and_target_value(action, GLib.Variant("i", albumid))
        sec.append_item(it)
    m.append_section(None, sec)
    nav = Gio.Menu()
    if with_artist:
        it = Gio.MenuItem.new("Go to Artist", None)
        it.set_action_and_target_value("win.artist-of-album", GLib.Variant("i", albumid))
        nav.append_item(it)
    if with_fav:  # grids have the ★ on the cover instead
        fav = albumid in FAVOURITES["albums"]
        it = Gio.MenuItem.new("Remove from Favorites" if fav else "Add to Favorites", None)
        it.set_action_and_target_value("win.fav-album", GLib.Variant("i", albumid))
        nav.append_item(it)
    nav.append_submenu(rating_label("album", albumid), rating_submenu("album", albumid))
    nav.append_submenu("Add to Playlist", playlist_submenu("album", albumid))
    m.append_section(None, nav)
    return m


def song_menu(songid: int, albumid: int | None = None) -> Gio.Menu:
    m = Gio.Menu()
    for label, action in (("Play", "win.play-song"), ("Play Next", "win.next-song"),
                          ("Add to Queue", "win.queue-song")):
        it = Gio.MenuItem.new(label, None)
        it.set_action_and_target_value(action, GLib.Variant("i", songid))
        m.append_item(it)
    if albumid:
        it = Gio.MenuItem.new("Go to Album", None)
        it.set_action_and_target_value("win.show-album", GLib.Variant("i", albumid))
        m.append_item(it)
    fav = songid in FAVOURITES["songs"]
    sec = Gio.Menu()
    it = Gio.MenuItem.new("Remove from Favorites" if fav else "Add to Favorites", None)
    it.set_action_and_target_value("win.fav-song", GLib.Variant("i", songid))
    sec.append_item(it)
    sec.append_submenu(rating_label("song", songid), rating_submenu("song", songid))
    sec.append_submenu("Add to Playlist", playlist_submenu("song", songid))
    m.append_section(None, sec)
    return m


def set_menu(button: Gtk.MenuButton, model: Gio.MenuModel):
    """Give a MenuButton its menu (built when opened, so it is always current)."""
    button.set_menu_model(model)


def attach_context_menu(widget: Gtk.Widget, menu_factory):
    """Right-click / long-press opens a popover menu built by menu_factory()."""

    def popup(x, y):
        model = menu_factory()
        if model is None:
            return
        pop = Gtk.PopoverMenu.new_from_model(model)
        pop.set_parent(widget)
        pop.set_has_arrow(True)
        rect = Gdk.Rectangle()
        rect.x, rect.y, rect.width, rect.height = int(x), int(y), 1, 1
        pop.set_pointing_to(rect)
        pop.connect("closed", lambda p: GLib.idle_add(p.unparent))
        pop.popup()

    click = Gtk.GestureClick(button=Gdk.BUTTON_SECONDARY)
    click.connect("pressed", lambda g, n, x, y: popup(x, y))
    widget.add_controller(click)
    press = Gtk.GestureLongPress(touch_only=True)
    press.connect("pressed", lambda g, x, y: (g.set_state(Gtk.EventSequenceState.CLAIMED), popup(x, y)))
    widget.add_controller(press)


class FavToggle(Gtk.ToggleButton):
    """★ toggle for a Kodi Favourite. ``on_toggle()`` runs when the user clicks it;
    ``show_state(bool)`` reflects the real state without calling it back."""

    def __init__(self, on_toggle):
        super().__init__(icon_name="tonearm-starred-symbolic", valign=Gtk.Align.CENTER)
        self.add_css_class("circular")
        self.add_css_class("fav-toggle")
        self._quiet = False
        self.connect("toggled", lambda *_: None if self._quiet else on_toggle())
        self.show_state(False)

    def show_state(self, fav: bool):
        self._quiet = True
        self.set_active(fav)
        self._quiet = False
        self.set_tooltip_text("Remove from Favorites" if fav else "Add to Favorites")


def scrolled(child: Gtk.Widget) -> Gtk.ScrolledWindow:
    """A page's vertical scroller (never sideways). ``page-scroller``: it gets room at
    the end for the bottom bars (window._room_under_bars) — scrollers inside menus and
    popovers must not."""
    sw = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True, child=child)
    sw.add_css_class("page-scroller")
    return sw


def load_avatar(owner, avatar: Adw.Avatar, session, name: str, thumb: str, size: str):
    """Show ``name``'s initials in ``avatar``, then its picture once loaded. ``owner``
    (a recycled row / tile) keeps the request, so a late picture for the artist it
    showed before is dropped."""
    avatar.set_text(name)
    avatar.set_custom_image(None)
    owner._avatar_key = thumb
    art = session.artwork
    if art is None:
        return
    art.cancel(getattr(owner, "_avatar_ticket", None))
    owner._avatar_ticket = None
    if thumb:
        def done(tex, key=thumb):
            if owner._avatar_key == key and tex is not None:
                avatar.set_custom_image(tex)
        owner._avatar_ticket = art.load(thumb, size, done)


def button_row(**kw) -> Gtk.Widget:
    """Centred buttons that wrap onto a new line when the row gets too narrow:
    Adw.WrapBox (libadwaita ≥ 1.7), else a FlowBox. Both have ``append``."""
    if hasattr(Adw, "WrapBox"):
        box = Adw.WrapBox(child_spacing=8, line_spacing=8, align=0.5,
                          halign=Gtk.Align.CENTER, **kw)
    else:
        box = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, column_spacing=8,
                          row_spacing=8, min_children_per_line=1, max_children_per_line=4,
                          homogeneous=False, halign=Gtk.Align.CENTER, **kw)
        box.add_css_class("button-row")
    return box


def pill(label: str, icon: str, suggested: bool = False) -> Gtk.Button:
    """A pill button with an icon (Play, Shuffle…), a little narrower than Adwaita's
    so a row of them fits a 360 px phone."""
    content = Adw.ButtonContent(label=label, icon_name=icon)
    if hasattr(content.props, "can_shrink"):      # libadwaita ≥ 1.4: may ellipsize
        content.set_can_shrink(True)
    b = Gtk.Button(child=content)
    b.add_css_class("pill")
    b.add_css_class("page-pill")
    if suggested:
        b.add_css_class("suggested-action")
    return b


def plain_row(title: str = "", subtitle: str = "", use_markup: bool = False,
              **kw) -> Adw.ActionRow:
    """An ActionRow showing title/subtitle as plain text. Setting them in the
    constructor can apply them before use-markup=False, so "R&B" would be parsed as
    Pango markup (and rejected); set them afterwards instead."""
    row = Adw.ActionRow(use_markup=False, **kw)
    row.set_title(title or "")
    row.set_subtitle(subtitle or "")
    return row


def status_page(icon: str, title: str, desc: str = "") -> Adw.StatusPage:
    sp = Adw.StatusPage(icon_name=icon, title=title, description=desc, vexpand=True)
    sp.add_css_class("compact")
    return sp

