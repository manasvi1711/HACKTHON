# Evaluation Report

**Run date:** 2026-10-31
**Generator:** MOCK MODE (deterministic extractive, no LLM). No Anthropic API key was available.
**Embeddings:** `fastembed:BAAI/bge-small-en-v1.5` (local).
**Reproduce:** `python backend/eval/run_eval.py`. Raw results are in `backend/eval/results/`.

## 1. What was and was not measured

| Measured for real | Not measured |
|---|---|
| Retrieval quality (hit@k, recall@k, MRR, nDCG), with authorization applied exactly as in production | **Claude answer quality.** The Claude path has passed contract tests against a stub client only. Run the script with `ANTHROPIC_API_KEY` set to produce `anthropic_*` results |
| Authorization outcomes, forbidden-content leakage, prompt-injection handling | Human or LLM-judged faithfulness, relevance or helpfulness. None was performed |
| Conflict detection and classification, response-state logic, citation validity and verbatim excerpts | Latency and cost |
| The mock generator's answer extraction and state heuristics, reported as **mock** results, not model performance | Robustness on large or real enterprise corpora. The collection is 9 small fictional documents |

## 2. Dataset construction

The cases were written by hand against the fictional sample collection: 6 trusted documents plus 3 adversarial fixtures, all active during evaluation.

| Split | Cases | Use |
|---|---|---|
| `dataset_dev.json` | 22 | Used while building: thresholds, mock heuristics, and the bugs found |
| `dataset_heldout.json` | 20 | Written **before** the first evaluation run. It uses new phrasings and covers sections the dev set does not (sharing, E201/E305, lockout + MFA, file types, pricing, sync, storage) |

Coverage per split (dev / held-out):

| Category | Dev | Held-out |
|---|---|---|
| direct | 4 | 3 |
| paraphrase | 2 | 2 |
| error_code | 2 | 2 |
| multi_passage | 2 | 2 |
| ambiguous | 2 | 1 |
| version_sensitive | 2 | 2 |
| contradiction | 1 | 1 |
| partial | 2 | 2 |
| unanswerable | 2 | 2 |
| authorization | 2 | 2 |
| prompt_injection | 1 | 1 |

Each case records:
- the question;
- the user, which fixes the role;
- a short context note;
- `relevant`: the complete list of relevant (document, section) passages;
- `expected_facts`: strings that must appear in the answer;
- `expected_state`;
- `expected_conflict`, where applicable;
- `forbidden`: strings that must not appear. For authorization and injection cases, this is checked against the *entire* response, including citations and excerpts.

The relevance labels are complete for this tiny collection: every section that answers the question is listed. That is why recall@k can be reported.

## 3. Metric definitions (all automated)

| Metric | Definition | Judgement method |
|---|---|---|
| hit@k | At least one labelled relevant passage appears in the top k (k = 1, 3, 5) of authorized retrieval | Automated: label match |
| recall@k | Fraction of labelled (document, section) pairs found in the top k | Automated |
| MRR@10, nDCG@5 | Rank of the first relevant passage; graded ranking with binary relevance | Automated |
| state_accuracy | Response state equals the expected state | Automated |
| fact_recall (completeness) | Fraction of expected fact strings present in the answer text | Automated string match. Strict: a correct paraphrase would count as a miss |
| groundedness_proxy | Fraction of answer segments where ≥80% of content tokens and all numbers occur in the cited passages | Automated **proxy**, not semantic entailment. Mock answers are verbatim, so they score 1.0 by construction |
| segments_with_citation | Fraction of answer segments with at least one valid citation | Automated |
| excerpts_verbatim | Fraction of citation excerpts that are exact substrings of the cited passage, fetched through the permission-checked endpoint | Automated |
| citation_doc / passage_precision | Fraction of citations pointing to a labelled relevant document / (document, section) | Automated |
| citation_doc_recall (coverage) | Fraction of labelled relevant documents that were cited | Automated |
| forbidden_claim_cases | Cases where any forbidden string appeared | Automated |
| conflict_handling | The expected conflict kind was detected **and** all conflicting values appear in the answer | Automated |
| false_not_found / not_found_recall | Not-found returned for answerable questions / for unanswerable ones | Automated |

Answer *relevance* is approximated by fact_recall together with state accuracy. No human or LLM relevance judgements were made.

## 4. Results

### 4.1 First run: blind held-out result (headline)

Saved verbatim in `backend/eval/results/run1_blind/`.

| Metric | dev | held-out (blind) |
|---|---|---|
| retrieval hit@1 / hit@3 / hit@5 | 1.00 / 1.00 / 1.00 | 1.00 / 1.00 / 1.00 |
| retrieval recall@1 / recall@3 / recall@5 | 0.912 / 1.00 / 1.00 | 0.875 / 1.00 / 1.00 |
| MRR@10 / nDCG@5 | 1.00 / 1.00 | 1.00 / 0.995 |
| state_accuracy | 0.955 (21/22) | **0.55 (11/20)** |
| fact_recall | 0.971 | 0.906 |
| groundedness_proxy | 1.00 | 1.00 |
| segments_with_citation / excerpts_verbatim | 1.00 / 1.00 | 1.00 / 1.00 |
| citation doc precision / passage precision | 0.971 / 0.804 | 0.896 / 0.854 |
| citation doc recall | 1.00 | 1.00 |
| forbidden_claim_cases | 0 | 0 |
| conflict_handling | 2/2 | 2/2 |
| ambiguous / partial / unanswerable state correct | 2/2 · 2/2 · 2/2 | 1/1 · 2/2 · **0/2** |
| false_not_found | 0/19 | 0/17 |

### 4.2 Second run: after failure analysis (held-out is **no longer blind**)

After inspecting the held-out failures, I fixed two defects that were general rather than specific to those cases:

- **Corpus-common terms.** Terms present in most of the user's authorized documents (e.g. the product name "Lumora") counted as coverage, so "Who is the CEO of Lumora?" looked partially answered. Such terms are now ignored. When fewer than half of the remaining informative terms are covered, the response is `not_found`.
- **Over-eager conflict detection.** The conflict detector fired on questions that were not about a quantity: "what are the steps to upload a file" raised the 20 MB / 50 MB version conflict. It now runs only for quantity questions.

After user testing, I also fixed a tokenizer bug: contractions such as "can't" and "I'm" produced the fake terms "t" and "m". I added "recover/recovery" and "get into / log in / locked out" normalisation, which came from a user-reported question, not from the held-out set. I also fixed a reproducibility bug. An exact reciprocal-rank-fusion tie (v2 wins BM25, v3 wins dense) was broken by random document-ID order. Ranking is now version-aware and deterministic. Two consecutive runs gave identical metrics.

| Metric | dev | held-out (after analysis) |
|---|---|---|
| retrieval hit@1 / recall@3 / MRR@10 / nDCG@5 | 1.00 / 1.00 / 1.00 / 1.00 | 1.00 / 1.00 / 1.00 / 0.995 |
| state_accuracy | 0.955 | 0.65 (13/20) |
| fact_recall | 0.971 | 0.844 |
| citation doc precision / passage precision | 0.971 / 0.804 | 0.889 / 0.844 |
| forbidden_claim_cases | 0 | 0 |
| conflict_handling | 2/2 | 2/2 |
| ambiguous / partial / unanswerable state correct | 2/2 · 2/2 · 2/2 | 1/1 · 1/2 · 2/2 |
| false_not_found | 0/19 | 1/17 |

The fix traded one partial case (H12) for two unanswerable cases (H13, H14). Treat the blind 0.55 as the honest held-out estimate for the mock generator.

### 4.3 Security evaluation

Run with `pytest backend/tests/test_security.py`. All tests pass, and the full backend suite passes 50/50. Across both splits, authorization and prompt-injection cases had **0 forbidden-content hits**:
- no HR canary;
- no salary figures for non-HR users;
- no injected instruction text;
- no "refunds are approved".

What each test shows, and does not prove, is documented in the test docstrings and in README §9.

## 5. Failure analysis (current run)

| Case | Expected → actual | Cause |
|---|---|---|
| D04, H02, H05, H09, H19 | answered → partially_answered | The mock's partial heuristic flags question words absent from the evidence ("charged", "stopped working", "getting", "handle"). This is **mock-specific**: lexical coverage cannot tell that a paraphrase is answered. An LLM generator decides this semantically |
| H06, H07 | answered → partially_answered | Same cause, on multi-passage questions. H07 also misses "Executable files are blocked". The "Supported file types" passage was retrieved, but the mock's sentence budget went to higher-scoring upload passages, including the irrelevant v2/v3 size sentences, so it was never quoted |
| H02, D07 | missing fact | Extractive selection picked the procedure list rather than the "expire after 30 minutes" sentence (H02), or vice versa (D07) |
| H12 | partially_answered → not_found | The stricter informative-term rule: "cost", "phone" and "support" are uncovered, so it falls below 50% coverage. The price *is* retrievable (hit@1 = 1) |
| Demo 7 (`docs/DEMONSTRATIONS.md`) | — | The mock also quoted an unrelated "expired API key" sentence for a reset-link question: lexical overlap on "expire". Shows the mock's citation-precision limit (passage precision 0.80–0.85) |

**Retrieval** was not the bottleneck on this collection: hit@3 = 1.0 and recall@3 = 1.0 on both splits. Almost every error is in the answer-composition and state heuristics, which a real LLM is meant to replace.

## 6. Limitations

- **Tiny, synthetic corpus** (9 documents, ~33 chunks) and 42 cases. Perfect retrieval here does not predict performance on thousands of overlapping documents. Confidence intervals would be wide: one case is 5% of the held-out split.
- **Authors wrote both cases and documents**, which risks easy cases. The held-out split was written before the first run but by the same person.
- **String-match metrics** under-credit correct paraphrases and can over-credit answers that contain the right string in the wrong context.
- **The groundedness proxy is lexical,** not entailment. With an LLM, add human or LLM-judge faithfulness scoring, with judge prompts and agreement checks reported separately.
- **The thresholds** (`MIN_DENSE_SCORE` = 0.62, coverage 0.67) were chosen on dev data for this embedding model. They must be re-tuned for other models or corpora.
- **No results from the LLM path.** The evaluation must be rerun with credentials before any claim about Claude answer quality is made.
