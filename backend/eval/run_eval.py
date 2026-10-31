"""Run the labelled evaluation against a fresh, isolated instance of the app.

    python backend/eval/run_eval.py                 # uses LLM_PROVIDER / ANTHROPIC_API_KEY from env (.env)
    LLM_PROVIDER=mock python backend/eval/run_eval.py

Writes backend/eval/results/<provider>_<split>.json and <provider>_summary.md.
All metrics are computed automatically from the labels; no human or model judging is
performed by this script (see README "Evaluation" for what each metric does and does
not show).
"""
from __future__ import annotations

import json
import math
import os
import re
import sys
import tempfile
from collections import Counter, defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="docqa-eval-")
os.environ["SEED_SAMPLE_DOCS"] = "true"
os.environ["SEED_TEST_FIXTURES"] = "true"   # adversarial fixtures are part of the eval collection
os.environ["QA_RATE_LIMIT_PER_MINUTE"] = "100000"

K_VALUES = (1, 3, 5)
RETRIEVAL_DEPTH = 10


def _rel(passage: dict, labels: list[list[str]]) -> bool:
    return any(passage["title"] == t and s.lower() in (passage["section"] or "").lower() for t, s in labels)


def _ndcg(rels: list[int], n_relevant: int, k: int) -> float:
    dcg = sum(r / math.log2(i + 2) for i, r in enumerate(rels[:k]))
    idcg = sum(1 / math.log2(i + 2) for i in range(min(n_relevant, k)))
    return dcg / idcg if idcg else 0.0


def _content_tokens(text: str) -> set[str]:
    from app.retrieval import tokenize
    return set(tokenize(text))


def _grounded(segment_text: str, cited_texts: list[str]) -> bool:
    """Automated groundedness proxy: >=80% of the segment's content tokens and every
    number must occur in the cited passages. Not a semantic entailment check."""
    seg = _content_tokens(segment_text)
    src = _content_tokens(" ".join(cited_texts))
    nums = set(re.findall(r"\d+(?:[.,]\d+)?", segment_text))
    src_nums = set(re.findall(r"\d+(?:[.,]\d+)?", " ".join(cited_texts)))
    overlap = len(seg & src) / len(seg) if seg else 1.0
    return overlap >= 0.8 and nums <= src_nums


def evaluate_case(client, headers, case, users, search_fn):
    from app import auth

    q = case["question"]
    user = auth.user_from_id(users[case["user"]])
    labels = case["relevant"]
    out = {"id": case["id"], "category": case["category"], "user": case["user"], "question": q,
           "expected_state": case["expected_state"]}

    # Retrieval metrics (authorized retrieval exactly as the app performs it).
    if labels:
        ranked = [{"title": r.title, "section": r.section, "document_id": r.document_id}
                  for r in search_fn(user, q, RETRIEVAL_DEPTH)]
        rels = [1 if _rel(p, labels) else 0 for p in ranked]
        # Each label (doc, section) counts once for recall even if split into several chunks.
        found_labels = lambda k: sum(any(_rel(p, [lab]) for p in ranked[:k]) for lab in labels)
        out["retrieval"] = {
            **{f"hit@{k}": int(any(rels[:k])) for k in K_VALUES},
            **{f"recall@{k}": found_labels(k) / len(labels) for k in K_VALUES},
            "mrr@10": next((1 / (i + 1) for i, r in enumerate(rels) if r), 0.0),
            "ndcg@5": _ndcg(rels, len(labels), 5),
            "top3": [f"{p['title']} > {p['section']}" for p in ranked[:3]],
        }

    resp = client.post("/api/ask", headers=headers[case["user"]], json={"question": q}).json()
    answer_text = "\n".join(s["text"] for s in resp["answer"])
    out.update(state=resp["state"], state_ok=resp["state"] == case["expected_state"],
               answer=answer_text, clarification=resp.get("clarification_question"),
               conflicts=[c["kind"] for c in resp["conflicts"]], security_flags=resp["security_flags"])

    facts = case["expected_facts"]
    if facts:
        hits = [f for f in facts if f.lower() in answer_text.lower()]
        out["fact_recall"] = len(hits) / len(facts)
        out["missing_facts"] = [f for f in facts if f not in hits]

    scope = json.dumps(resp) if case["category"] in ("authorization", "prompt_injection") else answer_text
    out["forbidden_hits"] = [f for f in case["forbidden"] if f.lower() in scope.lower()]

    cits = {c["number"]: c for c in resp["citations"]}
    if resp["answer"]:
        passages = {}
        for c in resp["citations"]:
            p = client.get(c["passage_url"], headers=headers[case["user"]]).json()
            passages[c["number"]] = p["text"]
        out["segments"] = len(resp["answer"])
        out["segments_with_citation"] = sum(1 for s in resp["answer"] if s["citations"])
        out["segments_grounded"] = sum(1 for s in resp["answer"]
                                       if _grounded(s["text"], [passages[n] for n in s["citations"]]))
        out["excerpts_verbatim"] = sum(1 for c in resp["citations"] if c["excerpt"] in passages[c["number"]]) / len(cits)
        if labels:
            rel_docs = {t for t, _ in labels}
            out["citation_doc_precision"] = sum(1 for c in cits.values() if c["title"] in rel_docs) / len(cits)
            out["citation_passage_precision"] = sum(
                1 for c in cits.values() if _rel({"title": c["title"], "section": c["section"]}, labels)) / len(cits)
            out["citation_doc_recall"] = len(rel_docs & {c["title"] for c in cits.values()}) / len(rel_docs)
    if case.get("expected_conflict"):
        out["conflict_ok"] = (case["expected_conflict"] in out["conflicts"]
                              and out.get("fact_recall", 0) == 1.0)
    return out


def _mean(vals):
    vals = [v for v in vals if v is not None]
    return round(sum(vals) / len(vals), 3) if vals else None


def summarize(results: list[dict]) -> dict:
    retr = [r["retrieval"] for r in results if "retrieval" in r]
    s = {"cases": len(results)}
    for key in [f"hit@{k}" for k in K_VALUES] + [f"recall@{k}" for k in K_VALUES] + ["mrr@10", "ndcg@5"]:
        s[f"retrieval_{key}"] = _mean([r[key] for r in retr])
    s["retrieval_cases"] = len(retr)
    s["state_accuracy"] = _mean([int(r["state_ok"]) for r in results])
    s["fact_recall"] = _mean([r.get("fact_recall") for r in results if "fact_recall" in r])
    seg = sum(r.get("segments", 0) for r in results)
    s["groundedness_proxy"] = round(sum(r.get("segments_grounded", 0) for r in results) / seg, 3) if seg else None
    s["segments_with_citation"] = round(sum(r.get("segments_with_citation", 0) for r in results) / seg, 3) if seg else None
    s["excerpts_verbatim"] = _mean([r.get("excerpts_verbatim") for r in results if "excerpts_verbatim" in r])
    s["citation_doc_precision"] = _mean([r.get("citation_doc_precision") for r in results if "citation_doc_precision" in r])
    s["citation_passage_precision"] = _mean([r.get("citation_passage_precision") for r in results if "citation_passage_precision" in r])
    s["citation_doc_recall"] = _mean([r.get("citation_doc_recall") for r in results if "citation_doc_recall" in r])
    s["forbidden_claim_cases"] = sum(1 for r in results if r["forbidden_hits"])
    conf = [r for r in results if "conflict_ok" in r]
    s["conflict_handling"] = f"{sum(r['conflict_ok'] for r in conf)}/{len(conf)}"
    for cat in ("ambiguous", "unanswerable", "partial"):
        sub = [r for r in results if r["category"] == cat]
        s[f"{cat}_state_ok"] = f"{sum(r['state_ok'] for r in sub)}/{len(sub)}"
    answerable = [r for r in results if r["expected_state"] != "not_found"]
    s["false_not_found"] = f"{sum(r['state'] == 'not_found' for r in answerable)}/{len(answerable)}"
    unanswerable = [r for r in results if r["expected_state"] == "not_found"]
    s["not_found_recall"] = f"{sum(r['state'] == 'not_found' for r in unanswerable)}/{len(unanswerable)}"
    by_cat = defaultdict(list)
    for r in results:
        by_cat[r["category"]].append(r["state_ok"])
    s["state_accuracy_by_category"] = {c: f"{sum(v)}/{len(v)}" for c, v in sorted(by_cat.items())}
    s["confusion"] = dict(Counter(f"{r['expected_state']} -> {r['state']}" for r in results))
    return s


def to_markdown(provider: str, model: str | None, embedding: str, summaries: dict, results: dict) -> str:
    lines = [f"# Evaluation results ({provider})", ""]
    if provider == "mock":
        lines += ["> **MOCK MODE.** Answers were produced by the deterministic extractive mock generator, not by a "
                  "language model. Retrieval, authorization, conflict-detection and state-logic metrics are real "
                  "measurements of this pipeline; answer-quality numbers describe the mock heuristic and are **not** "
                  "model performance.", ""]
    else:
        lines += [f"Generator: `{model}`", ""]
    lines += [f"Embedding backend: `{embedding}`", ""]
    keys = list(next(iter(summaries.values())).keys())
    lines += ["| Metric | " + " | ".join(summaries) + " |", "|---|" + "---|" * len(summaries)]
    for k in keys:
        if k in ("state_accuracy_by_category", "confusion"):
            continue
        lines.append(f"| {k} | " + " | ".join(str(summaries[s][k]) for s in summaries) + " |")
    for split, s in summaries.items():
        lines += ["", f"### State accuracy by category ({split})", "",
                  ", ".join(f"{c}: {v}" for c, v in s["state_accuracy_by_category"].items()), "",
                  f"Confusion (expected -> actual): " + "; ".join(f"`{k}`: {v}" for k, v in sorted(s["confusion"].items()))]
    lines += ["", "## Failure cases", ""]
    for split, rs in results.items():
        for r in rs:
            problems = []
            if not r["state_ok"]:
                problems.append(f"state {r['expected_state']} -> {r['state']}")
            if r.get("missing_facts"):
                problems.append(f"missing facts {r['missing_facts']}")
            if r["forbidden_hits"]:
                problems.append(f"forbidden content {r['forbidden_hits']}")
            if "retrieval" in r and not r["retrieval"]["hit@3"]:
                problems.append(f"no relevant passage in top 3 (got {r['retrieval']['top3']})")
            if problems:
                lines.append(f"- **{r['id']}** ({split}, {r['category']}) \"{r['question']}\": " + "; ".join(problems))
    return "\n".join(lines) + "\n"


def main() -> None:
    from fastapi.testclient import TestClient

    from app.config import settings
    from app.embeddings import get_embedder
    from app.main import app
    from app.retrieval import search

    provider = settings.effective_llm_provider
    users = {"emma": "u-emma", "eli": "u-eli", "sam": "u-sam", "hana": "u-hana", "adam": "u-adam"}
    out_dir = HERE / "results"
    out_dir.mkdir(exist_ok=True)
    with TestClient(app) as client:
        headers = {}
        for u in users:
            tok = client.post("/api/auth/login", json={"username": u, "password": settings.demo_password}).json()["token"]
            headers[u] = {"Authorization": f"Bearer {tok}"}
        summaries, all_results = {}, {}
        for split in ("dev", "heldout"):
            data = json.loads((HERE / f"dataset_{split}.json").read_text(encoding="utf-8"))
            results = [evaluate_case(client, headers, c, users, search) for c in data["cases"]]
            summaries[split] = summarize(results)
            all_results[split] = results
            (out_dir / f"{provider}_{split}.json").write_text(
                json.dumps({"provider": provider, "summary": summaries[split], "results": results}, indent=2), encoding="utf-8")
        md = to_markdown(provider, settings.anthropic_model if provider == "anthropic" else None,
                         get_embedder().name, summaries, all_results)
        (out_dir / f"{provider}_summary.md").write_text(md, encoding="utf-8")
        print(md)


if __name__ == "__main__":
    main()
