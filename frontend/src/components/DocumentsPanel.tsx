import { FormEvent, useEffect, useState } from "react";
import { api, ApiError, downloadProtected } from "../api";
import type { DocumentInfo, Passage, Role, User } from "../types";

const ROLES: Role[] = ["employee", "sales", "hr", "admin"];
const ACCEPT = ".pdf,.html,.htm,.md,.markdown,.txt";

interface UploadStatus { name: string; state: "uploading" | "done" | "error"; message: string }

function StatusBadges({ d }: { d: DocumentInfo }) {
  return (
    <span className="row gap-s wrap">
      <span className={`badge ${d.status === "ready" ? "badge-ok" : d.status === "quarantined" ? "badge-danger" : "badge-muted"}`}>{d.status}</span>
      {d.approval_status !== "approved" && <span className="badge badge-warn">{d.approval_status}</span>}
      {d.collection === "test_fixtures" && <span className="badge badge-info">test fixture</span>}
      {d.security_flags.map((f) => <span key={f} className="badge badge-danger" title={f}>{f.split(":")[0].replace(/_/g, " ")}</span>)}
    </span>
  );
}

function UploadForm({ user, onDone }: { user: User; onDone: () => void }) {
  const [files, setFiles] = useState<FileList | null>(null);
  const [roles, setRoles] = useState<Role[]>(user.is_admin ? ["employee", "sales", "hr"] : [user.role]);
  const [version, setVersion] = useState("");
  const [effective, setEffective] = useState("");
  const [statuses, setStatuses] = useState<UploadStatus[]>([]);
  const busy = statuses.some((s) => s.state === "uploading");

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (!files?.length) return;
    const list = Array.from(files);
    setStatuses(list.map((f) => ({ name: f.name, state: "uploading", message: "Uploading and processing…" })));
    for (const [i, f] of list.entries()) {
      const fd = new FormData();
      fd.append("file", f);
      fd.append("allowed_roles", roles.join(","));
      if (version) fd.append("product_version", version);
      if (effective) fd.append("effective_date", effective);
      let next: UploadStatus;
      try {
        const doc = await api<DocumentInfo>("/api/documents", { method: "POST", body: fd });
        const extra = doc.status === "quarantined" ? " — quarantined (possible secret detected); not searchable"
          : doc.approval_status === "pending" ? " — awaiting admin approval before it is searchable" : "";
        next = { name: f.name, state: "done", message: `Indexed ${doc.chunk_count} passages${extra}` };
      } catch (err) {
        next = { name: f.name, state: "error", message: err instanceof Error ? err.message : "Upload failed" };
      }
      setStatuses((s) => s.map((x, j) => (j === i ? next : x)));
    }
    onDone();
  }

  return (
    <form className="card upload" onSubmit={submit}>
      <h3 className="sub">Upload documents</h3>
      <p className="muted small">PDF (text-based; scanned PDFs need OCR, which is not supported), HTML, Markdown, TXT · max 10 MB each.</p>
      <input type="file" multiple accept={ACCEPT} onChange={(e) => setFiles(e.target.files)} aria-label="Files to upload" />
      <fieldset className="roles">
        <legend>Who may read these documents</legend>
        {ROLES.filter((r) => r !== "admin").map((r) => (
          <label key={r} className="inline">
            <input type="checkbox" checked={roles.includes(r)} disabled={!user.is_admin && r === user.role}
              onChange={(e) => setRoles((cur) => (e.target.checked ? [...cur, r] : cur.filter((x) => x !== r)))} />
            {r}
          </label>
        ))}
        <span className="muted small">Admins always have access.</span>
      </fieldset>
      <div className="row gap wrap">
        <label className="inline">Product version (optional)<input value={version} onChange={(e) => setVersion(e.target.value)} placeholder="e.g. 3" maxLength={20} /></label>
        <label className="inline">Effective date (optional)<input type="date" value={effective} onChange={(e) => setEffective(e.target.value)} /></label>
      </div>
      <p className="muted small">Leave version/date empty unless you know them; values found explicitly in the document are used otherwise. Nothing is guessed.</p>
      <button className="btn" disabled={busy || !files?.length || roles.length === 0}>{busy ? "Processing…" : "Upload"}</button>
      {statuses.length > 0 && (
        <ul className="upload-status">
          {statuses.map((s, i) => (
            <li key={i} className={s.state}><b>{s.name}</b>: {s.state === "uploading" ? <span className="spinner" aria-hidden /> : null}{s.message}</li>
          ))}
        </ul>
      )}
    </form>
  );
}

function DocumentDetail({ doc, user, onChange }: { doc: DocumentInfo; user: User; onChange: () => void }) {
  const [passages, setPassages] = useState<Passage[] | null>(null);
  const [roles, setRoles] = useState<Role[]>(doc.allowed_roles ?? []);
  const [msg, setMsg] = useState<string | null>(null);
  const [integrity, setIntegrity] = useState<string | null>(null);
  const readable = doc.status === "ready" && (doc.approval_status === "approved" || user.is_admin);

  useEffect(() => { setPassages(null); setRoles(doc.allowed_roles ?? []); setMsg(null); setIntegrity(null); }, [doc.id]);

  async function run(label: string, fn: () => Promise<unknown>) {
    setMsg(null);
    try { await fn(); setMsg(`${label}: done`); onChange(); }
    catch (e) { setMsg(`${label} failed: ${e instanceof ApiError ? e.message : "error"}`); }
  }

  return (
    <div className="card detail">
      <h3 className="sub">{doc.title}</h3>
      <StatusBadges d={doc} />
      <dl className="meta-grid">
        <dt>Document ID</dt><dd><code>{doc.id}</code></dd>
        <dt>File</dt><dd>{doc.original_filename} ({doc.file_type}{doc.page_count ? `, ${doc.page_count} pages` : ""})</dd>
        <dt>Source</dt><dd><code>{doc.source_id}</code></dd>
        <dt>Product / version</dt><dd>{doc.product ?? "—"} / {doc.product_version ?? "not stated"}</dd>
        <dt>Effective date</dt><dd>{doc.effective_date ?? "not stated"}</dd>
        <dt>Uploaded</dt><dd>{doc.uploaded_at} by {doc.uploader_id}</dd>
        <dt>Passages</dt><dd>{doc.chunk_count}</dd>
        <dt>SHA-256</dt><dd><code className="hash">{doc.content_hash}</code></dd>
        {doc.allowed_roles && (<><dt>Readable by</dt><dd>{doc.allowed_roles.join(", ")}</dd></>)}
      </dl>
      <div className="row gap wrap">
        {readable && <button className="btn small" onClick={() => api<{ passages: Passage[] }>(`/api/documents/${doc.id}/preview`).then((r) => setPassages(r.passages)).catch((e) => setMsg(e.message))}>Preview passages</button>}
        {readable && doc.status !== "quarantined" && <button className="btn small ghost" onClick={() => downloadProtected(`/api/documents/${doc.id}/download`, doc.original_filename).catch((e) => setMsg(e.message))}>Download</button>}
      </div>

      {user.is_admin && (
        <div className="admin-box">
          <h4>Administration</h4>
          {doc.approval_status !== "approved" && doc.status === "ready" && (
            <div className="row gap">
              <button className="btn small" onClick={() => run("Approve", () => api(`/api/documents/${doc.id}/approval`, { method: "POST", body: JSON.stringify({ approve: true }) }))}>Approve &amp; publish</button>
              <button className="btn small ghost" onClick={() => run("Reject", () => api(`/api/documents/${doc.id}/approval`, { method: "POST", body: JSON.stringify({ approve: false }) }))}>Reject</button>
            </div>
          )}
          <fieldset className="roles">
            <legend>Permissions</legend>
            {ROLES.filter((r) => r !== "admin").map((r) => (
              <label key={r} className="inline">
                <input type="checkbox" checked={roles.includes(r)} onChange={(e) => setRoles((cur) => (e.target.checked ? [...cur, r] : cur.filter((x) => x !== r)))} />{r}
              </label>
            ))}
            <button className="btn small" onClick={() => run("Permissions", () => api(`/api/documents/${doc.id}/permissions`, { method: "PATCH", body: JSON.stringify({ allowed_roles: roles.length ? roles : ["admin"] }) }))}>Save permissions</button>
          </fieldset>
          <div className="row gap wrap">
            <label className="btn small ghost file-btn">Replace file…
              <input type="file" accept={ACCEPT} hidden onChange={(e) => {
                const f = e.target.files?.[0];
                if (!f) return;
                const fd = new FormData(); fd.append("file", f);
                run("Replace", () => api(`/api/documents/${doc.id}/file`, { method: "PUT", body: fd }));
              }} />
            </label>
            <button className="btn small ghost" onClick={() => api<{ ok: boolean }>(`/api/documents/${doc.id}/integrity`).then((r) => setIntegrity(r.ok ? "Stored file matches recorded and approved hashes" : "Integrity check FAILED")).catch((e) => setMsg(e.message))}>Verify integrity</button>
            <button className="btn small danger" onClick={() => { if (confirm(`Delete "${doc.title}"? Its passages are removed from search immediately.`)) run("Delete", () => api(`/api/documents/${doc.id}`, { method: "DELETE" })); }}>Delete</button>
          </div>
          {integrity && <p className="note">{integrity}</p>}
        </div>
      )}
      {msg && <p className="note">{msg}</p>}
      {passages && (
        <ol className="preview">
          {passages.map((p) => (
            <li key={p.chunk_id}>
              <small className="muted">{p.section || "—"}{p.page ? ` · page ${p.page}` : ""} · <code>{p.chunk_id}</code>{p.injection_flag ? " · flagged: instruction-like text" : ""}</small>
              <pre className="passage">{p.text}</pre>
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}

export function DocumentsPanel({ user }: { user: User }) {
  const [docs, setDocs] = useState<DocumentInfo[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(null);

  const load = () => api<DocumentInfo[]>("/api/documents").then((d) => { setDocs(d); setError(null); }).catch((e) => setError(e.message));
  useEffect(() => { load(); }, []);
  const current = docs?.find((d) => d.id === selected) ?? null;

  return (
    <div className="docs-layout">
      <div>
        {user.can_upload ? <UploadForm user={user} onDone={load} /> : (
          <div className="card muted small">Your role ({user.role}) can read documents but cannot publish them.</div>
        )}
        <div className="card">
          <h3 className="sub">Documents you can access</h3>
          {error && <p className="note danger">{error}</p>}
          {docs === null && !error && <p className="muted">Loading…</p>}
          {docs?.length === 0 && <p className="muted">No documents are available to you yet.</p>}
          <ul className="doc-list">
            {docs?.map((d) => (
              <li key={d.id}>
                <button className={`doc-item ${selected === d.id ? "active" : ""}`} onClick={() => setSelected(d.id)}>
                  <span className={`ftype ${d.file_type}`}>{d.file_type}</span>
                  <span className="doc-title">{d.title}{d.product_version ? <small> · v{d.product_version}</small> : null}</span>
                  <StatusBadges d={d} />
                </button>
              </li>
            ))}
          </ul>
        </div>
      </div>
      <div>{current ? <DocumentDetail doc={current} user={user} onChange={load} /> : <div className="card empty">Select a document to see its metadata, passages and permissions.</div>}</div>
    </div>
  );
}
