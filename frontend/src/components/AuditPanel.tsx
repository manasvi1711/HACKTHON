import { useEffect, useState } from "react";
import { api } from "../api";
import type { AuditEntry } from "../types";

export function AuditPanel() {
  const [rows, setRows] = useState<AuditEntry[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState("");

  const load = () => api<AuditEntry[]>("/api/audit?limit=300").then(setRows).catch((e) => setError(e.message));
  useEffect(() => { load(); }, []);
  const shown = rows?.filter((r) => !filter || `${r.action} ${r.user_id} ${r.decision} ${r.resource}`.includes(filter));

  return (
    <div className="card">
      <div className="row between wrap gap">
        <h3 className="sub">Security audit log (metadata only — no document text or credentials)</h3>
        <div className="row gap">
          <input placeholder="Filter (e.g. access.denied)" value={filter} onChange={(e) => setFilter(e.target.value)} />
          <button className="btn small ghost" onClick={load}>Refresh</button>
        </div>
      </div>
      {error && <p className="note danger">{error}</p>}
      {!rows && !error && <p className="muted">Loading…</p>}
      {shown && (
        <div className="table-wrap">
          <table>
            <thead><tr><th>Time (UTC)</th><th>Action</th><th>User</th><th>Resource</th><th>Decision</th><th>Details</th><th>Request</th></tr></thead>
            <tbody>
              {shown.map((r) => (
                <tr key={r.id} className={r.decision === "deny" ? "deny" : ""}>
                  <td>{r.ts}</td><td>{r.action}</td><td>{r.user_id ?? "—"}</td><td><code>{r.resource ?? ""}</code></td>
                  <td>{r.decision}</td><td className="details">{r.details}</td><td><code>{r.request_id?.slice(0, 8)}</code></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
