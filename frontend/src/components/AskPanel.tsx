import { FormEvent, useEffect, useState } from "react";
import { api } from "../api";
import type { AskResponse } from "../types";
import { AnswerView, StateBadge } from "./AnswerView";

interface HistoryItem { id: string; created_at: string; question: string; response: AskResponse }

const EXAMPLES = [
  "I forgot my login password",
  "What does error E104 mean?",
  "What is the maximum upload size?",
  "Can I get a refund for my monthly subscription?",
  "Can the reset link expiry be extended?",
];

export function AskPanel() {
  const [question, setQuestion] = useState("");
  const [topK, setTopK] = useState(6);
  const [debug, setDebug] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<AskResponse | null>(null);
  const [history, setHistory] = useState<HistoryItem[]>([]);

  const loadHistory = () => api<HistoryItem[]>("/api/history?limit=10").then(setHistory).catch(() => setHistory([]));
  useEffect(() => { loadHistory(); }, []);

  async function submit(e?: FormEvent, q = question) {
    e?.preventDefault();
    if (!q.trim()) return;
    setLoading(true);
    setError(null);
    try {
      setResult(await api<AskResponse>("/api/ask", { method: "POST", body: JSON.stringify({ question: q, top_k: topK, debug }) }));
      loadHistory();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Request failed");
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="ask-layout">
      <div className="ask-main">
        <form className="card ask-form" onSubmit={submit}>
          <label htmlFor="q" className="label">Ask a question about your documentation</label>
          <textarea
            id="q" value={question} maxLength={1000} rows={3}
            placeholder="e.g. How do I reset my password?"
            onChange={(e) => setQuestion(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); submit(); } }}
          />
          <div className="row between wrap gap">
            <div className="row gap wrap">
              <label className="inline">Passages (top-k)
                <select value={topK} onChange={(e) => setTopK(Number(e.target.value))}>
                  {[3, 4, 6, 8, 10].map((k) => <option key={k} value={k}>{k}</option>)}
                </select>
              </label>
              <label className="inline"><input type="checkbox" checked={debug} onChange={(e) => setDebug(e.target.checked)} /> Show retrieval debug</label>
            </div>
            <div className="row gap">
              <span className="muted small">{question.length}/1000</span>
              <button className="btn" type="submit" disabled={loading || !question.trim()}>{loading ? "Searching…" : "Ask"}</button>
            </div>
          </div>
          <div className="examples">
            {EXAMPLES.map((ex) => (
              <button type="button" key={ex} className="chip" onClick={() => { setQuestion(ex); submit(undefined, ex); }}>{ex}</button>
            ))}
          </div>
        </form>

        {error && <p className="note danger" role="alert">{error}</p>}
        {loading && <div className="card skeleton" aria-busy="true">Retrieving authorized passages and composing an answer…</div>}
        {!loading && result && <AnswerView result={result} />}
        {!loading && !result && !error && (
          <div className="card empty">Ask a question to get an answer grounded in the documents you are allowed to read, with citations.</div>
        )}
      </div>

      <aside className="card history">
        <h3 className="sub">Your recent questions</h3>
        {history.length === 0 ? <p className="muted small">No questions yet.</p> : (
          <ul>
            {history.map((h) => (
              <li key={h.id}>
                <button className="link" onClick={() => { setQuestion(h.question); setResult(h.response); }}>{h.question}</button>
                <StateBadge state={h.response.state} />
              </li>
            ))}
          </ul>
        )}
      </aside>
    </div>
  );
}
