"""Columns of the album / artist list views (no GTK, so it is unit-testable).

Each column has a fixed width (covers, numbers) or shares the free room (title,
artist, genre…), and needs a minimum list width to be shown at all: as the list gets
narrower they drop out, the least important first (ui/listview.py lays them out).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable


@dataclass
class Spec:
    key: str
    px: int | None = None         # fixed width, or None: share the free room
    need: int = 0                 # shown when the row is at least this wide
    title: str = ""               # header text ("" for the cover / ⋯ cells)
    value: Callable | None = None  # text in the cell
    meta: Callable | None = None   # text for the subtitle when the column is hidden
    xalign: float = 0.0
    numeric: bool = False
    sortable: bool = True


def choose(width: int, specs: Iterable[Spec]) -> list[str]:
    """The columns that fit a row ``width`` px wide, in display order."""
    return [s.key for s in specs if width >= s.need]
