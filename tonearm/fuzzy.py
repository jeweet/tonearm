"""fzf-style fuzzy matching.

Supports fzf's extended search syntax:

    foo      fuzzy match
    'foo     exact substring
    ^foo     prefix
    foo$     suffix
    !foo     must not contain
    a | b    either term

Smart case: all-lowercase patterns match case-insensitively. Accents are
ignored ("beyonce" finds "Beyoncé").
"""
from __future__ import annotations

import heapq
import unicodedata
from dataclasses import dataclass

SCORE_MATCH = 16
GAP_START = -3
GAP_EXT = -1
BONUS_BOUNDARY = 9
BONUS_CAMEL = 7
BONUS_CONSECUTIVE = 5
BONUS_FIRST_CHAR_MULT = 2
BONUS_EXACT_WORD = 12

_DELIMS = set(" /,:;|-_.()[]{}&+'\"!?·")


_FOLD: dict = {}  # ord(char) -> base char, grown lazily; used with str.translate (C speed)


def _fold_char(ch: str) -> str:
    d = unicodedata.normalize("NFKD", ch)
    base = "".join(c for c in d if not unicodedata.combining(c))
    return base[:1] if base else ch  # one char per input char → positions stay valid


def normalize(s: str) -> str:
    """Strip accents, keeping the length (so match positions map back to the original)."""
    if s.isascii():
        return s
    missing = {c for c in s if ord(c) > 127 and ord(c) not in _FOLD}
    for c in missing:
        _FOLD[ord(c)] = _fold_char(c)
    return s.translate(_FOLD)


def _bonus(text: str, i: int) -> int:
    if i == 0:
        return BONUS_BOUNDARY
    prev, cur = text[i - 1], text[i]
    if prev in _DELIMS or prev.isspace():
        return BONUS_BOUNDARY
    if prev.islower() and cur.isupper():
        return BONUS_CAMEL
    if not prev.isdigit() and cur.isdigit():
        return BONUS_CAMEL
    return 0


@dataclass
class Term:
    text: str
    kind: str  # fuzzy | exact | prefix | suffix
    inverse: bool = False
    case_sensitive: bool = False


def parse(query: str) -> list[list[Term]]:
    """Return AND-groups of OR-terms."""
    groups: list[list[Term]] = []
    tokens = query.split()
    i = 0
    current: list[Term] = []
    while i < len(tokens):
        tok = tokens[i]
        if tok == "|":
            i += 1
            continue
        inverse = tok.startswith("!") and len(tok) > 1
        if inverse:
            tok = tok[1:]
        kind = "fuzzy"
        if tok.startswith("'") and len(tok) > 1:
            kind, tok = "exact", tok[1:]
        elif tok.startswith("^") and len(tok) > 1:
            kind, tok = "prefix", tok[1:]
        elif tok.endswith("$") and len(tok) > 1:
            kind, tok = "suffix", tok[:-1]
        if inverse and kind == "fuzzy":
            kind = "exact"
        cs = tok != tok.lower()
        tok = normalize(tok)
        current.append(Term(tok if cs else tok.lower(), kind, inverse, cs))
        if i + 1 < len(tokens) and tokens[i + 1] == "|":
            i += 2
            continue
        groups.append(current)
        current = []
        i += 1
    if current:
        groups.append(current)
    return groups


def _fuzzy(text: str, low: str, pat: str, cs: bool):
    hay = text if cs else low
    # forward pass: is it a subsequence at all, and where does it end?
    idx = -1
    for ch in pat:
        idx = hay.find(ch, idx + 1)
        if idx < 0:
            return None
    end = idx
    # backward pass: tighten the window (fzf v1)
    j = len(pat) - 1
    start = end
    for k in range(end, -1, -1):
        if hay[k] == pat[j]:
            j -= 1
            if j < 0:
                start = k
                break
    # scoring pass within [start, end], greedy left-to-right
    score, positions = 0, []
    pi = 0
    consecutive = 0
    in_gap = False
    first_bonus = 0
    for k in range(start, end + 1):
        if pi < len(pat) and hay[k] == pat[pi]:
            b = _bonus(text, k)
            if consecutive == 0:
                first_bonus = b
            else:
                b = max(b, first_bonus, BONUS_CONSECUTIVE)
            if pi == 0:
                b *= BONUS_FIRST_CHAR_MULT
            score += SCORE_MATCH + b
            positions.append(k)
            consecutive += 1
            in_gap = False
            pi += 1
        else:
            score += GAP_EXT if in_gap else GAP_START
            in_gap = True
            consecutive = 0
    return score, positions


def _match_term(text: str, low: str, t: Term):
    hay = text if t.case_sensitive else low
    n = len(t.text)
    if t.kind == "fuzzy":
        return _fuzzy(text, low, t.text, t.case_sensitive)
    if t.kind == "exact":
        i = hay.find(t.text)
        if i < 0:
            return None
    elif t.kind == "prefix":
        stripped = len(hay) - len(hay.lstrip())
        if not hay.startswith(t.text, stripped):
            return None
        i = stripped
    else:  # suffix
        trimmed = hay.rstrip()
        if not trimmed.endswith(t.text):
            return None
        i = len(trimmed) - n
    bonus = _bonus(text, i) * BONUS_FIRST_CHAR_MULT
    end = i + n
    if end == len(hay) or hay[end] in _DELIMS or hay[end].isspace():
        bonus += BONUS_EXACT_WORD
    return SCORE_MATCH * n + bonus + BONUS_CONSECUTIVE * (n - 1), list(range(i, end))


def match(groups: list[list[Term]], text: str, low: str | None = None):
    """Score ``text`` against a parsed query. Returns (score, positions) or None."""
    if low is None:
        low = normalize(text).lower()
    total, positions = 0, set()
    for group in groups:
        best = None
        for t in group:
            m = _match_term(text if not t.case_sensitive else normalize(text), low, t)
            if t.inverse:
                if m is None:
                    best = (0, [])
                    break
                continue
            if m is not None and (best is None or m[0] > best[0]):
                best = m
        if best is None:
            return None
        total += best[0]
        positions.update(best[1])
    return total, sorted(positions)


class FuzzyIndex:
    """Pre-normalised haystacks for fast repeated queries."""

    def __init__(self, entries, key=lambda e: e):
        self.entries = list(entries)
        self._texts = [key(e) for e in self.entries]
        self._low = [normalize(t).lower() for t in self._texts]

    def __len__(self):
        return len(self.entries)

    def search(self, query: str, limit: int = 200, accept=None, cancelled=lambda: False):
        """Return up to ``limit`` (score, entry, positions), best first."""
        groups = parse(query)
        if not groups:
            return []
        heap: list = []
        texts, lows, entries = self._texts, self._low, self.entries
        # Typing narrows: if the new query only extends the previous plain one,
        # only re-check entries that matched before (like fzf does).
        candidates = range(len(entries))
        plain = not any(c in query for c in "|!^$'")
        last = getattr(self, "_last", None)
        if plain and last and last[0] and last[2] is accept and query.startswith(last[0]):
            candidates = last[1]
        matched = []
        for n, idx in enumerate(candidates):
            if n % 4096 == 0 and cancelled():
                return []
            e = entries[idx]
            if accept is not None and not accept(e):
                continue
            m = match(groups, texts[idx], lows[idx])
            if m is None:
                continue
            matched.append(idx)
            # tie-break: shorter haystacks first
            key = (m[0], -len(texts[idx]), -idx)
            if len(heap) < limit:
                heapq.heappush(heap, (key, idx, m[1]))
            elif key > heap[0][0]:
                heapq.heapreplace(heap, (key, idx, m[1]))
        self._last = (query, matched, accept) if plain else None
        heap.sort(reverse=True)
        return [(k[0], entries[i], pos) for k, i, pos in heap]
