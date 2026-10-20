"""Years from Kodi, cleaned (pure, no GTK)."""
from __future__ import annotations


def clean_year(value) -> int:
    """A usable year, else 0. Kodi reports some albums as year 1 (or 2, 19…) — a date
    tag it couldn't read — which showed as "1" instead of nothing."""
    try:
        y = int(str(value).strip()[:4] or 0)
    except (TypeError, ValueError):
        return 0
    return y if 1000 <= y <= 2999 else 0


def year_from_date(text) -> int:
    """'1992-05-01', '1992' → 1992; anything else → 0."""
    return clean_year((text or "")[:4]) if isinstance(text, str) else 0


__all__ = ["clean_year", "year_from_date"]
