# Evaluation results (mock)

> **MOCK MODE.** Answers were produced by the deterministic extractive mock generator, not by a language model. Retrieval, authorization, conflict-detection and state-logic metrics are real measurements of this pipeline; answer-quality numbers describe the mock heuristic and are **not** model performance.

Embedding backend: `fastembed:BAAI/bge-small-en-v1.5 (local)`

| Metric | dev | heldout |
|---|---|---|
| cases | 22 | 20 |
| retrieval_hit@1 | 1.0 | 1.0 |
| retrieval_hit@3 | 1.0 | 1.0 |
| retrieval_hit@5 | 1.0 | 1.0 |
| retrieval_recall@1 | 0.912 | 0.875 |
| retrieval_recall@3 | 1.0 | 1.0 |
| retrieval_recall@5 | 1.0 | 1.0 |
| retrieval_mrr@10 | 1.0 | 1.0 |
| retrieval_ndcg@5 | 1.0 | 0.995 |
| retrieval_cases | 17 | 16 |
| state_accuracy | 0.955 | 0.55 |
| fact_recall | 0.971 | 0.906 |
| groundedness_proxy | 1.0 | 1.0 |
| segments_with_citation | 1.0 | 1.0 |
| excerpts_verbatim | 1.0 | 1.0 |
| citation_doc_precision | 0.971 | 0.896 |
| citation_passage_precision | 0.804 | 0.854 |
| citation_doc_recall | 1.0 | 1.0 |
| forbidden_claim_cases | 0 | 0 |
| conflict_handling | 2/2 | 2/2 |
| ambiguous_state_ok | 2/2 | 1/1 |
| unanswerable_state_ok | 2/2 | 0/2 |
| partial_state_ok | 2/2 | 2/2 |
| false_not_found | 0/19 | 0/17 |
| not_found_recall | 3/3 | 1/3 |

### State accuracy by category (dev)

ambiguous: 2/2, authorization: 2/2, contradiction: 1/1, direct: 4/4, error_code: 2/2, multi_passage: 2/2, paraphrase: 1/2, partial: 2/2, prompt_injection: 1/1, unanswerable: 2/2, version_sensitive: 2/2

Confusion (expected -> actual): `answered -> answered`: 12; `answered -> partially_answered`: 1; `clarification_needed -> clarification_needed`: 3; `conflicting_evidence -> conflicting_evidence`: 1; `not_found -> not_found`: 3; `partially_answered -> partially_answered`: 2

### State accuracy by category (heldout)

ambiguous: 1/1, authorization: 2/2, contradiction: 1/1, direct: 2/3, error_code: 1/2, multi_passage: 0/2, paraphrase: 0/2, partial: 2/2, prompt_injection: 1/1, unanswerable: 0/2, version_sensitive: 1/2

Confusion (expected -> actual): `answered -> answered`: 5; `answered -> clarification_needed`: 1; `answered -> partially_answered`: 6; `clarification_needed -> clarification_needed`: 2; `conflicting_evidence -> conflicting_evidence`: 1; `not_found -> not_found`: 1; `not_found -> partially_answered`: 2; `partially_answered -> partially_answered`: 2

## Failure cases

- **D04** (dev, paraphrase) "Will I keep getting charged after I cancel?": state answered -> partially_answered
- **D07** (dev, multi_passage) "How do I reset my password and how long is the reset link valid?": missing facts ['Forgot password']
- **H02** (heldout, paraphrase) "My password reset email link stopped working, why?": state answered -> partially_answered; missing facts ['30 minutes']
- **H03** (heldout, paraphrase) "Is there a way to get my money back for this month's subscription?": state answered -> partially_answered
- **H05** (heldout, error_code) "Getting E305 from the API": state answered -> partially_answered
- **H06** (heldout, multi_passage) "How long do locked accounts stay locked, and is multi-factor authentication required?": state answered -> partially_answered
- **H07** (heldout, multi_passage) "Which file types can I upload, and what are the steps to upload a file?": state answered -> clarification_needed; missing facts ['Executable files are blocked']
- **H09** (heldout, version_sensitive) "On release 3, what's the per-file upload limit?": state answered -> partially_answered
- **H13** (heldout, unanswerable) "What is the maximum number of users in a workspace?": state not_found -> partially_answered
- **H14** (heldout, unanswerable) "Who is the CEO of Lumora?": state not_found -> partially_answered
- **H19** (heldout, direct) "Can the sync client handle file names longer than 255 characters?": state answered -> partially_answered
