import { FormEvent, useEffect, useState } from "react";
import { api, getToken, setToken } from "./api";
import { AskPanel } from "./components/AskPanel";
import { AuditPanel } from "./components/AuditPanel";
import { DocumentsPanel } from "./components/DocumentsPanel";
import type { Health, User } from "./types";

const DEMO_USERS = [
  ["emma", "Employee"], ["sam", "Sales"], ["hana", "HR"], ["adam", "Admin"], ["eli", "Employee (2nd)"],
] as const;

function Login({ onLogin }: { onLogin: (u: User) => void }) {
  const [username, setUsername] = useState("emma");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const r = await api<{ token: string; user: User }>("/api/auth/login", { method: "POST", body: JSON.stringify({ username, password }) });
      setToken(r.token);
      onLogin(r.user);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Login failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="login-wrap">
      <form className="card login" onSubmit={submit}>
        <h1>Docs Q&amp;A Assistant</h1>
        <p className="muted">Answers grounded in the enterprise documents you are allowed to read.</p>
        <p className="note info">Local demo authentication with synthetic users. Not for production use; see README.</p>
        <label className="label">User
          <select value={username} onChange={(e) => setUsername(e.target.value)}>
            {DEMO_USERS.map(([u, r]) => <option key={u} value={u}>{u} — {r}</option>)}
          </select>
        </label>
        <label className="label">Password
          <input type="password" value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="current-password"
            placeholder="DEMO_PASSWORD from the server .env" />
        </label>
        {error && <p className="note danger" role="alert">{error}</p>}
        <button className="btn" disabled={busy || !password}>{busy ? "Signing in…" : "Sign in"}</button>
      </form>
    </div>
  );
}

type Tab = "ask" | "documents" | "audit";

export default function App() {
  const [user, setUser] = useState<User | null>(null);
  const [checking, setChecking] = useState(true);
  const [health, setHealth] = useState<Health | null>(null);
  const [tab, setTab] = useState<Tab>("ask");

  useEffect(() => {
    api<Health>("/api/health").then(setHealth).catch(() => setHealth(null));
    if (!getToken()) { setChecking(false); return; }
    api<User>("/api/auth/me").then(setUser).catch(() => setToken(null)).finally(() => setChecking(false));
  }, []);

  async function logout() {
    try { await api("/api/auth/logout", { method: "POST" }); } catch { /* already invalid */ }
    setToken(null);
    setUser(null);
    setTab("ask");
  }

  if (checking) return <div className="login-wrap"><p className="muted">Loading…</p></div>;
  if (!user) return <Login onLogin={setUser} />;

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">Docs Q&amp;A Assistant</div>
        <nav className="tabs" role="tablist">
          <button role="tab" aria-selected={tab === "ask"} onClick={() => setTab("ask")}>Ask</button>
          <button role="tab" aria-selected={tab === "documents"} onClick={() => setTab("documents")}>Documents</button>
          {user.is_admin && <button role="tab" aria-selected={tab === "audit"} onClick={() => setTab("audit")}>Audit log</button>}
        </nav>
        <div className="row gap wrap right">
          {health && (health.mock_mode
            ? <span className="badge badge-warn" title="No model credentials configured">MOCK MODE · no LLM</span>
            : <span className="badge badge-info" title={health.embedding_backend}>Model: {health.model}</span>)}
          <span className="user">{user.display_name} <span className="badge badge-role">{user.role}</span></span>
          <button className="btn small ghost" onClick={logout}>Sign out</button>
        </div>
      </header>
      <main className="content">
        {tab === "ask" && <AskPanel />}
        {tab === "documents" && <DocumentsPanel user={user} />}
        {tab === "audit" && user.is_admin && <AuditPanel />}
      </main>
    </div>
  );
}
