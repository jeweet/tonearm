"""Keyboard shortcuts overview (Adw.ShortcutsDialog on libadwaita ≥ 1.8,
Gtk.ShortcutsWindow before that)."""
from __future__ import annotations

from xml.sax.saxutils import escape

from gi.repository import Adw, Gtk

SHORTCUTS = [
    ("General", [
        ("Search", "<Ctrl>f <Ctrl>k"),
        ("Sync library changes", "<Ctrl>r"),
        ("Preferences", "<Ctrl>comma"),
        ("Keyboard shortcuts", "<Ctrl>question"),
        ("Quit", "<Ctrl>q"),
    ]),
    ("Playback", [
        ("Play / pause", "space <Ctrl>space"),
        ("Next track", "<Ctrl>Right"),
        ("Previous track", "<Ctrl>Left"),
        ("Volume up", "<Ctrl>Up"),
        ("Volume down", "<Ctrl>Down"),
        ("Open the player", "<Ctrl>p"),
    ]),
    ("Search", [
        ("Move into the results", "Down"),
        ("Open the first result", "Return"),
        ("Close the results", "Escape"),
    ]),
    ("Navigation", [
        ("Go back", "<Alt>Left"),
    ]),
]


def show_shortcuts(parent: Gtk.Widget):
    if hasattr(Adw, "ShortcutsDialog"):
        dialog = Adw.ShortcutsDialog()
        for title, items in SHORTCUTS:
            section = Adw.ShortcutsSection(title=title)
            for label, accel in items:
                section.add(Adw.ShortcutsItem(title=label, accelerator=accel))
            dialog.add(section)
        dialog.present(parent)
        return

    groups = []
    for title, items in SHORTCUTS:
        rows = "".join(
            '<child><object class="GtkShortcutsShortcut">'
            f'<property name="title">{escape(label)}</property>'
            f'<property name="accelerator">{escape(accel)}</property>'
            '</object></child>' for label, accel in items)
        groups.append('<child><object class="GtkShortcutsGroup">'
                      f'<property name="title">{escape(title)}</property>{rows}</object></child>')
    xml = ('<interface><object class="GtkShortcutsWindow" id="win"><property name="modal">1</property>'
           '<child><object class="GtkShortcutsSection"><property name="section-name">main</property>'
           f'{"".join(groups)}</object></child></object></interface>')
    builder = Gtk.Builder.new_from_string(xml, -1)
    win = builder.get_object("win")
    win.set_transient_for(parent.get_root())
    win.present()
