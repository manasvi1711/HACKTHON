"""Defensive helpers: content-type sniffing, prompt-injection screening, secret
detection, rate limiting. All are heuristics that reduce risk; none guarantee it."""
from __future__ import annotations

import re
import threading
import time
from collections import defaultdict, deque

from fastapi import HTTPException

EXTENSION_TYPES = {
    ".pdf": "pdf",
    ".html": "html",
    ".htm": "html",
    ".md": "markdown",
    ".markdown": "markdown",
    ".txt": "txt",
}


class UploadRejected(Exception):
    def __init__(self, message: str, code: str = "rejected"):
        super().__init__(message)
        self.code = code


def sniff_file_type(filename: str, data: bytes) -> str:
    """Validate by content, not only extension. Returns pdf|html|markdown|txt."""
    ext = ""
    if "." in filename:
        ext = "." + filename.rsplit(".", 1)[1].lower()
    declared = EXTENSION_TYPES.get(ext)
    if declared is None:
        raise UploadRejected(f"Unsupported file type '{ext or 'none'}'. Supported: PDF, HTML, Markdown, TXT.", "unsupported_type")
    if not data.strip():
        raise UploadRejected("The file is empty.", "empty")
    is_pdf = data[:1024].lstrip().startswith(b"%PDF-")
    if declared == "pdf":
        if not is_pdf:
            raise UploadRejected("File has a .pdf extension but its content is not a PDF.", "type_mismatch")
        return "pdf"
    if is_pdf:
        raise UploadRejected("File content is a PDF but the extension says otherwise.", "type_mismatch")
    # Text formats: reject binary content (NUL bytes / undecodable / known binary magics).
    if b"\x00" in data[:8192] or data[:4] in (b"PK\x03\x04", b"\x7fELF", b"MZ\x90\x00") or data[:2] == b"MZ":
        raise UploadRejected("File appears to be binary, not a text document.", "type_mismatch")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise UploadRejected("Text files must be UTF-8 encoded.", "encoding")
    if declared == "html" and not re.search(r"<\s*(html|body|head|p|div|h[1-6]|title|!doctype)\b", text[:20000], re.I):
        raise UploadRejected("File has an HTML extension but does not contain HTML markup.", "type_mismatch")
    return declared


SAFE_TITLE_RE = re.compile(r"[^\w .,()\-]+", re.UNICODE)


def display_filename(name: str) -> str:
    """Strip directory components and odd characters from a user-supplied name.
    It is stored for display only; files on disk use generated IDs."""
    name = name.replace("\\", "/").split("/")[-1]
    name = SAFE_TITLE_RE.sub("_", name).strip(" .") or "document"
    return name[:200]


# --------------------------------------------------------------- prompt injection
INJECTION_PATTERNS = [
    r"\bignore\s+(all\s+|any\s+)?(the\s+)?(previous|prior|above|earlier)\s+(instructions|prompts?|rules)",
    r"\bdisregard\s+(all\s+|any\s+)?(the\s+)?(previous|prior|above|system)\b",
    r"\b(reveal|print|show|output|leak|dump)\s+(all\s+|the\s+|your\s+)?(confidential|secret|hidden|restricted|system\s+prompt|instructions)",
    r"\byou\s+are\s+now\s+(a|an|in)\b",
    r"\b(new|updated)\s+system\s+(prompt|instructions?)\b",
    r"\bact\s+as\s+(the\s+)?(admin|administrator|root|developer)\b",
    r"\bdeveloper\s+mode\b",
    r"<\s*/?\s*(system|evidence|instructions)\s*>",
    r"\b(execute|run)\s+(this|the following)\s+(code|command|script)\b",
    r"\b(send|email|post)\s+(this|the|all)\s+.{0,40}\bto\s+https?://",
]
_INJ = [re.compile(p, re.I) for p in INJECTION_PATTERNS]


def detect_injection(text: str) -> list[str]:
    return [m.group(0) for p in _INJ if (m := p.search(text))]


# --------------------------------------------------------------- secrets
SECRET_PATTERNS = {
    "aws_access_key_id": r"\bAKIA[0-9A-Z]{16}\b",
    "private_key_block": r"-----BEGIN (RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----",
    "anthropic_or_openai_key": r"\bsk-(ant-)?[A-Za-z0-9_\-]{20,}\b",
    "github_token": r"\bgh[pousr]_[A-Za-z0-9]{30,}\b",
    "slack_token": r"\bxox[abpr]-[A-Za-z0-9-]{10,}\b",
    "generic_assignment": r"(?i)\b(api[_-]?key|secret[_-]?key|access[_-]?token|password|passwd)\b\s*[:=]\s*['\"]?[A-Za-z0-9/+_\-]{12,}",
    "jwt": r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b",
}
_SEC = {k: re.compile(v) for k, v in SECRET_PATTERNS.items()}


def detect_secrets(text: str) -> list[str]:
    """Returns the *types* of likely secrets found (never the values)."""
    return sorted({name for name, p in _SEC.items() if p.search(text)})


# --------------------------------------------------------------- links
SAFE_SCHEMES = ("http://", "https://", "mailto:")


def is_safe_link(url: str) -> bool:
    return url.strip().lower().startswith(SAFE_SCHEMES)


# --------------------------------------------------------------- rate limiting
class RateLimiter:
    """In-memory sliding window per key. Single-process only; use a shared store
    (e.g. Redis) behind a load balancer."""

    def __init__(self) -> None:
        self._events: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, key: str, limit: int, window_s: float = 60.0) -> None:
        now = time.monotonic()
        with self._lock:
            q = self._events[key]
            while q and now - q[0] > window_s:
                q.popleft()
            if len(q) >= limit:
                raise HTTPException(status_code=429, detail="Rate limit exceeded; please wait and retry.")
            q.append(now)

    def reset(self) -> None:
        with self._lock:
            self._events.clear()


rate_limiter = RateLimiter()
