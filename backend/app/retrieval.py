"""Hybrid retrieval: BM25 keyword scoring + dense cosine similarity, fused with
reciprocal-rank fusion (RRF), plus an exact-match boost for error codes.

Authorization is applied FIRST: candidate chunks are restricted to documents the user
may read before any scoring, so restricted passages are never scored, ranked,
returned, or sent to a model. BM25 corpus statistics are also computed over the
authorized subset only, so scores do not depend on restricted content.

All scores are ranking signals, not calibrated probabilities.
"""
from __future__ import annotations

import math
import re
import threading
from collections import Counter
from dataclasses import asdict, dataclass

import numpy as np

from .auth import User, readable_document_ids
from .db import get_conn, index_epoch
from .embeddings import get_embedder

STOPWORDS = set("""a an the and or but if of to in on at by for with from as is are was were be been being do does did
i me my we our you your it its this that these those there here what which who whom how when where why can could
should would will shall may might must not no yes so than then too very just about into over under up down out
any all some each more most other such only own same have has had having get got am please tell know""".split())

# Small, explicit domain lexicon for keyword-side query expansion (dense retrieval
# handles most paraphrases on its own). Keys and values are stemmed tokens.
SYNONYMS = {
    "forgot": ["reset", "recover"], "forget": ["reset", "recover"], "lost": ["reset", "recover"],
    "login": ["sign", "password", "account"], "log": ["sign"], "signin": ["sign"],
    "refund": ["refundabl", "non-refundabl"], "money": ["refund", "payment"], "back": ["refund"],
    "cancel": ["cancellation", "renewal"], "unsubscrib": ["cancel", "cancellation"],
    "big": ["size", "maximum"], "larg": ["size", "maximum"], "limit": ["maximum", "size"], "max": ["maximum"],
    "pay": ["payment", "billing"], "charg": ["payment", "billing"],
    "expir": ["expire", "expired"], "expiry": ["expire"], "valid": ["expire"], "last": ["expire"],
    "salary": ["compensation", "pay"], "bonus": ["compensation"],
    "key": ["api"], "token": ["key"],
    "recover": ["reset", "password", "lock"], "recovery": ["reset", "password", "lock"],
    "access": ["sign", "account"],
}


def stem(tok: str) -> str:
    for suf in ("ing", "ed", "es", "s", "e"):
        if len(tok) > 4 and tok.endswith(suf):
            return tok[: -len(suf)]
    return tok


_CONTRACTION_RE = re.compile(r"(\w)['’](t|s|m|re|ve|ll|d)\b")
# Multi-word access phrases whose words are otherwise stopwords or too generic.
_PHRASES = [(re.compile(p), r) for p, r in (
    (r"\b(get|got|getting)\s+(in|into|back\s+into)\b", "access"),
    (r"\b(log|logging|sign|signing)\s+(in|into|on)\b", "login"),
    (r"\blocked\s+out\b", "locked"),
)]


# Normalise synonym keys through the same stemmer used for tokens.
SYNONYMS = {stem(k): v for k, v in SYNONYMS.items()}


def tokenize(text: str) -> list[str]:
    # Drop contraction suffixes ("can't" -> "can", "I'm" -> "i") so they don't become fake terms.
    text = _CONTRACTION_RE.sub(r"\1", text.lower())
    for pattern, repl in _PHRASES:
        text = pattern.sub(repl, text)
    return [stem(t) for t in re.findall(r"[a-z0-9]+(?:-[a-z0-9]+)*", text)
            if t not in STOPWORDS and (len(t) > 1 or t.isdigit())]


ERROR_CODE_RE = re.compile(r"\b[A-Z]{1,3}-?\d{2,5}\b")


@dataclass
class Retrieved:
    chunk_id: str
    document_id: str
    title: str
    section: str
    section_anchor: str
    page: int | None
    text: str
    product: str | None
    product_version: str | None
    effective_date: str | None
    source_id: str
    injection_flag: bool
    dense_score: float
    bm25_score: float
    fused_score: float
    term_coverage: float
    exact_code_match: bool
    rank: int = 0

    def to_debug(self) -> dict:
        d = asdict(self)
        d["text"] = self.text[:600]
        return d


class Index:
    """In-memory snapshot of all indexed chunks, rebuilt when the index epoch changes."""

    def __init__(self) -> None:
        self.epoch = -1
        self.rows: list[dict] = []
        self.tokens: list[list[str]] = []
        self.matrix = np.zeros((0, 1), np.float32)
        self._lock = threading.Lock()

    def refresh(self) -> None:
        epoch = index_epoch()
        if epoch == self.epoch:
            return
        with self._lock:
            if epoch == self.epoch:
                return
            rows = get_conn().execute(
                "SELECT c.id, c.document_id, c.section, c.section_anchor, c.page, c.text, c.embedding, c.injection_flag, "
                "d.title, d.product, d.product_version, d.effective_date, d.source_id "
                "FROM chunks c JOIN documents d ON d.id = c.document_id "
                "WHERE d.status='ready' ORDER BY c.document_id, c.ordinal").fetchall()
            self.rows = [dict(r) for r in rows]
            self.tokens = [tokenize((r["section"] or "") + " " + r["text"]) for r in self.rows]
            vecs = [np.frombuffer(r["embedding"], dtype=np.float32) for r in self.rows if r["embedding"]]
            self.matrix = np.vstack(vecs) if vecs and len(vecs) == len(self.rows) else np.zeros((len(self.rows), 1), np.float32)
            self.epoch = epoch


_index = Index()


def get_index() -> Index:
    _index.refresh()
    return _index


def _bm25(query_terms: list[str], docs: list[list[str]], k1: float = 1.4, b: float = 0.75) -> np.ndarray:
    n = len(docs)
    if n == 0:
        return np.zeros(0)
    avgdl = sum(len(d) for d in docs) / n or 1.0
    df = Counter(t for d in docs for t in set(d))
    scores = np.zeros(n)
    tfs = [Counter(d) for d in docs]
    for t in set(query_terms):
        if t not in df:
            continue
        idf = math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5))
        w = query_terms.count(t)
        for i, tf in enumerate(tfs):
            f = tf.get(t, 0)
            if f:
                scores[i] += w * idf * f * (k1 + 1) / (f + k1 * (1 - b + b * len(docs[i]) / avgdl))
    return scores


def query_terms(question: str) -> tuple[list[str], list[str]]:
    base = tokenize(question)
    expanded = list(base)
    for t in base:
        expanded += [stem(s) for s in SYNONYMS.get(t, [])]
    return base, expanded


def term_coverage(base_terms: list[str], tokens: set[str]) -> float:
    if not base_terms:
        return 0.0
    hit = 0
    for t in set(base_terms):
        if t in tokens or any(stem(s) in tokens for s in SYNONYMS.get(t, [])):
            hit += 1
    return hit / len(set(base_terms))


def common_terms(user: User, min_fraction: float = 0.5) -> frozenset:
    """Terms that occur in at least `min_fraction` of the documents this user may read
    (computed over authorized documents only)."""
    idx = get_index()
    allowed = set(readable_document_ids(user))
    per_doc: dict[str, set] = {}
    for row, toks in zip(idx.rows, idx.tokens):
        if row["document_id"] in allowed:
            per_doc.setdefault(row["document_id"], set()).update(toks)
    if len(per_doc) < 3:
        return frozenset()
    counts = Counter(t for toks in per_doc.values() for t in toks)
    return frozenset(t for t, c in counts.items() if c / len(per_doc) >= min_fraction)


def search(user: User, question: str, k: int, *, document_ids: list[str] | None = None) -> list[Retrieved]:
    idx = get_index()
    allowed = set(readable_document_ids(user))  # authorization before scoring
    if document_ids is not None:
        allowed &= set(document_ids)
    cand = [i for i, r in enumerate(idx.rows) if r["document_id"] in allowed]
    if not cand or not question.strip():
        return []

    base, expanded = query_terms(question)
    bm = _bm25(expanded, [idx.tokens[i] for i in cand])
    if idx.matrix.shape[1] > 1:
        qv = get_embedder().embed_query(question)
        dense = idx.matrix[cand] @ qv if idx.matrix.shape[1] == qv.shape[0] else np.zeros(len(cand))
    else:
        dense = np.zeros(len(cand))

    from .conflicts import version_in_question  # local import: conflicts imports this module
    from .parsing import normalize_version

    asked_version = version_in_question(question)
    codes = {c.replace("-", "").lower() for c in ERROR_CODE_RE.findall(question)}
    rank_dense = np.argsort(-dense).argsort()
    rank_bm = np.argsort(-bm).argsort()
    results = []
    for j, i in enumerate(cand):
        r = idx.rows[i]
        toks = set(idx.tokens[i])
        exact = bool(codes) and any(c in toks for c in codes)
        fused = 1 / (60 + rank_dense[j]) + (1 / (60 + rank_bm[j]) if bm[j] > 0 else 0) + (0.05 if exact else 0)
        if asked_version and normalize_version(r["product_version"]) == asked_version:
            fused += 0.005  # version-aware ranking: prefer the version the user named
        results.append(Retrieved(
            chunk_id=r["id"], document_id=r["document_id"], title=r["title"], section=r["section"] or "",
            section_anchor=r["section_anchor"], page=r["page"], text=r["text"], product=r["product"],
            product_version=r["product_version"], effective_date=r["effective_date"], source_id=r["source_id"],
            injection_flag=bool(r["injection_flag"]), dense_score=round(float(dense[j]), 4),
            bm25_score=round(float(bm[j]), 4), fused_score=round(float(fused), 5),
            term_coverage=round(term_coverage(base, toks), 3), exact_code_match=exact))
    # Deterministic order: fused score, then dense score, then stable chunk id.
    results.sort(key=lambda x: (-x.fused_score, -x.dense_score, x.chunk_id))
    out = results[:k]
    for n, r in enumerate(out, 1):
        r.rank = n
    return out
