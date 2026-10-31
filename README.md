# Enterprise Documentation Q&A Assistant

A secure retrieval-augmented generation (RAG) assistant for the HCLTech Future Ready AI Engineer Hackathon.

Employees upload manuals, FAQs, release notes and support articles in PDF, HTML, Markdown or TXT, then ask questions in natural language. Answers:

- use only passages the signed-in user is authorized to read;
- cite each statement;
- show contradictions and version differences instead of resolving them silently;
- ask for clarification, or say the information was not found, when the evidence is insufficient.

The assistant is **read-only**. It never resets passwords, rotates keys, sends messages, or acts on instructions found in documents.

> **Status: what is real and what is mocked.**
>
> **Real and tested:**
> - ingestion of all four formats;
> - section-aware chunking;
> - local semantic embeddings (`BAAI/bge-small-en-v1.5`) combined with BM25 hybrid retrieval;
> - authorization, citation validation, conflict detection, response states and every security control;
> - the React UI;
> - the evaluation harness.
>
> **Mocked:** no Anthropic API key was available while this was built. Answer *wording* was therefore produced by the clearly labelled **MOCK MODE**, a deterministic extractive generator that copies evidence sentences verbatim. Set `ANTHROPIC_API_KEY` to a real key to switch to Claude (placeholder values are ignored) (`claude-opus-5-5` by default). The Claude integration is covered by contract tests against a stub client, but **it has not been run against the live API**, and no model-quality results are claimed. See [docs/EVALUATION_REPORT.md](docs/EVALUATION_REPORT.md).

All sample content is fictional ("Lumora Workspace"). All users are synthetic.

---

## 1. Quick start

**Prerequisites:**
- Python 3.11+ (developed on 3.13).
- Node.js 18+ only for building the UI. If Node isn't installed, `pip install nodejs-wheel` provides `node`/`npm` inside the virtualenv, which is how this repo was built.

```bash
# 1. Backend environment
python -m venv .venv
# Windows: .venv\Scripts\activate      macOS/Linux: source .venv/bin/activate
pip install -r backend/requirements-dev.txt

# 2. Configuration
cp .env.example .env                    # set DEMO_PASSWORD; optionally ANTHROPIC_API_KEY

# 3. Frontend build (served by the backend at http://localhost:8000)
cd frontend && npm install && npm run build && cd ..

# 4. Run
cd backend && uvicorn app.main:app --host 127.0.0.1 --port 8000
# open http://localhost:8000  - sign in as emma / sam / hana / adam / eli with DEMO_PASSWORD
```

**Windows PowerShell 5.1** doesn't support `&&`. Use these commands instead (with `.venv` activated, `npm` comes from the venv's `nodejs-wheel`):

```powershell
python -m venv .venv; .venv\Scripts\Activate.ps1
pip install -r backend/requirements-dev.txt
Copy-Item .env.example .env              # then edit DEMO_PASSWORD
cd frontend; npm install; npm run build; cd ..
cd backend; uvicorn app.main:app --host 127.0.0.1 --port 8000
```

On first start:
- The six trusted sample documents are ingested automatically.
- The embedding model (~130 MB) is downloaded once into `.models/`.
- To also load the adversarial fixtures (prompt-injection document and two contradictory documents), start with `SEED_TEST_FIXTURES=true` on an empty `DATA_DIR`.

**UI development with hot reload:** run `npm run dev` in `frontend/` (http://localhost:5173, proxies `/api` to port 8000).

### Validation commands

```bash
python -m pytest backend/tests -q                          # 54 backend tests (API, retrieval, security)
cd frontend && npm run typecheck && npm test && cd ..      # TypeScript check + 5 UI tests
python backend/scripts/security_check.py                   # 30 live PASS/FAIL security probes against a running server
python backend/eval/run_eval.py                            # evaluation -> backend/eval/results/
python backend/scripts/run_demos.py                        # regenerates docs/DEMONSTRATIONS.md
python backend/scripts/make_sample_pdfs.py                 # regenerates the sample PDFs
```

The eval and demo scripts use whatever generator is configured: mock without a key, Claude with one. They label their output accordingly.

### Demo accounts (synthetic)

| User | Role | Can read | Can upload |
|---|---|---|---|
| `emma`, `eli` | employee | general docs | no |
| `sam` | sales | general docs | yes; needs admin approval |
| `hana` | hr | general docs + HR-only | yes; needs admin approval |
| `adam` | admin | everything; approves, sets permissions, replaces, deletes, reads the audit log | yes; auto-approved |

---

## 2. Architecture and data flow

```
 Browser (React + TS)                         FastAPI backend (Python)
 ───────────────────                          ─────────────────────────────────────────────────────────────
 Login ──────── POST /api/auth/login ───────▶ auth.py  pbkdf2 password check, opaque bearer token (sha256 stored)
 Upload ─────── POST /api/documents ────────▶ documents.ingest
                                                1 size limit → content sniffing (security.sniff_file_type)
                                                2 parse in a separate worker process with timeout (parsing.py)
                                                3 secret scan → quarantine/reject   4 prompt-injection screen → hold for review
                                                5 section-aware chunks (chunking.py) → local embeddings (embeddings.py)
                                                6 SQLite: document row + chunks + vectors; file saved as <doc_id>.<ext>
                                                7 index epoch++ (invalidates in-memory index and answer cache); audit
 Ask ────────── POST /api/ask ──────────────▶ qa.answer_question
                                                a authorized doc IDs for this user (deny by default)
                                                b hybrid search over authorized chunks only (retrieval.py)
                                                c evidence gate (thresholds) → not_found / clarification
                                                d deterministic conflict detection (conflicts.py)
                                                e generator: Claude (structured JSON) or MOCK (extractive)
                                                f validate: citation IDs ∈ retrieved set, numbers ∈ cited text
                                                g decide state; build citations; per-user cache + history; audit
 Citation ───── GET /api/passages/{id} ─────▶ permission check → passage text (404 if not readable)
 Download ───── GET /api/documents/{id}/download ─▶ permission + integrity check → attachment + sandbox CSP
```

**Storage.**
- SQLite (`backend/data/app.db`) holds users, sessions, documents, version history, chunks with float32 embedding blobs, Q&A history, and the audit log.
- Original files are stored under generated names in `backend/data/files/`.
- The retrieval index is an in-memory snapshot rebuilt from SQLite whenever the global `index_epoch` changes. SQLite is the persistent index.

### Technology choices and trade-offs

| Choice | Why | Trade-off |
|---|---|---|
| FastAPI + SQLite | Small, typed, no extra services for a local prototype | Single node. Move to Postgres plus a vector store (pgvector, OpenSearch) for multi-instance use |
| BM25 (own code) + `bge-small-en-v1.5` via `fastembed` (ONNX) | Hybrid retrieval handles exact error codes *and* paraphrases. Runs locally, so no document text goes to an embedding provider, and no torch is needed | Brute-force cosine is fine for thousands of chunks, not millions. A small English model |
| Reciprocal-rank fusion (k=60) | No score calibration needed between BM25 and cosine | Scores are ranks, not probabilities. Exact ties are broken deterministically |
| Claude via the official `anthropic` SDK with JSON-schema output | Structured `segments[{text, citations}]` can be validated mechanically | Needs network and credentials. Output still has to be validated (it is) |
| Deterministic conflict detector + model | Version conflicts never depend on the model noticing them | Only numeric facts are detected deterministically |
| React + Vite + TypeScript, served by FastAPI | One origin in production (strict CSP, no CORS needed) | Needs a build step |

---

## 3. Supported formats and limitations

| Format | Parser | What is kept | Limitations |
|---|---|---|---|
| PDF | `pypdf` text extraction | Page numbers, heading heuristics, ordered lists. Repeated headers, footers and "Page n of m" removed | **No OCR.** Scanned or image-only PDFs are rejected with an explanatory message (also shown in the UI). Encrypted PDFs are rejected. Headings are inferred from line shape. Complex layouts and multi-column text may extract out of order |
| HTML | BeautifulSoup `html.parser` (inert parsing) | Heading hierarchy, paragraphs, lists, tables as `Header: value` rows, `<meta>` version/date | `script`, `style`, `nav`, `header`, `footer`, `aside`, `form` and `iframe` are dropped. Scripts never run and no external resources are fetched |
| Markdown | Line-based parser | Front matter (`version`, `effective_date`, `title`, `product`), heading hierarchy, lists, tables, code blocks | No Markdown rendering. Text stays as written (e.g. `**bold**` markers) |
| TXT (UTF-8) | Line-based parser | Underlined or ALL-CAPS headings, numbered steps | Heading detection is heuristic |

**Metadata policy.**
- Version, effective date and product are taken only from explicit sources: front matter, `<meta>` tags, `Version: N` / `Effective date: YYYY-MM-DD` lines, or the upload form. Otherwise they stay empty; nothing is inferred from file names or upload time.
- Non-PDF formats get stable section anchors (`password-reset-1`), never page numbers.

**Failure handling.** Each failure gets its own error code, and the HTTP status is shown in brackets:

| Failure | Code |
|---|---|
| Unsupported extension | `unsupported_type` [415] |
| Extension/content mismatch | `type_mismatch` [415] |
| Empty file | `empty` |
| Malformed file | `malformed` |
| Encrypted PDF | `encrypted` |
| No text layer | `no_text` |
| Too large (bytes, pages or extracted characters) | `too_large` [413] |
| Parse timeout | `timeout` |
| Identical content already present | `duplicate` [409] |
| Secret detected, under the `reject` policy | `secret_detected` |

Failures are audited, and nothing is indexed for a failed upload.

---

## 4. Ingestion, chunking, indexing and retrieval

**Chunking** ([backend/app/chunking.py](backend/app/chunking.py)):
- Blocks (paragraph, an entire ordered list, a table) are grouped by heading path and PDF page.
- A chunk **never crosses a section, page or document boundary**, so different documents and versions are never mixed.
- Blocks are packed up to `CHUNK_SIZE_CHARS` (default 900). Oversized blocks split at sentence or line boundaries.
- Consecutive chunks in the same section overlap by up to `CHUNK_OVERLAP_CHARS` (default 150) of whole sentences.
- The heading path is stored separately and prepended only for embedding, so `chunks.text` is always an exact source excerpt for citations.
- Each chunk inherits its document's permissions through a join at query time. Permissions are not copied, so they cannot go stale.

Limitations:
- A chunk can reach size + overlap characters.
- Very long single sentences are hard-split.
- Heading detection in PDF and TXT is heuristic.
- A table split across PDF pages is split into two chunks.

**Retrieval** ([backend/app/retrieval.py](backend/app/retrieval.py)):
1. **Authorization first.** The candidate set is restricted to chunks of documents the user may read: `ready` and `approved` status, with the user's role in `allowed_roles`. BM25 statistics are computed over that subset only.
2. **BM25** over stemmed tokens. A small, explicit domain synonym list expands the keyword side, for example *forgot → reset* and *refund → non-refundable*.
3. **Dense cosine** between the query (bge query prefix) and chunk vectors.
4. **Reciprocal-rank fusion.** On top of the fused rank:
   - an exact error-code boost (e.g. `E104`);
   - a small boost for documents whose version matches a version named in the question (`v3`, `version 3`, `release 3`).
5. `top_k` is configurable per request (1–20, default 6). The UI's *Show retrieval debug* option lists every authorized retrieved passage with its dense, BM25, fused and term-coverage scores, and whether it was used as evidence.

**Evidence-sufficiency gate.** A passage counts as evidence if any one of these holds:
- it contains the exact error code asked about;
- `dense ≥ MIN_DENSE_SCORE` (0.62);
- `BM25 > 0` and query-term coverage ≥ 0.67.

The thresholds were set on the dev split only. These scores are ranking signals, not probabilities, and no confidence percentage is ever displayed.

---

## 5. Model and embedding configuration

All settings are environment variables; see [.env.example](.env.example).

| Variable | Default | Meaning |
|---|---|---|
| `LLM_PROVIDER` | `auto` | `anthropic`, `mock`, or `auto` (anthropic if `ANTHROPIC_API_KEY` is set) |
| `ANTHROPIC_MODEL` | `claude-opus-5-5` | Any Claude model ID |
| `LLM_EFFORT` / `LLM_MAX_TOKENS` / `LLM_TIMEOUT_SECONDS` | `low` / 4000 / 60 | Bounded model requests |
| `LLM_SERVER_FALLBACKS` | `true` | Server-side fallback if a request is declined by safety classifiers (Claude API only; set `false` on Bedrock, Vertex or Foundry) |
| `EMBEDDING_PROVIDER` / `EMBEDDING_MODEL` | `fastembed` / `BAAI/bge-small-en-v1.5` | Local embeddings. `hashing` is a lexical fallback that is **not semantic**; `/api/health` reports which is active |

**What leaves the application.** Only in `anthropic` mode, each question sends the following to the Anthropic API:
- the question;
- the authorized evidence passages selected for it;
- their titles, sections, versions and dates;
- the fixed system prompt.

Embeddings are computed locally. Restricted passages are never sent. Credentials stay in backend environment variables; they are never returned by the API, logged, or included in the frontend bundle.

**Generation contract** ([backend/app/generation.py](backend/app/generation.py)):
- The system prompt, the evidence and the question are kept in separate, XML-escaped sections.
- The model must return JSON matching a schema: `state`, `segments[{text, citations}]`, `missing_information`, `clarification_question`, `conflict_notes`.
- Refusals, truncation, invalid JSON and API errors all become `not_found` with a warning. No unvalidated text is ever shown.

---

## 6. Citation generation

1. Evidence passages are labelled `P1…Pn` for the generator.
2. Every returned citation label must refer to a passage retrieved **for this request**. Unknown labels are removed.
3. A segment left with no valid citation is removed.
4. A segment containing a number (other than 1–9) that does not appear in its cited passages is removed, which guards against invented limits. Each removal produces a warning.
5. Surviving labels are renumbered `[1]…[n]` in order of first use.

Each citation carries:
- document title and ID;
- section path;
- PDF page, if known;
- version and effective date, if known;
- the stable section anchor;
- the source identifier (`upload://<doc>/<file>`);
- an excerpt: the cited statement if it appears **verbatim** in the passage, otherwise the passage's best-overlapping sentences, also verbatim.

Citations link to `GET /api/passages/{chunk_id}` and `GET /api/documents/{id}/download`, and **both re-check permissions on every request**. Excerpts are therefore never fabricated, and page numbers exist only for PDFs.

---

## 7. Conflict and fallback behaviour

**Conflict detection** ([backend/app/conflicts.py](backend/app/conflicts.py)) runs only when the question asks about a quantity (*how long, maximum, size, limit, timeout*, …). It works as follows:
1. Extract number+unit facts from the evidence: sizes, durations, percentages, money, counts.
2. Compare facts from **different documents** that share a unit family and topical words.

| Situation | Classification | Response state |
|---|---|---|
| Both documents declare different versions (v2: 20 MB, v3: 50 MB) and the question names no version | `version_difference` (explainable) | `clarification_needed`. Both values are shown with citations and the user is asked *"Which product version are you using?"* |
| The question names a version | Passages explicitly for other versions are excluded (noted in the debug view) | Normal answer |
| Same declared version (or no version info) with different values | `contradiction` | `conflicting_evidence`. Both are shown with citations and the user is told to confirm with the document owner |

**No source-precedence policy is applied.** Neither retrieval rank, upload time nor effective date silently picks a winner. Any conflicting fact the generator did not cite is appended with its citation, so conflicts stay visible.

**Response states.**

| State | When |
|---|---|
| `answered` | Every statement is supported by cited evidence |
| `partially_answered` | Some of the question is supported. `missing_information` lists the rest (*"The documents state that reset links expire after 30 minutes, but do not say whether that can be extended."*) |
| `clarification_needed` | Question too broad (≤1 content term), or version-dependent with no version given |
| `conflicting_evidence` | Genuine contradiction |
| `not_found` | *"I could not find that information in the documents available to you."* This is identical whether the information doesn't exist or exists in documents the user can't read, so restricted content isn't revealed |

If every evidence passage is only marginally above the gate, a "weak evidence" warning is added.

**Mock mode only.** Partial and not-found decisions use query-term coverage, ignoring terms common to most authorized documents. This is a lexical heuristic, and its weaknesses are measured in the evaluation report.

---

## 8. Authentication and authorization (SEC-01)

**Local demo authentication.**
- Synthetic users, PBKDF2-SHA256 password hashes, and a shared `DEMO_PASSWORD`.
- Opaque random bearer tokens. Only their SHA-256 is stored, with an 8-hour expiry and server-side revocation on logout.
- The token is kept in `sessionStorage`.
- **Production limitations:** there is no IdP, MFA, password policy, account lockout or token rotation. In production, replace `auth.authenticate`/sessions with OIDC/SAML and map IdP groups to roles; the authorization layer stays the same.

**Authorization.**
- Deny by default; the role is always read from the server-side user record. Client-supplied role fields or headers are ignored, and tests prove it.
- Documents carry `allowed_roles`. Admins can read everything.
- A document is searchable only when it is `ready` **and** `approved`.

Checks are enforced at each of these points:

| Action | Rule |
|---|---|
| Upload | Admin, HR or Sales only. Non-admins must keep their own role in the ACL, and their uploads wait for approval |
| Search, answer | Authorized-subset retrieval |
| Citation, preview, download | Per-request check. Returns 404 rather than 403, so the document's existence isn't confirmed |
| Permissions, approval, replacement, deletion, integrity check, audit log | Admin only. Uploaders may delete their own unapproved uploads |

**Leak prevention.**
- Restricted content is excluded before scoring, so its titles, excerpts and scores never appear in answers, debug views, errors or the model request.
- The answer cache key includes the user ID, role and the global index epoch. Every upload, ACL change, approval, replacement or deletion bumps the epoch, so stale answers are never served.
- Q&A history is per user.

---

## 9. Security controls and remaining risks

| Area | Controls implemented | Remaining risks / not implemented |
|---|---|---|
| SEC-02 Prompt injection | Questions and evidence are treated as untrusted. They are kept in separate escaped sections, and the system prompt forbids following embedded instructions. Regex screening flags instruction-like text in questions and chunks. New documents with flagged chunks need admin approval. Flagged passages are marked in the model request and the UI, and mock mode never echoes flagged sentences. Permissions and capabilities are enforced outside the model; there are no tools, URL fetching or code execution | Screening is pattern-based and bypassable. Prompting reduces but **does not eliminate** injection risk with a real LLM. The model could still be misled about *authorized* content, though it can never reach unauthorized content |
| SEC-03 Uploads | Extension allow-list plus content sniffing. Limits on bytes, pages, extracted characters and parse time; parsing runs in a separate process that is killed on timeout (memory rlimit on POSIX only). Generated storage names prevent path traversal. HTML is parsed inertly. Downloads are forced attachments with `CSP: sandbox`. The UI renders all document text as React text nodes (no `dangerouslySetInnerHTML`). Unsafe link schemes are rejected | **No malware scanning.** Production should add AV/CDR scanning before ingestion. No memory limit on Windows. pypdf parsing hostile PDFs is a residual risk |
| SEC-04 Secrets | Regex detection (AWS keys, private keys, `sk-` keys, GitHub/Slack tokens, JWTs, `password=…`). Default policy **quarantine**: stored, never indexed, visible only to admins, not downloadable. Alternative policy `reject`. Only secret *types* are logged | Detection is imperfect: novel formats and obfuscated values will be missed, and false positives are possible |
| SEC-05 Provenance | Uploader, timestamp, source ID, version, approval status, approver and SHA-256 are recorded. `baseline_hash` is set on admin approval. Downloads verify the hash, and tampering returns 409. Replacement is admin-only and keeps `document_versions` history. Deletion removes chunks and files in the same operation | A hash shows a file changed since it was approved; it says nothing about whether the content is accurate or safe. The DB itself must be protected (file permissions, encrypted volume, backups) |
| SEC-06 API/UI | Pydantic schemas and length limits (question ≤1000 chars, top_k ≤20). Per-user Q&A and upload rate limits; per-IP login rate limit. Bounded model requests. Parameterised SQL only. Explicit CORS allow-list. Strict CSP, `X-Frame-Options`, `nosniff`, `no-store` on API responses. Generic 500 errors with a request ID | Rate limits are in-memory (single process). Bearer tokens in `sessionStorage` could be stolen by an XSS bug (mitigated by CSP). **Local HTTP only: deploy behind TLS** (HSTS at the proxy) |
| SEC-07 Audit | Logged: request ID, user, action, resource, decision, retrieved and cited document IDs, security flags, upload, replacement and deletion events, failures. Never logged: tokens, passwords or document text. The audit log is admin-only. Retention is `AUDIT_RETENTION_DAYS` (default 90), purged at startup | Deleted documents keep a metadata row (`status=deleted`) for audit; content and chunks are removed. Q&A history (which includes answers) has no automatic retention limit yet |

**SEC-08 security tests** are in [backend/tests/test_security.py](backend/tests/test_security.py). Each docstring states what the test shows and what it does not prove.

| Requirement | Test |
|---|---|
| Sales cannot retrieve HR-only information | `test_sales_cannot_retrieve_hr_only_information`, `test_debug_view_contains_only_authorized_passages` |
| Restricted content does not appear in the model request | `test_restricted_content_never_reaches_generator`, `test_anthropic_request_contract_and_validation` |
| Direct access to restricted citations or downloads is denied | `test_direct_access_to_restricted_citations_downloads_previews_denied` |
| Document-based and direct injection | `test_document_based_prompt_injection_not_followed`, `test_direct_prompt_injection_in_question`, `test_injection_fixture_requires_admin_approval` |
| HTML scripts do not execute | `test_html_scripts_navigation_not_indexed`, UI test `never interprets document text as HTML` |
| Unsupported, oversized and malformed files | `test_unsupported_and_mismatched_types_rejected`, `test_oversized_upload_rejected`, `test_empty_malformed_encrypted_and_scanned_pdfs_fail_safely`, `test_parse_timeout_kills_hung_parser` |
| Dummy secrets | `test_secret_quarantined_and_not_searchable`, `test_secret_reject_policy` |
| Cache and session isolation | `test_cached_answers_and_history_isolated_between_users`, `test_logout_revokes_session` |
| Permission changes and deletion | `test_permission_change_and_deletion_remove_stale_access`, `test_replacement_leaves_no_stale_chunks` |
| Rate and input limits | `test_rate_limits_and_input_limits` |

These tests do **not** prove:
- resistance to a determined attacker;
- absence of timing side channels;
- LLM robustness to novel injections;
- correctness of the parsers on adversarial PDFs.

---

## 10. Evaluation

See [docs/EVALUATION_REPORT.md](docs/EVALUATION_REPORT.md) for measured results, failure analysis and limitations. In summary:
- [backend/eval/dataset_dev.json](backend/eval/dataset_dev.json) has 22 cases; [backend/eval/dataset_heldout.json](backend/eval/dataset_heldout.json) has 20.
- Both cover direct, paraphrase, error-code, multi-passage, ambiguous, version-sensitive, contradiction, partial, unanswerable, authorization and prompt-injection cases.
- Each case records the question, the user and role context, labelled relevant passages, expected facts, the expected state, and forbidden claims.
- All metrics are computed automatically by [backend/eval/run_eval.py](backend/eval/run_eval.py). No human or LLM judging was performed.

---

## 11. Assumptions and design trade-offs

- **Roles over per-user ACLs.** Roles were sufficient for the brief. The ACL column could hold groups as well.
- **Admins can read every document,** including quarantined ones, for review. Stricter separation of duties (approvers who cannot read HR content) is a possible extension.
- **Non-admin uploads require approval.** This trades speed for integrity of the shared collection.
- **404 rather than 403** for documents the user cannot read. This hides existence but makes debugging harder.
- **Duplicate detection is global,** so the response reveals that identical content exists somewhere. The message never names the other document.
- **Single-turn Q&A.** History is stored per user for display, but it is not fed back into retrieval, which avoids conversational leakage and drift.
- **Answers are cached per user.** Responses are deterministic in mock mode; LLM answers are cached until any collection change.
- **Mock mode exists only for demonstration** when credentials are unavailable. Its answer-quality results are reported separately and never presented as model performance.

---

## 12. Requirements traceability

| Req | Implementation | Tests / evidence |
|---|---|---|
| FR-01 Multi-format ingestion | `parsing.py` (PDF/HTML/MD/TXT, explicit metadata), `documents.ingest` (hash, status, uploader, ACL, duplicates, scanned-PDF detection), `POST /api/documents`, UI `DocumentsPanel` | `test_ingestion.py`: `test_all_sample_formats_ingested_with_metadata`, `test_non_pdf_formats_have_no_fabricated_pages`, `test_empty_malformed_encrypted_and_scanned_pdfs_fail_safely`, `test_duplicate_detected` |
| FR-02 Content preparation | `parsing.py` (noise removal, tables, lists), `chunking.py` (section/page-bounded, size/overlap configurable) | `test_chunking_retrieval.py`: `test_chunks_never_cross_sections_or_pages`, `test_size_and_overlap`, `test_ordered_list_kept_intact…`; `test_pdf_pages_and_sections_preserved_and_noise_removed` |
| FR-03 Retrieval | `retrieval.py` (authorized subset → BM25 + dense + RRF, error-code and version boosts, top_k), debug view in `/api/ask` and the UI | `test_paraphrase_retrieves_password_reset`, `test_exact_error_code_match`, `test_top_k_configurable`, `test_debug_view_contains_only_authorized_passages`; eval hit@k / recall@k / MRR / nDCG |
| FR-04 Grounded answers | `generation.py` (prompt, schema, mock), `qa.validate_draft` (citation IDs, numbers), evidence gate | `test_validate_draft_drops_fabricated_citations_and_numbers`, `test_partially_answerable`, `test_anthropic_request_contract_and_validation`; eval groundedness proxy |
| FR-05 Citations | `qa._citation_payload`, `/api/passages/{id}`, `/api/documents/{id}/download`, UI `AnswerView` | `test_direct_answer_with_verifiable_citation`, `test_every_segment_has_valid_citation`, UI citation test; eval citation precision, recall, verbatim |
| FR-06 Conflicts | `conflicts.py` (`version_difference` vs `contradiction`, version filter), `qa` state logic, UI conflict box | `test_version_difference_asks_for_clarification_and_shows_both`, `test_version_specified_is_answered_without_other_version`, `test_genuine_contradiction_same_version`; eval conflict handling |
| FR-07 Confidence and fallback | `qa.answer_question` states, `is_relevant` thresholds, weak-evidence warning, no percentages | `test_not_found_without_hints`, `test_ambiguous_question_requests_clarification`, `test_model_refusal_or_bad_output_falls_back_to_not_found`; eval state accuracy |
| FR-08 Evaluation | `backend/eval/` datasets (dev and held-out), `run_eval.py`, results JSON and markdown | [docs/EVALUATION_REPORT.md](docs/EVALUATION_REPORT.md) |
| SEC-01 AuthN/AuthZ | `auth.py`, `retrieval.search` (authorized subset), `visible_doc_or_404`, per-user cache key + epoch | `test_sales_cannot…`, `test_authentication_required_and_client_role_ignored`, `test_cached_answers…`, `test_permission_change…` |
| SEC-02 Prompt injection | `security.detect_injection`, `generation.SYSTEM_PROMPT` + escaped sections, approval hold, flags | `test_document_based_prompt_injection_not_followed`, `test_direct_prompt_injection_in_question`, `test_prompt_separates_roles_and_escapes_evidence` |
| SEC-03 Secure uploads | `security.sniff_file_type`, `documents.parse_with_limits` (subprocess + timeout), limits, generated filenames, inert HTML, sandboxed downloads | `test_unsupported…`, `test_oversized…`, `test_extracted_text_limit`, `test_parse_timeout_kills_hung_parser`, `test_path_traversal_filename_neutralised`, `test_only_authorized_roles_publish` |
| SEC-04 Secrets | `security.detect_secrets`, `SECRET_POLICY`, `.env.example`, server-only key | `test_secret_quarantined_and_not_searchable`, `test_secret_reject_policy`, `test_audit_log_restricted_and_free_of_secrets` |
| SEC-05 Provenance | `documents` columns, `baseline_hash` on approval, `verify_integrity`, `document_versions`, admin-only replace and approve | `test_non_admin_upload_requires_approval_before_search`, `test_integrity_check_detects_tampering`, `test_replacement_leaves_no_stale_chunks` |
| SEC-06 API/UI protection | Pydantic limits, `RateLimiter`, CORS allow-list, security headers and CSP, `safeHref`, React text rendering | `test_rate_limits_and_input_limits`, `test_security_headers_cors_and_safe_download`, UI tests `never interprets…`, `rejects unsafe link schemes` |
| SEC-07 Audit and retention | `audit.py`, `GET /api/audit` (admin), `AUDIT_RETENTION_DAYS` purge | `test_audit_log_restricted_and_free_of_secrets`, `test_direct_access…` (denials audited) |
| SEC-08 Security evaluation | `backend/tests/test_security.py` (and the security tests in `test_ingestion.py`) | Table in §9 |

---

## 13. Repository layout

```
backend/app/        config, db, auth, security, parsing, chunking, embeddings, retrieval, conflicts, generation, qa, documents, audit, main
backend/tests/      pytest suite (54 tests)
backend/eval/       labelled datasets, run_eval.py, results/ (run1_blind/ = first blind held-out run)
backend/scripts/    make_sample_pdfs.py, run_demos.py
sample_docs/        trusted/ (6 fictional docs), adversarial/ (3 labelled test fixtures), manifest.json (ACLs)
frontend/           React + TypeScript (Vite) UI and vitest tests
docs/               ARCHITECTURE.md (pipeline + file-by-file guide), EVALUATION_REPORT.md, DEMONSTRATIONS.md
```
