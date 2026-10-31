"""Question-answering orchestrator: retrieve -> assess evidence -> detect conflicts ->
generate -> validate citations/numbers -> decide response state."""
from __future__ import annotations

import hashlib
import json
import re
import threading
import uuid
from collections import OrderedDict

from .audit import audit, now_iso
from .auth import User
from .config import settings
from .conflicts import detect_conflicts, version_in_question
from .db import get_conn, index_epoch
from .generation import Draft, MockGenerator, get_generator
from .parsing import normalize_version
from .retrieval import Retrieved, common_terms, search, tokenize
from .security import detect_injection

NOT_FOUND_MESSAGE = "I could not find that information in the documents available to you."
GENERATION_ERRORS = {
    "model_api_error_401": "The answer service rejected the API key (HTTP 401). Check ANTHROPIC_API_KEY on the server, "
                           "or leave it empty to use mock mode.",
    "model_api_error_403": "The answer service denied access (HTTP 403). Check the API key's permissions.",
    "model_rate_limited": "The answer service is rate-limiting requests. Please retry shortly.",
    "model_unreachable": "The answer service could not be reached (network error).",
    "model_refused": "The model declined to answer this request.",
    "model_output_truncated": "The model's answer was cut off before completion.",
    "model_output_invalid": "The model returned an answer in an unexpected format.",
}
NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)?")


# --------------------------------------------------------------- per-user answer cache
class AnswerCache:
    """Key includes user id and the global index epoch, so entries are never shared
    between users and are invalidated by any upload/ACL/approval/deletion change."""

    def __init__(self, size: int = 256):
        self._d: OrderedDict = OrderedDict()
        self._size = size
        self._lock = threading.Lock()

    @staticmethod
    def key(user: User, question: str, k: int) -> str:
        norm = " ".join(question.lower().split())
        raw = json.dumps([user.id, user.role, index_epoch(), settings.effective_llm_provider, norm, k])
        return hashlib.sha256(raw.encode()).hexdigest()

    def get(self, key: str):
        with self._lock:
            if key in self._d:
                self._d.move_to_end(key)
                return self._d[key]
        return None

    def put(self, key: str, value) -> None:
        with self._lock:
            self._d[key] = value
            self._d.move_to_end(key)
            while len(self._d) > self._size:
                self._d.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._d.clear()


answer_cache = AnswerCache()


# --------------------------------------------------------------- evidence assessment
def is_relevant(r: Retrieved) -> bool:
    """Evidence-sufficiency gate (documented in README). A passage counts as evidence
    if it is semantically close, matches most query terms lexically, or contains the
    exact error code asked about."""
    return (r.exact_code_match
            or r.dense_score >= settings.min_dense_score
            or (r.bm25_score > 0 and r.term_coverage >= max(settings.min_term_coverage, 0.67)))


def best_excerpt(text: str, claim: str) -> str:
    """Return the sentence(s) of the passage that best overlap the claim, verbatim."""
    sents = [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n", text) if s.strip()]
    if not sents:
        return text[:300]
    ct = set(tokenize(claim))
    scored = sorted(range(len(sents)), key=lambda i: -len(ct & set(tokenize(sents[i]))))
    chosen = sorted(scored[:2]) if len(ct & set(tokenize(sents[scored[0]]))) else [0]
    return " ".join(sents[i] for i in chosen)[:500]


def _numbers(text: str) -> set[str]:
    return {n.replace(",", ".") for n in NUMBER_RE.findall(text)}


def validate_draft(draft: Draft, labels: dict[str, Retrieved]) -> tuple[list[dict], list[str]]:
    """Drop citations to passages not retrieved for this request; drop segments left
    without valid citations; drop segments containing numbers absent from their cited
    passages (a common hallucination pattern). Returns (segments, warnings)."""
    out, warnings = [], []
    for seg in draft.segments:
        cits = [c for c in dict.fromkeys(seg.get("citations", [])) if c in labels]
        invalid = [c for c in seg.get("citations", []) if c not in labels]
        if invalid:
            warnings.append(f"Removed {len(invalid)} citation(s) that did not refer to retrieved passages.")
        if not cits:
            warnings.append("Removed a statement that had no valid supporting citation.")
            continue
        support = " ".join(labels[c].text + " " + labels[c].section for c in cits)
        unsupported_nums = _numbers(seg["text"]) - _numbers(support) - {"1", "2", "3", "4", "5", "6", "7", "8", "9"}
        if unsupported_nums:
            warnings.append("Removed a statement containing numbers not found in its cited evidence.")
            continue
        out.append({"text": seg["text"].strip(), "citations": cits})
    return out, warnings


def _citation_payload(label: str, r: Retrieved, n: int, claims: list[str]) -> dict:
    # Prefer a cited statement that appears verbatim in the passage; otherwise the
    # passage sentences that best overlap the statements. Never invented text.
    verbatim = next((c for c in claims if c and c in r.text), None)
    excerpt = verbatim[:500] if verbatim else best_excerpt(r.text, " ".join(claims))
    return {
        "label": label, "number": n, "chunk_id": r.chunk_id, "document_id": r.document_id,
        "title": r.title, "section": r.section or None, "page": r.page, "section_anchor": r.section_anchor,
        "product_version": r.product_version, "effective_date": r.effective_date,
        "source_id": r.source_id, "excerpt": excerpt,
        "passage_url": f"/api/passages/{r.chunk_id}", "download_url": f"/api/documents/{r.document_id}/download",
        "injection_flag": r.injection_flag,
    }


# --------------------------------------------------------------- main entry
def answer_question(user: User, question: str, *, top_k: int | None = None, debug: bool = False,
                    request_id: str | None = None) -> dict:
    k = max(1, min(top_k or settings.retrieval_top_k, settings.retrieval_max_k))
    cache_key = AnswerCache.key(user, question, k)
    cached = answer_cache.get(cache_key)
    if cached is not None:
        result = json.loads(cached)
        result["cached"] = True
        if not debug:
            result.pop("debug", None)
        return result

    flags = detect_injection(question)
    retrieved = search(user, question, k)
    notes: list[str] = []
    asked_version = version_in_question(question)
    evidence = [r for r in retrieved if is_relevant(r)]
    if asked_version:
        excluded = [r for r in evidence if r.product_version and normalize_version(r.product_version) != asked_version]
        evidence = [r for r in evidence if r not in excluded]
        if excluded:
            notes.append(f"The question names version {asked_version}; passages explicitly for other versions were excluded.")

    result: dict = {
        "id": str(uuid.uuid4()), "question": question, "state": "not_found", "answer": [],
        "message": None, "missing_information": [], "clarification_question": None, "conflicts": [],
        "citations": [], "warnings": [], "generator": None,
        "mock_mode": settings.effective_llm_provider == "mock", "security_flags": [], "cached": False,
        "generation_error": None,
    }
    if flags:
        result["security_flags"].append("question_contains_instruction_like_text")
    if any(r.injection_flag for r in evidence):
        result["security_flags"].append("evidence_contains_instruction_like_text")
        result["warnings"].append("Some supporting text contains embedded instructions; they were treated as document content and not followed.")

    base_terms = tokenize(question)
    if not evidence:
        result["message"] = NOT_FOUND_MESSAGE
    elif len(base_terms) <= 1 and not any(r.exact_code_match for r in evidence):
        topics = list(dict.fromkeys(f"{r.title} - {r.section}" if r.section else r.title for r in evidence))[:4]
        result["state"] = "clarification_needed"
        result["clarification_question"] = (
            "Your question is quite broad. Which topic do you mean? For example: " + "; ".join(topics) + ".")
    else:
        conflicts = detect_conflicts(evidence, question)
        labels = {f"P{i}": r for i, r in enumerate(evidence, 1)}
        for c in conflicts:
            notes.append(c["description"])
        gen = get_generator()
        draft = gen.generate(question, list(labels.items()), notes, common_terms=common_terms(user))
        result["generator"] = draft.generator
        if draft.error:
            result["generation_error"] = GENERATION_ERRORS.get(draft.error, f"The language model request failed ({draft.error}).")
            result["warnings"].append(f"Answer generation failed ({draft.error}); no unsupported answer was produced.")
        segments, warnings = validate_draft(draft, labels)
        result["warnings"] += warnings
        by_chunk = {r.chunk_id: lab for lab, r in labels.items()}

        # Make sure every conflicting fact is cited, whatever the generator wrote.
        for c in conflicts:
            for f in c["facts"]:
                lab = by_chunk[f["chunk_id"]]
                if not any(lab in s["citations"] for s in segments):
                    ver = f" (version {f['version']})" if f["version"] else ""
                    segments.append({"text": f"{f['title']}{ver}: {f['sentence']}", "citations": [lab]})

        # Assign display numbers in order of first citation.
        numbering: dict[str, int] = {}
        for s in segments:
            for c in s["citations"]:
                numbering.setdefault(c, len(numbering) + 1)
        result["answer"] = [{"text": s["text"], "citations": [numbering[c] for c in s["citations"]]} for s in segments]
        result["citations"] = [_citation_payload(lab, labels[lab], n,
                                                 [s["text"] for s in segments if lab in s["citations"]])
                               for lab, n in numbering.items()]
        result["conflicts"] = [
            {"kind": c["kind"], "description": c["description"],
             "citations": [numbering[by_chunk[f["chunk_id"]]] for f in c["facts"]],
             "facts": [{"value": f["value"], "title": f["title"], "version": f["version"]} for f in c["facts"]]}
            for c in conflicts
        ]
        result["missing_information"] = draft.missing_information
        result["clarification_question"] = draft.clarification_question

        version_diffs = [c for c in conflicts if c["kind"] == "version_difference"]
        contradictions = [c for c in conflicts if c["kind"] == "contradiction"]
        if not segments:
            result["state"] = "clarification_needed" if draft.state == "clarification_needed" and draft.clarification_question else "not_found"
            if result["state"] == "not_found":
                # A failed model call is an error, not evidence that the documents lack the answer.
                result["message"] = result["generation_error"] or NOT_FOUND_MESSAGE
        elif contradictions:
            result["state"] = "conflicting_evidence"
            result["message"] = "The documents contain conflicting statements. Both are shown with their sources; please confirm with the document owner."
        elif version_diffs and not asked_version:
            versions = sorted({f["version"] for c in version_diffs for f in c["facts"] if f["version"]})
            result["state"] = "clarification_needed"
            result["clarification_question"] = (
                f"The answer depends on the product version ({', '.join('v' + v for v in versions)}). "
                "Which product version are you using?")
        elif draft.state == "conflicting_evidence" and len({labels[c].document_id for s in segments for c in s["citations"]}) >= 2:
            result["state"] = "conflicting_evidence"
        elif draft.state in ("answered", "partially_answered", "clarification_needed"):
            result["state"] = draft.state
        else:
            result["state"] = "answered"
        if result["state"] == "partially_answered" and not result["missing_information"]:
            result["missing_information"] = ["Part of the question is not covered by the available documents."]
        if isinstance(gen, MockGenerator):
            result["warnings"].append("MOCK MODE: answer text is extracted verbatim from evidence by a deterministic "
                                      "heuristic, not generated by a language model.")

    weak = [r for r in evidence if r.dense_score < settings.min_dense_score + 0.05 and not r.exact_code_match]
    if evidence and len(weak) == len(evidence) and result["state"] in ("answered", "partially_answered"):
        result["warnings"].append("Evidence is weak: the supporting passages are only loosely related to the question.")

    result["debug"] = {
        "top_k": k, "evidence_count": len(evidence), "asked_version": asked_version, "notes": notes,
        "thresholds": {"min_dense_score": settings.min_dense_score, "min_term_coverage": settings.min_term_coverage},
        "retrieved": [dict(r.to_debug(), used_as_evidence=r in evidence) for r in retrieved],
    }
    audit("qa.ask", user_id=user.id, request_id=request_id, decision=result["state"],
          retrieved_documents=sorted({r.document_id for r in retrieved}),
          cited_documents=sorted({c["document_id"] for c in result["citations"]}),
          security_flags=result["security_flags"])
    payload = json.dumps(result)
    if not result["generation_error"]:  # never cache transient failures
        answer_cache.put(cache_key, payload)
    get_conn().execute("INSERT INTO qa_history(id, user_id, created_at, question, response_json) VALUES (?,?,?,?,?)",
                       (result["id"], user.id, now_iso(), question, payload))
    if not debug:
        result = dict(result)
        result.pop("debug")
    return result
