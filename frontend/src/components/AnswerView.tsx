import { useState } from "react";
import { api, ApiError, downloadProtected } from "../api";
import type { AnswerState, AskResponse, Citation, Passage } from "../types";

const STATE_META: Record<AnswerState, { label: string; tone: string; hint: string }> = {
  answered: { label: "Answered", tone: "ok", hint: "Supported by the cited passages." },
  partially_answered: { label: "Partially answered", tone: "warn", hint: "Only part of the question is covered by the documents." },
  clarification_needed: { label: "Clarification needed", tone: "info", hint: "More detail is needed to give a precise answer." },
  conflicting_evidence: { label: "Conflicting evidence", tone: "danger", hint: "Sources disagree. Both are shown; neither was silently chosen." },
  not_found: { label: "Not found", tone: "muted", hint: "No supporting evidence in the documents available to you." },
};

export function StateBadge({ state }: { state: AnswerState }) {
  const meta = STATE_META[state];
  return <span className={`badge badge-${meta.tone}`} data-testid="state-badge">{meta.label}</span>;
}

function CitationCard({ c, open, onToggle }: { c: Citation; open: boolean; onToggle: () => void }) {
  const [passage, setPassage] = useState<Passage | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  async function loadPassage() {
    setLoading(true);
    setError(null);
    try {
      setPassage(await api<Passage>(c.passage_url));
    } catch (e) {
      setError(e instanceof ApiError && e.status === 404 ? "This passage is no longer available to you." : "Could not load the passage.");
    } finally {
      setLoading(false);
    }
  }

  async function download() {
    try {
      await downloadProtected(c.download_url, c.title);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Download failed");
    }
  }

  const where = [c.section, c.page ? `page ${c.page}` : null, c.product_version ? `v${c.product_version}` : null]
    .filter(Boolean)
    .join(" · ");
  return (
    <li className="citation" id={`cite-${c.number}`}>
      <button className="citation-head" onClick={onToggle} aria-expanded={open}>
        <span className="cite-num">[{c.number}]</span>
        <span className="cite-title">{c.title}</span>
        {where && <span className="cite-where">{where}</span>}
        <span className="chev" aria-hidden>{open ? "▾" : "▸"}</span>
      </button>
      {open && (
        <div className="citation-body">
          <blockquote className="excerpt">{c.excerpt}</blockquote>
          {c.injection_flag && (
            <p className="note warn">This passage contains instruction-like text. It was treated as document content only.</p>
          )}
          <dl className="meta-grid">
            <dt>Document ID</dt><dd><code>{c.document_id}</code></dd>
            <dt>Passage ID</dt><dd><code>{c.chunk_id}</code> ({c.section_anchor})</dd>
            <dt>Source</dt><dd><code>{c.source_id}</code></dd>
            {c.effective_date && (<><dt>Effective</dt><dd>{c.effective_date}</dd></>)}
          </dl>
          <div className="row gap">
            <button className="btn small" onClick={loadPassage} disabled={loading}>
              {loading ? "Loading…" : "View full passage"}
            </button>
            <button className="btn small ghost" onClick={download}>Download source</button>
          </div>
          {error && <p className="note danger">{error}</p>}
          {passage && <pre className="passage">{passage.text}</pre>}
        </div>
      )}
    </li>
  );
}

export function AnswerView({ result }: { result: AskResponse }) {
  const [openCite, setOpenCite] = useState<number | null>(null);
  const meta = STATE_META[result.state];
  const toggle = (n: number) => setOpenCite((cur) => (cur === n ? null : n));
  const failed = Boolean(result.generation_error) && result.answer.length === 0;

  return (
    <section className={`answer tone-${failed ? "danger" : meta.tone}`} aria-live="polite">
      <header className="answer-head">
        {failed ? <span className="badge badge-danger" data-testid="state-badge">Answer service error</span> : <StateBadge state={result.state} />}
        <span className="hint">{failed ? "The documents were searched, but the answer could not be generated." : meta.hint}</span>
        {result.cached && <span className="badge badge-muted">cached for you</span>}
      </header>

      {failed && <p className="note danger" role="alert">{result.generation_error}</p>}

      {result.state === "not_found" && !failed && (
        <p className="note info">
          Tip: rephrase with the terms your documents use, or tick <b>Show retrieval debug</b> and ask again to see
          which passages were searched and how closely they matched.
        </p>
      )}

      {result.mock_mode && (
        <p className="note mock" role="note">
          MOCK MODE: the text below is extracted verbatim from evidence by a deterministic heuristic, not written by a language model.
        </p>
      )}

      {result.message && !failed && <p className="message">{result.message}</p>}

      {result.clarification_question && (
        <p className="clarify"><strong>Question for you:</strong> {result.clarification_question}</p>
      )}

      {result.conflicts.length > 0 && (
        <div className="conflicts">
          {result.conflicts.map((c, i) => (
            <div key={i} className={`conflict ${c.kind}`}>
              <strong>{c.kind === "version_difference" ? "Differs by version" : "Contradictory sources"}</strong>
              <ul>
                {c.facts.map((f, j) => (
                  <li key={j}>
                    <b>{f.value}</b>: {f.title}{f.version ? ` (v${f.version})` : ""}{" "}
                    <button className="cite-link" onClick={() => toggle(c.citations[j])}>[{c.citations[j]}]</button>
                  </li>
                ))}
              </ul>
            </div>
          ))}
        </div>
      )}

      {result.answer.length > 0 && (
        <ul className="segments">
          {result.answer.map((s, i) => (
            <li key={i}>
              <span className="seg-text">{s.text}</span>{" "}
              {s.citations.map((n) => (
                <button key={n} className="cite-link" onClick={() => toggle(n)} aria-label={`Show citation ${n}`}>
                  [{n}]
                </button>
              ))}
            </li>
          ))}
        </ul>
      )}

      {result.missing_information.length > 0 && (
        <div className="missing">
          <strong>Not covered by the documents:</strong>
          <ul>{result.missing_information.map((m, i) => <li key={i}>{m}</li>)}</ul>
        </div>
      )}

      {result.citations.length > 0 && (
        <>
          <h3 className="sub">Sources</h3>
          <ol className="citations">
            {result.citations.map((c) => (
              <CitationCard key={c.number} c={c} open={openCite === c.number} onToggle={() => toggle(c.number)} />
            ))}
          </ol>
        </>
      )}

      {(result.warnings.length > 0 || result.security_flags.length > 0) && (
        <div className="warnings">
          {result.security_flags.map((f) => <span key={f} className="badge badge-warn">{f.replace(/_/g, " ")}</span>)}
          <ul>{result.warnings.map((w, i) => <li key={i}>{w}</li>)}</ul>
        </div>
      )}

      {result.debug && <DebugTable result={result} />}
      <p className="footnote">Generator: {result.generator ?? "none (no evidence)"} · Retrieval scores are ranking signals, not probabilities.</p>
    </section>
  );
}

function DebugTable({ result }: { result: AskResponse }) {
  const d = result.debug!;
  return (
    <details className="debug" open>
      <summary>Retrieval debug (authorized passages only) · top_k={d.top_k} · evidence={d.evidence_count}</summary>
      {d.notes.length > 0 && <ul className="notes">{d.notes.map((n, i) => <li key={i}>{n}</li>)}</ul>}
      <div className="table-wrap">
        <table>
          <thead>
            <tr><th>#</th><th>Document / section</th><th>Dense</th><th>BM25</th><th>Fused</th><th>Coverage</th><th>Evidence</th></tr>
          </thead>
          <tbody>
            {d.retrieved.map((r) => (
              <tr key={r.chunk_id} className={r.used_as_evidence ? "used" : ""}>
                <td>{r.rank}</td>
                <td title={r.text}>
                  {r.title}{r.product_version ? ` (v${r.product_version})` : ""}<br />
                  <small>{r.section || "—"}{r.page ? ` · p.${r.page}` : ""}{r.exact_code_match ? " · exact code" : ""}{r.injection_flag ? " · flagged" : ""}</small>
                </td>
                <td>{r.dense_score.toFixed(3)}</td>
                <td>{r.bm25_score.toFixed(2)}</td>
                <td>{r.fused_score.toFixed(4)}</td>
                <td>{r.term_coverage.toFixed(2)}</td>
                <td>{r.used_as_evidence ? "yes" : "no"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="footnote">Thresholds: {Object.entries(d.thresholds).map(([k, v]) => `${k}=${v}`).join(", ")}</p>
    </details>
  );
}
