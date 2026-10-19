"""Table-like list rows that use the whole width.

A ``ColumnSet`` describes the extra columns of a list (Artist, Year, Genre, …). The
list sits in a ``WidthBox``; whenever its width changes, columns are dropped one by
one — lowest priority first — until the rest fit. Whatever is hidden moves into the
row's dim subtitle, so no information disappears on a narrow window or a phone.
"""
from __future__ import annotations

import weakref
from dataclasses import dataclass
from typing import Callable

from gi.repository import GLib, Gtk, Pango

SPACING = 12


@dataclass
class Column:
    key: str
    title: str
    width: int
    priority: int                        # 1 = hidden last
    value: Callable[[object], str]       # text in the column
    meta: Callable[[object], str] | None = None   # text in the subtitle when hidden
    xalign: float = 0.0
    numeric: bool = False


class WidthBox(Gtk.Widget):
    """A vertical box that reports its allocated width. (A custom widget rather than a
    Gtk.Box subclass: widgets with a layout manager never call size_allocate.)

    ``elastic``: ask for no minimum width. When the window gets narrower than what the
    content needs, the content is clipped for a moment while ``on_width`` re-lays it
    out — instead of the content holding the window wide (and never learning that it
    should get narrower)."""

    __gtype_name__ = "TonearmWidthBox"

    def __init__(self, on_width, elastic: bool = False):
        super().__init__(vexpand=True)
        self._on_width = on_width
        self._elastic = elastic
        self._width = -1
        self._box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self._box.set_parent(self)
        if elastic:
            self.set_overflow(Gtk.Overflow.HIDDEN)

    def append(self, child: Gtk.Widget):
        self._box.append(child)

    def _content_min_width(self) -> int:
        return self._box.measure(Gtk.Orientation.HORIZONTAL, -1)[0]

    def do_get_request_mode(self):
        # height-for-width, like the box inside: otherwise GTK asks for our height
        # without a width, wrapping grids (FlowBox) answer for one long row, and the
        # page ends up shorter than its content — the last albums out of reach
        return Gtk.SizeRequestMode.HEIGHT_FOR_WIDTH

    def do_measure(self, orientation, for_size):
        if not self._elastic:
            return self._box.measure(orientation, for_size)
        if orientation == Gtk.Orientation.HORIZONTAL:
            minimum, natural, _b1, _b2 = self._box.measure(orientation, for_size)
            return 0, natural, -1, -1
        if for_size >= 0:
            for_size = max(for_size, self._content_min_width())
        return self._box.measure(orientation, for_size)

    def do_size_allocate(self, width, height, baseline):
        inner = max(width, self._content_min_width()) if self._elastic else width
        self._box.allocate(inner, height, baseline, None)
        if width != self._width:
            self._width = width
            self._on_width(width)

    def do_dispose(self):
        from .listview import unparent_children
        unparent_children(self)
        self.__dict__["_box"] = None
        Gtk.Widget.do_dispose(self)


def column_box(columns: "ColumnSet", vexpand: bool = True) -> Gtk.Widget:
    """A vertical box for a column list whose columns follow its width (an elastic
    WidthBox: it takes its full natural height inside a scrolling page)."""
    box = WidthBox(columns.set_width, elastic=True)
    box.set_vexpand(vexpand)
    return box


class ColumnSet:
    def __init__(self, columns: list[Column], reserve: int):
        """``reserve``: pixels the fixed part of a row needs (cover/avatar, a readable
        title, the ⋯ button, margins) before any column gets room."""
        self.columns = columns
        self.reserve = reserve
        self.visible: set[str] = set()
        self._rows: "weakref.WeakSet" = weakref.WeakSet()
        self._pending: set | None = None

    # -- width → visible columns -----------------------------------------
    def fit(self, width: int) -> set[str]:
        room = width - self.reserve
        visible = set()
        for col in sorted(self.columns, key=lambda c: c.priority):
            need = col.width + SPACING
            if room < need:
                break  # strictly one by one: never skip a column to fit a later one
            visible.add(col.key)
            room -= need
        return visible

    def set_width(self, width: int):
        visible = self.fit(width)
        if visible == self.visible and self._pending is None:
            return
        first = self._pending is None
        self._pending = visible
        if first:  # never change widgets during size allocation
            # high priority: ahead of the stream of artwork callbacks, or the columns
            # would wait (showing their info as subtitles) until those are all done
            GLib.idle_add(self._apply, priority=GLib.PRIORITY_HIGH)

    def _apply(self):
        visible, self._pending = self._pending, None
        if visible is not None and visible != self.visible:
            self.visible = visible
            for row in list(self._rows):
                row.apply_columns()
        return False

    def guess(self, window):
        """Start from the window's width (less a sidebar) so rows show their columns from
        the first frame; the real width corrects it right after."""
        width = window.get_width() or window.get_default_size()[0]
        if width > 0 and not self.visible:
            self.visible = self.fit(width - 300 if width > 900 else width)

    # -- rows ------------------------------------------------------------
    def add_labels(self, box: Gtk.Box) -> dict:
        """Append one label per column to a row box; returns them by key."""
        labels = {}
        for col in self.columns:
            lbl = Gtk.Label(xalign=col.xalign, width_request=col.width, max_width_chars=1,
                            ellipsize=Pango.EllipsizeMode.END, visible=col.key in self.visible)
            lbl.add_css_class("dim-label")
            if col.numeric:
                lbl.add_css_class("numeric")
            labels[col.key] = lbl
            box.append(lbl)
        return labels

    def register(self, row):
        self._rows.add(row)

    def meta_text(self, obj, extra: str | None = None) -> str:
        """Subtitle: the hidden columns, most important first."""
        bits = [extra] if extra else []
        for col in sorted(self.columns, key=lambda c: c.priority):
            if col.key not in self.visible:
                text = (col.meta or col.value)(obj)
                if text and text != "–":
                    bits.append(text)
        return " · ".join(bits)


class ColumnRow(Gtk.Box):
    """Base row: prefix widget, title + subtitle, columns, optional suffix."""

    def __init__(self, columns: ColumnSet, prefix: Gtk.Widget, suffix: Gtk.Widget | None = None,
                 margins: int = 4):
        # no side margins: the row's padding (style.css) is the inset, as in the header
        super().__init__(spacing=SPACING, margin_top=margins, margin_bottom=margins)
        self.columns = columns
        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER, hexpand=True)
        self.title = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)
        self.title.add_css_class("heading")
        self.sub = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)
        self.sub.add_css_class("dim-label")
        self.sub.add_css_class("caption")
        texts.append(self.title)
        texts.append(self.sub)
        self.append(prefix)
        self.append(texts)
        self.cols = columns.add_labels(self)
        self.prefix, self.suffix = prefix, suffix
        if suffix is not None:
            self.append(suffix)
        self.item = None
        self.extra = None
        columns.register(self)

    def set_item(self, title: str, item, extra: str | None = None):
        self.item = item
        self.extra = extra
        self.title.set_label(title)
        for col in self.columns.columns:
            self.cols[col.key].set_label(col.value(item))
        self.apply_columns()

    def apply_columns(self):
        for key, lbl in self.cols.items():
            lbl.set_visible(key in self.columns.visible)
        if self.item is not None:
            text = self.columns.meta_text(self.item, self.extra)
            self.sub.set_label(text)
            self.sub.set_visible(bool(text))
