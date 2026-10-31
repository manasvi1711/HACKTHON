"""Runtime configuration, read once from environment variables.

Secrets (ANTHROPIC_API_KEY) are only ever read here, on the server, and are never
returned by any API route or written to logs.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
PROJECT_DIR = BACKEND_DIR.parent


def _load_dotenv() -> None:
    """Minimal .env loader (no extra dependency). Existing env vars win."""
    for candidate in (PROJECT_DIR / ".env", BACKEND_DIR / ".env"):
        if not candidate.is_file():
            continue
        for line in candidate.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv()


def _path(name: str, default: Path) -> Path:
    """Relative paths are resolved against the project root, not the launch directory."""
    p = Path(os.environ.get(name, default))
    return p if p.is_absolute() else (PROJECT_DIR / p).resolve()


def _int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


def _float(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


def _bool(name: str, default: bool) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    data_dir: Path = field(default_factory=lambda: _path("DATA_DIR", BACKEND_DIR / "data"))

    # --- model / generation -------------------------------------------------
    # "anthropic" uses Claude; "mock" uses a deterministic extractive generator.
    # "auto" picks anthropic when an API key is configured, otherwise mock.
    llm_provider: str = field(default_factory=lambda: os.environ.get("LLM_PROVIDER", "auto"))
    anthropic_model: str = field(default_factory=lambda: os.environ.get("ANTHROPIC_MODEL", "claude-opus-5-5"))
    llm_effort: str = field(default_factory=lambda: os.environ.get("LLM_EFFORT", "low"))
    llm_max_tokens: int = field(default_factory=lambda: _int("LLM_MAX_TOKENS", 4000))
    llm_timeout_s: float = field(default_factory=lambda: _float("LLM_TIMEOUT_SECONDS", 60))
    llm_server_fallbacks: bool = field(default_factory=lambda: _bool("LLM_SERVER_FALLBACKS", True))

    # --- embeddings ---------------------------------------------------------
    # "fastembed" = local ONNX model (no data leaves the machine); "hashing" =
    # dependency-free lexical fallback (NOT semantic; for constrained environments).
    embedding_provider: str = field(default_factory=lambda: os.environ.get("EMBEDDING_PROVIDER", "fastembed"))
    embedding_model: str = field(default_factory=lambda: os.environ.get("EMBEDDING_MODEL", "BAAI/bge-small-en-v1.5"))
    model_cache_dir: Path = field(default_factory=lambda: _path("MODEL_CACHE_DIR", PROJECT_DIR / ".models"))

    # --- chunking / retrieval -----------------------------------------------
    chunk_size_chars: int = field(default_factory=lambda: _int("CHUNK_SIZE_CHARS", 900))
    chunk_overlap_chars: int = field(default_factory=lambda: _int("CHUNK_OVERLAP_CHARS", 150))
    retrieval_top_k: int = field(default_factory=lambda: _int("RETRIEVAL_TOP_K", 6))
    retrieval_max_k: int = 20
    # Evidence-sufficiency thresholds (ranking signals, not probabilities). See README.
    min_dense_score: float = field(default_factory=lambda: _float("MIN_DENSE_SCORE", 0.62))
    min_term_coverage: float = field(default_factory=lambda: _float("MIN_TERM_COVERAGE", 0.5))

    # --- upload limits --------------------------------------------------------
    max_upload_bytes: int = field(default_factory=lambda: _int("MAX_UPLOAD_BYTES", 10 * 1024 * 1024))
    max_pdf_pages: int = field(default_factory=lambda: _int("MAX_PDF_PAGES", 200))
    max_extracted_chars: int = field(default_factory=lambda: _int("MAX_EXTRACTED_CHARS", 2_000_000))
    parse_timeout_s: float = field(default_factory=lambda: _float("PARSE_TIMEOUT_SECONDS", 20))
    parse_in_subprocess: bool = field(default_factory=lambda: _bool("PARSE_IN_SUBPROCESS", True))
    secret_policy: str = field(default_factory=lambda: os.environ.get("SECRET_POLICY", "quarantine"))  # or "reject"

    # --- API protection ------------------------------------------------------
    max_question_chars: int = 1000
    qa_rate_limit_per_minute: int = field(default_factory=lambda: _int("QA_RATE_LIMIT_PER_MINUTE", 20))
    login_rate_limit_per_minute: int = field(default_factory=lambda: _int("LOGIN_RATE_LIMIT_PER_MINUTE", 10))
    upload_rate_limit_per_minute: int = field(default_factory=lambda: _int("UPLOAD_RATE_LIMIT_PER_MINUTE", 10))
    session_ttl_minutes: int = field(default_factory=lambda: _int("SESSION_TTL_MINUTES", 480))
    cors_origins: list[str] = field(
        default_factory=lambda: [o.strip() for o in os.environ.get("CORS_ORIGINS", "http://localhost:5173").split(",") if o.strip()]
    )
    audit_retention_days: int = field(default_factory=lambda: _int("AUDIT_RETENTION_DAYS", 90))

    # --- demo ---------------------------------------------------------------
    demo_password: str = field(default_factory=lambda: os.environ.get("DEMO_PASSWORD", "demo-only-password"))
    seed_sample_docs: bool = field(default_factory=lambda: _bool("SEED_SAMPLE_DOCS", True))

    @property
    def anthropic_api_key_present(self) -> bool:
        key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
        # Placeholders copied from .env.example must not switch the app out of mock mode.
        placeholder = not key or key.lower().startswith(("replace", "your", "changeme", "change-me", "<", "xxx"))
        return not placeholder

    @property
    def effective_llm_provider(self) -> str:
        if self.llm_provider == "auto":
            return "anthropic" if self.anthropic_api_key_present else "mock"
        return self.llm_provider

    @property
    def db_path(self) -> Path:
        return self.data_dir / "app.db"

    @property
    def files_dir(self) -> Path:
        return self.data_dir / "files"


settings = Settings()
