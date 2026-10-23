""""What should I play?" — one pick of decade and genre starts an endless radio mix."""
from __future__ import annotations

from gi.repository import Adw, Gtk, Pango

from ..util import run_async


PHONE = 600      # narrower windows get the radio as a bottom sheet, not a popover


def radio_button(window) -> Gtk.Button:
    """The radio button beside the search bar. On a phone it opens a bottom sheet (a
    dialog): a popover from inside the header's squeezed title area didn't open there."""
    btn = Gtk.Button(icon_name="tonearm-radio-symbolic", tooltip_text="What should I play?")
    btn.add_css_class("radio-button")
    state = {"pop": None}

    def clicked(_b):
        if window.get_width() < PHONE:
            open_radio_dialog(window)
            return
        if state["pop"] is None:
            panel = RadioPanel(window)
            pop = Gtk.Popover(child=panel)
            pop.set_parent(btn)
            panel.close = pop.popdown
            pop.connect("show", lambda *_: panel.opened())
            btn.connect("destroy", lambda *_: pop.unparent())
            state["pop"] = pop
        state["pop"].popup()

    btn.connect("clicked", clicked)
    return btn


def open_radio_dialog(window):
    panel = RadioPanel(window, compact=True)
    dlg = Adw.Dialog(title="What should I play?", content_width=380)
    tv = Adw.ToolbarView()
    tv.add_top_bar(Adw.HeaderBar())
    tv.set_content(panel)
    panel.title.set_visible(False)          # the dialog's header says it
    dlg.set_child(tv)
    panel.close = dlg.close
    panel.opened()
    dlg.present(window)


class RadioPanel(Gtk.Box):
    """The radio's choices; shown in a popover (desktop) or a bottom sheet (phone).
    ``close`` is set by whoever shows it."""

    def __init__(self, window, compact: bool = False):
        """``compact``: a phone's bottom sheet — nothing may need more than its width
        (the two pill buttons side by side did: one above the other here)."""
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.close = lambda: None
        self.window = window
        self.session = window.session
        self.decades: list = [None]
        self.genres: list = [None]

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin_top=12,
                      margin_bottom=12, margin_start=12 if not compact else 18,
                      margin_end=12 if not compact else 18,
                      width_request=260 if not compact else -1)
        title = Gtk.Label(label="What should I play?", xalign=0)
        title.add_css_class("title-4")
        box.append(title)
        self.title = title
        hint = Gtk.Label(label="Pick a decade and a genre for an endless random mix.",
                         xalign=0, wrap=True, max_width_chars=30)
        hint.add_css_class("dim-label")
        hint.add_css_class("caption")
        box.append(hint)

        self.decade_dd = self._dropdown()
        self.genre_dd = self._dropdown(searchable=True)
        # a grid: labels in one column, the dropdowns lined up in the next, both as wide
        grid = Gtk.Grid(column_spacing=12, row_spacing=8)
        for i, (label, dd) in enumerate((("Decade", self.decade_dd), ("Genre", self.genre_dd))):
            lbl = Gtk.Label(label=label, xalign=0)
            dd.set_hexpand(True)
            grid.attach(lbl, 0, i, 1, 1)
            grid.attach(dd, 1, i, 1, 1)
        box.append(grid)
        # Kodi's Party Mode instead: Kodi itself keeps the music coming (also with
        # Tonearm closed) — from the whole library; a smart playlist's Party Mode is in
        # the Playlists page
        party = Gtk.Box(spacing=12, margin_top=2)
        party_texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
        party_title = Gtk.Label(label="Keep playing when Tonearm is closed", xalign=0,
                                wrap=True, max_width_chars=24)
        party_sub = Gtk.Label(label="Kodi's Party Mode, from the whole library (a smart "
                                    "playlist's: on its page)", xalign=0, wrap=True,
                              max_width_chars=26)
        party_sub.add_css_class("dim-label")
        party_sub.add_css_class("caption")
        party_texts.append(party_title)
        party_texts.append(party_sub)
        self.party_switch = Gtk.Switch(valign=Gtk.Align.CENTER,
                                       active=bool(self.window.config.prefs.get("radio_partymode")))
        self.party_switch.connect("notify::active", lambda *_: self._party_toggled())
        party.append(party_texts)
        party.append(self.party_switch)
        box.append(party)
        self.count = Gtk.Label(xalign=0, wrap=True, max_width_chars=30)
        self.count.add_css_class("dim-label")
        self.count.add_css_class("caption")
        box.append(self.count)

        # both labels in a stack: the button keeps the width of the longer one, so the
        # popup doesn't change size when the Party Mode switch flips
        self.start_label = Gtk.Stack(hhomogeneous=True, vhomogeneous=True)
        self.start_label.add_named(Gtk.Label(label="Start Radio"), "radio")
        self.start_label.add_named(Gtk.Label(label="Start Party Mode"), "party")
        start = Gtk.Button(child=self.start_label)
        start.add_css_class("pill")
        start.add_css_class("suggested-action")
        start.connect("clicked", self._start)
        self.start_btn = start
        surprise = Gtk.Button(label="Surprise Me")
        surprise.add_css_class("pill")
        surprise.connect("clicked", self._surprise)
        self.surprise_btn = surprise
        buttons = Gtk.Box(spacing=8, homogeneous=True, margin_top=4)
        if compact:
            buttons.set_orientation(Gtk.Orientation.VERTICAL)
            buttons.append(start)
            buttons.append(surprise)
        else:
            buttons.append(surprise)
            buttons.append(start)
        box.append(buttons)

        self.now = Gtk.Box(spacing=8, visible=False)
        self.now_label = Gtk.Label(xalign=0, hexpand=True, wrap=True)
        self.now_label.add_css_class("caption")
        stop = Gtk.Button(label="Stop")
        stop.add_css_class("flat")
        stop.connect("clicked", lambda *_: self._stop())
        self.now.append(self.now_label)
        self.now.append(stop)
        box.append(self.now)

        self.append(box)
        self._box = box
        self._radio_pick = (None, None)     # the decade / genre while Party Mode is on
        self.decade_dd.connect("notify::selected", lambda *_: self._update_count())
        self.genre_dd.connect("notify::selected", lambda *_: self._update_count())

    def opened(self):
        """Shown (again): fresh choices from the library."""
        self._populate()

    @staticmethod
    def _label_factory(ellipsize: bool) -> Gtk.SignalListItemFactory:
        f = Gtk.SignalListItemFactory()

        def setup(_f, li):
            lbl = Gtk.Label(xalign=0)
            if ellipsize:
                lbl.set_ellipsize(Pango.EllipsizeMode.END)
                lbl.set_max_width_chars(1)
                lbl.set_hexpand(True)
            li.set_child(lbl)

        f.connect("setup", setup)
        f.connect("bind", lambda _f, li: li.get_child().set_label(li.get_item().get_string()))
        return f

    @classmethod
    def _dropdown(cls, searchable=False) -> Gtk.DropDown:
        dd = Gtk.DropDown.new_from_strings(["Any"])
        # the chosen value shortens with "…" (a long genre name made the panel wider
        # than a phone); the list itself shows names in full
        dd.set_factory(cls._label_factory(True))
        dd.set_list_factory(cls._label_factory(False))
        if searchable:
            dd.set_expression(Gtk.PropertyExpression.new(Gtk.StringObject, None, "string"))
            dd.set_enable_search(True)
        return dd

    def _show_now(self):
        label = self.session.radio_label
        self.now.set_visible(bool(label))
        if label:
            self.now_label.set_label(f"Now: {label}")

    def _stop(self):
        if self.session.state.partymode:
            self.session.stop_partymode()
        else:
            self.session.stop_radio()
        self.now.set_visible(False)

    @property
    def _party(self) -> bool:
        return self.party_switch.get_active()

    def _party_toggled(self):
        prefs = self.window.config.prefs
        if bool(prefs.get("radio_partymode")) != self._party:
            prefs["radio_partymode"] = self._party
            self.window.config.save()
        self._sync_party()

    def _sync_party(self):
        """Party Mode can't pick a decade or genre: the choices rest at "Any"."""
        party = self._party
        # never shrink or grow while flipping (a long genre name made the popup wider
        # than "Any genre" does)
        width = self._box.get_width()
        if width > self._box.get_size_request()[0]:
            self._box.set_size_request(width, -1)
        if party and self.decade_dd.get_sensitive():
            self._radio_pick = self._choice()          # given back when switched off
            self.decade_dd.set_selected(0)
            self.genre_dd.set_selected(0)
        elif not party and not self.decade_dd.get_sensitive():
            d, g = self._radio_pick
            self.decade_dd.set_selected(self.decades.index(d) if d in self.decades else 0)
            self.genre_dd.set_selected(self.genres.index(g) if g in self.genres else 0)
        for dd in (self.decade_dd, self.genre_dd):
            dd.set_sensitive(not party)
        self.surprise_btn.set_sensitive(not party)
        self.start_label.set_visible_child_name("party" if party else "radio")
        self._update_count()

    def _populate(self):
        """Fill the choices from the library each time it opens (cheap, from the cache)."""
        self._show_now()
        cache = self.session.cache
        if cache is None:
            return

        def work():
            return cache.decades(), cache.genres()

        def done(res):
            decades, genres = res
            keep_d = self.decades[self.decade_dd.get_selected()] if self.decades else None
            keep_g = self.genres[self.genre_dd.get_selected()] if self.genres else None
            self.decades = [None] + [d for d, n, _ in decades]
            self.genres = [None] + [g for g, n, _ in sorted(genres, key=lambda t: -t[1])]
            self.decade_dd.set_model(Gtk.StringList.new(
                ["Any decade"] + [(f"{d}s" if d else "Unknown year") for d in self.decades[1:]]))
            self.genre_dd.set_model(Gtk.StringList.new(["Any genre"] + self.genres[1:]))
            if keep_d in self.decades:
                self.decade_dd.set_selected(self.decades.index(keep_d))
            if keep_g in self.genres:
                self.genre_dd.set_selected(self.genres.index(keep_g))
            self._sync_party()

        run_async(work, done=done)

    def _choice(self):
        d = self.decades[self.decade_dd.get_selected()] if self.decade_dd.get_selected() < len(self.decades) else None
        g = self.genres[self.genre_dd.get_selected()] if self.genre_dd.get_selected() < len(self.genres) else None
        return d, g

    def _update_count(self):
        cache = self.session.cache
        if cache is None:
            return
        decade, genre = self._choice()

        def work():
            return cache.radio_count(cache.radio_album_ids(decade, genre))

        def done(n):
            if (decade, genre) != self._choice():
                return
            if self._party:
                self.count.set_label(f"Kodi picks from all {n:,} songs" if n
                                     else "The library is empty")
                self.start_btn.set_sensitive(n > 0)
                return
            self.count.set_label(f"{n:,} song{'s' if n != 1 else ''} to pick from" if n
                                 else "Nothing matches — try another pick")
            self.start_btn.set_sensitive(n > 0)

        run_async(work, done=done)

    def _start(self, *_):
        decade, genre = self._choice()
        self.close()
        if self._party:
            self.session.start_partymode()
        else:
            self.session.start_radio(decade, genre)

    def _surprise(self, *_):
        """A random decade and genre that has a decent number of songs."""
        def done(pick):
            d, g = pick
            self.decade_dd.set_selected(self.decades.index(d) if d in self.decades else 0)
            self.genre_dd.set_selected(self.genres.index(g) if g in self.genres else 0)
            self.close()
            self.session.start_radio(d, g)

        self.session.pick_surprise(done)
