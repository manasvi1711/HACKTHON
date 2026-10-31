"""Deterministic detection of conflicting quantitative statements across sources.

A *fact* is a number with a unit (MB, minutes, days, %, $ ...) plus the content words
of its sentence. Two facts from different documents are compared when they share a
unit family and at least two topical words, and both sentences relate to the question.

Classification:
* version_difference - both documents declare product versions and they differ: an
  explainable difference; the user is asked which version applies.
* contradiction      - same declared version (or no version information that would
  explain the difference): a genuine conflict; both sources are shown.

No source is silently preferred by rank, upload time or effective date.
Limitation: only numeric facts are compared deterministically; non-numeric
contradictions rely on the LLM (in anthropic mode) and are otherwise not detected.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .parsing import normalize_version
from .retrieval import Retrieved, stem, tokenize

UNIT_FAMILIES = {
    "kb": ("size", 1 / 1024), "mb": ("size", 1.0), "gb": ("size", 1024.0), "tb": ("size", 1024.0 ** 2),
    "second": ("duration", 1 / 60), "minute": ("duration", 1.0), "min": ("duration", 1.0), "hour": ("duration", 60.0),
    "day": ("duration", 1440.0), "week": ("duration", 10080.0), "month": ("duration", 43200.0),
    "%": ("percent", 1.0), "percent": ("percent", 1.0),
    "user": ("users", 1.0), "seat": ("users", 1.0), "request": ("requests", 1.0), "file": ("files", 1.0),
    "attempt": ("attempts", 1.0), "character": ("characters", 1.0),
}
NUM_RE = re.compile(
    r"(?P<cur>[$€£])?\s?(?P<num>\d+(?:[.,]\d+)?)\s?(?P<unit>kb|mb|gb|tb|%|percent|seconds?|minutes?|mins?|hours?|days?|"
    r"weeks?|months?|users?|seats?|requests?|files?|attempts?|characters?)?\b", re.I)
GENERIC = set("maximum minimum limit up allow allows allowed individual each per file size version product".split())


@dataclass
class Fact:
    chunk_id: str
    document_id: str
    title: str
    version: str | None
    effective_date: str | None
    family: str
    value: float
    raw: str
    sentence: str
    words: set


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n", text) if s.strip()]


def extract_facts(r: Retrieved) -> list[Fact]:
    facts = []
    for sent in _sentences(r.text):
        for m in NUM_RE.finditer(sent):
            unit = (m.group("unit") or "").lower()
            if m.group("cur"):
                family, mult = "money", 1.0
            elif unit:
                key = unit if unit in UNIT_FAMILIES else unit.rstrip("s")
                if key not in UNIT_FAMILIES:
                    continue
                family, mult = UNIT_FAMILIES[key]
            else:
                continue
            try:
                value = float(m.group("num").replace(",", ".")) * mult
            except ValueError:
                continue
            words = {w for w in tokenize(sent) if not w.isdigit() and w not in {"mb", "gb", "kb"}}
            facts.append(Fact(r.chunk_id, r.document_id, r.title, normalize_version(r.product_version),
                              r.effective_date, family, value, m.group(0).strip(), sent, words))
    return facts


# The deterministic detector only runs when the question asks about a quantity, so a
# numeric difference elsewhere in a retrieved passage is not reported as a conflict.
QUANTITY_CUE = re.compile(
    r"\b(how\s+(much|many|long|big|large|often|soon)|maximum|minimum|max|min|limit|limits|size|timeout|time\s*out|"
    r"duration|quota|cap|expir\w*|valid|price|cost|percent|rate|minutes?|hours?|days?|mb|gb|tb)\b", re.I)


def detect_conflicts(evidence: list[Retrieved], question: str) -> list[dict]:
    if not QUANTITY_CUE.search(question):
        return []
    q_terms = set(tokenize(question))
    q_terms |= {stem(w) for w in q_terms}
    facts = [f for r in evidence for f in extract_facts(r) if f.words & q_terms]
    conflicts: list[dict] = []
    seen: set = set()
    for i, a in enumerate(facts):
        for b in facts[i + 1:]:
            if a.document_id == b.document_id or a.family != b.family or abs(a.value - b.value) < 1e-9:
                continue
            shared = (a.words & b.words) - GENERIC
            if len(shared | ((a.words & b.words) & GENERIC)) < 2 or not shared:
                continue
            key = tuple(sorted([(a.document_id, a.value), (b.document_id, b.value)]))
            if key in seen:
                continue
            seen.add(key)
            if a.version and b.version and a.version != b.version:
                kind = "version_difference"
                desc = (f"The sources give different values for different product versions: "
                        f"version {a.version} states \"{a.raw}\" and version {b.version} states \"{b.raw}\".")
            else:
                kind = "contradiction"
                same = f" for the same declared version ({a.version})" if a.version and a.version == b.version else ""
                desc = (f"The sources disagree{same}: \"{a.raw}\" in {a.title} vs \"{b.raw}\" in {b.title}. "
                        "No documented precedence rule resolves this.")
            conflicts.append({
                "kind": kind,
                "description": desc,
                "facts": [
                    {"chunk_id": f.chunk_id, "document_id": f.document_id, "title": f.title, "version": f.version,
                     "effective_date": f.effective_date, "value": f.raw, "sentence": f.sentence}
                    for f in (a, b)
                ],
            })
    return conflicts


VERSION_IN_QUESTION = re.compile(r"\b(?:v|version\s*|release\s*)(\d+(?:\.\d+)*)\b", re.I)


def version_in_question(question: str) -> str | None:
    m = VERSION_IN_QUESTION.search(question)
    return normalize_version(m.group(1)) if m else None
