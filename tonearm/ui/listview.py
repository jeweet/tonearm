"""List views for albums and artists, built the way Nautilus builds its list view: a
Gtk.ColumnView. GTK draws the column headers itself — click one to sort, again to
reverse (the arrow shows which way), drag a border to resize — and keeps them
aligned with the cells.

On top of that, columns drop out one by one as the list gets narrower (the widths in
``listlayout.Spec.need``), and whatever is hidden moves into the grey line under the
title, so a phone still shows everything.
"""
from __future__ import annotations

from typing import Callable

from gi.repository import GLib, Gtk, Pango

from ..listlayout import Spec, choose

SIDE = 64   # list width − the width columns share (18 + 36 px padding, scrollbar)
NARROW = 600          # narrower (a phone): less side padding (style.css .narrow)
SIDE_NARROW = 36      # 4 + 26 px padding (the A–Z rail), a little slack


def unparent_children(widget: Gtk.Widget):
    """Unparent every child via GTK itself — not via Python attributes, which may be
    gone when a widget is disposed after its Python wrapper."""
    child = widget.get_first_child()
    while child is not None:
        nxt = child.get_next_sibling()
        child.unparent()
        child = nxt


class _WatchLayout(Gtk.LayoutManager):
    """Lays the one child out at full size and reports the width when it changes."""

    __gtype_name__ = "TonearmWatchLayout"

    def __init__(self, callback):
        super().__init__()
        self._callback = callback
        self._width = -1
        self._pending = False

    def do_get_request_mode(self, widget):
        child = widget.get_first_child()
        return child.get_request_mode() if child is not None \
            else Gtk.SizeRequestMode.CONSTANT_SIZE

    def do_measure(self, widget, orientation, for_size):
        child = widget.get_first_child()
        if child is None:
            return 0, 0, -1, -1
        return child.measure(orientation, for_size)

    def do_allocate(self, widget, width, height, baseline):
        child = widget.get_first_child()
        if child is not None:
            child.allocate(width, height, baseline, None)
        if width > 0 and width != self._width:
            self._width = width
            if not self._pending:         # never change the layout during allocation
                self._pending = True
                GLib.idle_add(self._emit, priority=GLib.PRIORITY_HIGH)

    def _emit(self):
        self._pending = False
        self._callback(self._width)
        return False


class SizeWatcher(Gtk.Box):
    """Single-child container that reports its width whenever it changes (a Gtk.Box
    with its own layout manager: GTK removes the child on dispose by itself)."""

    __gtype_name__ = "TonearmSizeWatcher"

    def __init__(self, child: Gtk.Widget, callback: Callable[[int], None]):
        super().__init__(hexpand=True, vexpand=True)
        self.set_layout_manager(_WatchLayout(callback))
        self.append(child)


def cell_label(*classes: str, xalign: float = 0.0) -> Gtk.Label:
    lbl = Gtk.Label(xalign=xalign, max_width_chars=1, width_chars=1,
                    ellipsize=Pango.EllipsizeMode.END)
    for c in classes:
        lbl.add_css_class(c)
    return lbl


class TitleCell(Gtk.Box):
    """Title with a dim subtitle: the info of the columns that are hidden right now."""

    __gtype_name__ = "TonearmTitleCell"

    def __init__(self, table: "TableView"):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER)
        self.table = table
        self.title = cell_label("heading")
        self.sub = cell_label("dim-label", "caption")
        self.append(self.title)
        self.append(self.sub)
        self.item = None
        self.extra = None
        # a strong list: the view owns its cells for its whole life anyway, and a weak
        # set could lose one — that row then kept a stale subtitle after a resize
        table._titles.append(self)
        self.connect("map", lambda *_: self.update())   # catch up when shown again

    def bind(self, title: str, item, extra: str | None):
        self.item, self.extra = item, extra
        self.title.set_label(title)
        self.update()

    def update(self):
        if self.item is None:
            return
        shown, bits = set(), []
        for spec in self.table.meta_specs:
            fn = spec.meta or spec.value
            text = fn(self.item) if fn else ""
            if spec.key in self.table.names:
                if spec.value:
                    shown.add(spec.value(self.item))
            elif text and text != "–":
                bits.append(text)
        # the page's own subtitle ("Artist · 3 plays") minus what a column already shows
        extra = [b for b in (self.extra or "").split(" · ") if b and b not in shown]
        text = " · ".join(dict.fromkeys(extra + bits))
        self.sub.set_label(text)
        self.sub.set_visible(bool(text))


class TableView:
    """A Gtk.ColumnView over ``store`` with one column per ``Spec``.

    ``get(obj)`` returns the item (album / artist) of a list entry; ``title(item)`` its
    title; ``extra(obj)`` an optional subtitle prefix. ``prefix`` / ``suffix`` are
    (setup, bind) pairs for the cover / avatar and ⋯ cells. ``on_sort(key, descending)``
    is called when a column header is clicked."""

    def __init__(self, specs: list[Spec], store, *, prefix_key: str, title_key: str,
                 get: Callable, title: Callable, extra: Callable | None = None,
                 prefix: tuple | None = None, suffix_key: str | None = None,
                 suffix: tuple | None = None, on_sort=None, on_activate=None,
                 menu: Callable | None = None, meta_order: list[str] | None = None):
        self.specs = specs
        self.menu = menu          # obj -> Gio.MenuModel for right-click / long-press
        fixed = {prefix_key, title_key, suffix_key}
        self.data_specs = [s for s in specs if s.key not in fixed]
        # the grey line under a title: hidden columns in this order (default: the
        # columns' own order)
        order = {k: i for i, k in enumerate(meta_order or [])}
        self.meta_specs = sorted(self.data_specs, key=lambda s: order.get(s.key, len(order)))
        self.get, self.title_of = get, title
        self.extra_of = extra or (lambda o: None)
        self.on_sort = on_sort
        self.names = choose(0, specs)              # until the width is known
        self._width = 0
        self._hidden: set = set()                  # columns with nothing to show
        self._titles: list = []
        self._quiet = False

        self.view = Gtk.ColumnView(model=Gtk.NoSelection(model=store),
                                   single_click_activate=True, reorderable=False,
                                   show_row_separators=False, show_column_separators=False)
        self.view.add_css_class("table-view")
        # GTK gives a column view the .view style class, which Adwaita paints in the
        # darker "view" colour — these lists sit on the page, so drop it
        self.view.remove_css_class("view")
        # like Nautilus: no line under the column titles at the top; once the rows
        # scroll under them, a dark one
        self._vadj = None
        self.view.connect("notify::vadjustment", lambda *_: self._watch_scroll())
        self._watch_scroll()
        if on_activate is not None:
            self.view.connect("activate", lambda v, pos: on_activate(store.get_item(pos)))
        self.columns: dict[str, Gtk.ColumnViewColumn] = {}
        for spec in specs:
            if spec.key == prefix_key:
                col = self._column(spec, *prefix)
            elif spec.key == title_key:
                col = self._column(spec, lambda: TitleCell(self),
                                   lambda w, obj: w.bind(self.title_of(self.get(obj)),
                                                         self.get(obj), self.extra_of(obj)))
            elif spec.key == suffix_key:
                col = self._column(spec, *suffix)
            else:
                col = self._column(
                    spec,
                    lambda s=spec: cell_label(*(["dim-label"] + (["numeric"] if s.numeric else [])),
                                              xalign=s.xalign),
                    lambda w, obj, s=spec: w.set_label(s.value(self.get(obj)) if s.value else ""))
            if spec.px is not None:
                col.set_fixed_width(spec.px + 12)        # + the cell's own padding
            else:
                col.set_expand(True)
                col.set_resizable(True)
            if on_sort is not None and spec.sortable and spec.title:
                # GTK draws the arrow; the order itself comes from the page (cache order)
                col.set_sorter(Gtk.CustomSorter.new(lambda a, b, _d: 0, None))
            col.set_visible(spec.key in self.names)
            self.columns[spec.key] = col
            self.view.append_column(col)
        sorter = self.view.get_sorter()
        if sorter is not None:
            sorter.connect("changed", self._on_sorter_changed)

    def _column(self, spec: Spec, setup, bind) -> Gtk.ColumnViewColumn:
        f = Gtk.SignalListItemFactory()

        def do_setup(_f, li):
            w = setup()
            w._tv_obj = None
            if self.menu is not None:
                from .widgets import attach_context_menu
                attach_context_menu(w, lambda w=w: self._menu_for(w))
            li.set_child(w)

        def do_bind(_f, li):
            w = li.get_child()
            w._tv_obj = li.get_item()
            bind(w, li.get_item())

        f.connect("setup", do_setup)
        f.connect("bind", do_bind)
        return Gtk.ColumnViewColumn(title=spec.title, factory=f)

    def _menu_for(self, widget):
        obj = getattr(widget, "_tv_obj", None) if widget is not None else None
        return self.menu(obj) if obj is not None else None

    def _watch_scroll(self):
        adj = self.view.get_vadjustment()
        if adj is None or adj is self._vadj:
            return
        self._vadj = adj
        adj.connect("value-changed", self._on_scroll)
        self._on_scroll(adj)

    def _on_scroll(self, adj):
        if adj.get_value() > 0:
            self.view.add_css_class("scrolled")
        else:
            self.view.remove_css_class("scrolled")

    # -- sorting ---------------------------------------------------------
    def set_sort(self, key: str | None, descending: bool = False):
        """Show which column the list is sorted by (no re-sort: the page did that)."""
        self._quiet = True
        try:
            col = self.columns.get(key) if key else None
            self.view.sort_by_column(col, Gtk.SortType.DESCENDING if descending
                                     else Gtk.SortType.ASCENDING)
        finally:
            self._quiet = False

    def _on_sorter_changed(self, sorter, _change):
        if self._quiet or self.on_sort is None:
            return
        col = sorter.get_primary_sort_column() if hasattr(sorter, "get_primary_sort_column") \
            else None
        if col is None:
            return
        key = next((k for k, c in self.columns.items() if c is col), None)
        if key is not None:
            descending = sorter.get_primary_sort_order() == Gtk.SortType.DESCENDING
            GLib.idle_add(lambda: (self.on_sort(key, descending), False)[1])

    # -- width -------------------------------------------------------------
    def wrap(self, list_widget: Gtk.Widget) -> Gtk.Widget:
        """Watch ``list_widget`` (the scrolled view with its A–Z rail) for its width."""
        list_widget.set_vexpand(True)
        list_widget.add_css_class("table-holder")
        return SizeWatcher(list_widget, self._on_width)

    def set_hidden(self, keys):
        """Leave these columns out whatever the width (e.g. no data for them)."""
        keys = set(keys)
        if keys != self._hidden:
            self._hidden = keys
            self._on_width(self._width, force=True)

    def _on_width(self, width: int, force: bool = False):
        self._width = width
        narrow = 0 < width < NARROW
        (self.view.add_css_class if narrow else self.view.remove_css_class)("narrow")
        side = SIDE_NARROW if narrow else SIDE
        names = [n for n in choose(width - side, self.specs) if n not in self._hidden]
        if names == self.names and not force:
            return
        self.names = names
        for key, col in self.columns.items():
            col.set_visible(key in names)
        for cell in self._titles:
            cell.update()


__all__ = ["Spec", "TableView", "TitleCell", "SizeWatcher", "cell_label", "unparent_children"]
