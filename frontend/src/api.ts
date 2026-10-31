// Thin API client. The bearer token lives in sessionStorage (cleared when the tab
// closes). No credentials or provider keys ever reach the browser.

const TOKEN_KEY = "docqa.token";

export class ApiError extends Error {
  constructor(public status: number, message: string, public code?: string) {
    super(message);
  }
}

export function getToken(): string | null {
  try {
    return sessionStorage.getItem(TOKEN_KEY);
  } catch {
    return null;
  }
}

export function setToken(token: string | null): void {
  try {
    if (token) sessionStorage.setItem(TOKEN_KEY, token);
    else sessionStorage.removeItem(TOKEN_KEY);
  } catch {
    /* storage unavailable: session lasts for this page only */
  }
}

function errorMessage(body: unknown, status: number): { message: string; code?: string } {
  if (body && typeof body === "object" && "detail" in body) {
    const detail = (body as { detail: unknown }).detail;
    if (typeof detail === "string") return { message: detail };
    if (detail && typeof detail === "object" && "message" in detail) {
      const d = detail as { message: string; code?: string };
      return { message: d.message, code: d.code };
    }
    if (Array.isArray(detail)) return { message: detail.map((d) => d.msg ?? String(d)).join("; ") };
  }
  if (status === 429) return { message: "Too many requests. Please wait a moment and try again." };
  return { message: `Request failed (${status})` };
}

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  const token = getToken();
  if (token) headers.set("Authorization", `Bearer ${token}`);
  if (init.body && !(init.body instanceof FormData)) headers.set("Content-Type", "application/json");
  const res = await fetch(path, { ...init, headers });
  const text = await res.text();
  let body: unknown = null;
  try {
    body = text ? JSON.parse(text) : null;
  } catch {
    body = null;
  }
  if (!res.ok) {
    const { message, code } = errorMessage(body, res.status);
    throw new ApiError(res.status, message, code);
  }
  return body as T;
}

/** Downloads a permission-protected file: the backend checks access on every request. */
export async function downloadProtected(path: string, fallbackName: string): Promise<void> {
  const token = getToken();
  const res = await fetch(path, { headers: token ? { Authorization: `Bearer ${token}` } : {} });
  if (!res.ok) throw new ApiError(res.status, res.status === 404 ? "Document not available to you" : `Download failed (${res.status})`);
  const blob = await res.blob();
  const match = /filename="?([^"]+)"?/.exec(res.headers.get("content-disposition") ?? "");
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = match?.[1] ?? fallbackName;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

/** Only http(s)/mailto links are ever rendered as clickable; everything else is text. */
export function safeHref(url: string | null | undefined): string | null {
  if (!url) return null;
  const trimmed = url.trim();
  if (trimmed.startsWith("/") && !trimmed.startsWith("//")) return trimmed;
  return /^(https?:|mailto:)/i.test(trimmed) ? trimmed : null;
}
