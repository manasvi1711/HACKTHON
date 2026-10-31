"""Section-aware chunking.

Rules:
* A chunk never spans two sections (heading paths), two PDF pages, or two documents.
* Blocks (paragraph, whole ordered list, table) are kept intact when they fit; an
  oversized block is split at sentence / line boundaries.
* Consecutive chunks inside one section overlap by up to `overlap` characters,
  taken from whole sentences/lines at the end of the previous chunk.
* The heading path is stored separately (used for display and prepended for
  embedding), so `text` remains an exact source excerpt for citations.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .parsing import Block


@dataclass
class Chunk:
    ordinal: int
    section: str
    section_anchor: str
    page: int | None
    text: str


_SENT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(])|\n")


def _split_units(text: str) -> list[str]:
    return [u.strip() for u in _SENT_RE.split(text) if u.strip()]


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:48] or "body"


def chunk_blocks(blocks: list[Block], size: int, overlap: int) -> list[Chunk]:
    groups: list[tuple[tuple[str, ...], int | None, list[Block]]] = []
    for b in blocks:
        key = (tuple(b.headings), b.page)
        if groups and (groups[-1][0], groups[-1][1]) == key:
            groups[-1][2].append(b)
        else:
            groups.append((key[0], key[1], [b]))

    chunks: list[Chunk] = []
    anchor_counts: dict[str, int] = {}
    for headings, page, group in groups:
        section = " > ".join(headings) if headings else ""
        base = _slug(headings[-1]) if headings else "body"
        pieces: list[str] = []  # units joined into chunks
        current: list[str] = []
        cur_len = 0
        fresh = 0  # units added since the last emitted chunk (excludes overlap carry)

        def emit():
            nonlocal current, cur_len, fresh
            if current and fresh:
                pieces.append("\n".join(current))
                fresh = 0
                # overlap: carry trailing units up to `overlap` chars
                carry, carried = [], 0
                for u in reversed(current):
                    if carried + len(u) > overlap:
                        break
                    carry.insert(0, u)
                    carried += len(u)
                current, cur_len = (carry, carried) if carry and len(carry) < len(current) else ([], 0)

        for b in group:
            units = [b.text] if len(b.text) <= size else _split_units(b.text)
            for u in units:
                while len(u) > size:  # pathological long sentence: hard split
                    head, u = u[:size], u[size:]
                    if fresh:
                        emit()
                    current, cur_len, fresh = [head], len(head), 1
                    emit()
                if fresh and cur_len + len(u) + 1 > size:
                    emit()
                current.append(u)
                cur_len += len(u) + 1
                fresh += 1
        emit()

        for piece in pieces:
            n = anchor_counts.get(base, 0) + 1
            anchor_counts[base] = n
            anchor = f"{base}-{n}" + (f"-p{page}" if page else "")
            chunks.append(Chunk(len(chunks), section, anchor, page, piece.strip()))
    return [c for c in chunks if c.text]
