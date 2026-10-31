"""FastAPI application: routes, middleware, startup seeding."""
from __future__ import annotations

import json
import logging
import re
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from . import auth
from .audit import audit, purge_expired
from .auth import ROLES, User, can_read_document, can_see_document_entry, current_user, require_admin
from .config import PROJECT_DIR, settings
from .db import get_conn, init_db
from .documents import (MIME, delete_document, document_public, ingest, set_approval, set_permissions, stored_path,
                        verify_integrity)
from .embeddings import get_embedder
from .qa import answer_question
from .security import UploadRejected, rate_limiter

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("app")

SAMPLE_DIR = PROJECT_DIR / "sample_docs"
FRONTEND_DIST = PROJECT_DIR / "frontend" / "dist"


def seed_samples(include_fixtures: bool = False) -> list[str]:
    """Ingest the synthetic sample collection (and optionally adversarial fixtures)."""
    manifest = json.loads((SAMPLE_DIR / "manifest.json").read_text(encoding="utf-8"))
    admin = auth.user_from_id("u-adam")
    loaded = []
    for entry in manifest["documents"]:
        if entry["collection"] == "test_fixtures" and not include_fixtures:
            continue
        path = SAMPLE_DIR / entry["path"]
        try:
            doc = ingest(admin, path.name, path.read_bytes(), entry["allowed_roles"], collection=entry["collection"])
            if entry["collection"] == "test_fixtures" and doc["approval_status"] != "approved":
                # Explicitly activated for testing: an admin approves flagged fixtures.
                set_approval(admin, doc["id"], True)
            loaded.append(doc["id"])
        except UploadRejected as exc:
            if exc.code != "duplicate":
                log.warning("sample %s not loaded: %s", entry["path"], exc)
    return loaded


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    auth.seed_users()
    purge_expired()
    get_embedder()
    if settings.seed_sample_docs and not get_conn().execute("SELECT 1 FROM documents LIMIT 1").fetchone():
        import os

        seed_samples(include_fixtures=os.environ.get("SEED_TEST_FIXTURES", "false").lower() == "true")
    yield


app = FastAPI(title="Enterprise Documentation Q&A Assistant", version="1.0.0", lifespan=lifespan,
              docs_url="/api/docs", openapi_url="/api/openapi.json", redoc_url=None)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,  # explicit list, never "*"
    allow_credentials=False,              # bearer tokens, no cookies
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
)


@app.middleware("http")
async def request_context(request: Request, call_next):
    request.state.request_id = uuid.uuid4().hex
    request.state.user_id = None
    cl = request.headers.get("content-length")
    if cl and cl.isdigit() and int(cl) > settings.max_upload_bytes + 1024 * 1024:
        return JSONResponse({"detail": "Request body too large"}, status_code=413)
    try:
        response = await call_next(request)
    except Exception:  # never leak stack traces / internals to clients
        log.exception("unhandled error request_id=%s", request.state.request_id)
        response = JSONResponse({"detail": "Internal error", "request_id": request.state.request_id}, status_code=500)
    response.headers["X-Request-ID"] = request.state.request_id
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
        "connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


def rid(request: Request) -> str:
    return request.state.request_id


def visible_doc_or_404(user: User, doc_id: str, request: Request, *, need_read: bool = True):
    row = get_conn().execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
    ok = can_read_document(user, row) if need_read else can_see_document_entry(user, row)
    if not ok:
        audit("access.denied", user_id=user.id, request_id=rid(request), resource=doc_id, decision="deny")
        # 404 (not 403) so the response does not confirm that a restricted document exists.
        raise HTTPException(status_code=404, detail="Document not found")
    return row


# ------------------------------------------------------------------ auth
class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


@app.post("/api/auth/login")
def login(body: LoginIn, request: Request):
    ip = request.client.host if request.client else "unknown"
    rate_limiter.check(f"login:{ip}", settings.login_rate_limit_per_minute)
    user = auth.authenticate(body.username.strip().lower(), body.password)
    if user is None:
        audit("auth.login", user_id=None, request_id=rid(request), decision="deny", username=body.username[:64])
        raise HTTPException(status_code=401, detail="Invalid username or password")
    token = auth.create_session(user)
    audit("auth.login", user_id=user.id, request_id=rid(request), decision="allow")
    return {"token": token, "user": user.public(), "demo_auth": True}


@app.post("/api/auth/logout")
def logout(request: Request, token: str = Depends(auth.bearer_token)):
    auth.revoke_session(token)
    return {"ok": True}


@app.get("/api/auth/me")
def me(user: User = Depends(current_user)):
    return user.public()


@app.get("/api/health")
def health():
    emb = get_embedder()
    provider = settings.effective_llm_provider
    return {"status": "ok", "llm_provider": provider, "mock_mode": provider == "mock",
            "model": settings.anthropic_model if provider == "anthropic" else None,
            "embedding_backend": emb.name, "roles": list(ROLES)}


# ------------------------------------------------------------------ documents
@app.get("/api/documents")
def list_documents(user: User = Depends(current_user)):
    rows = get_conn().execute("SELECT * FROM documents WHERE status!='deleted' ORDER BY title").fetchall()
    return [document_public(r, user) for r in rows if can_see_document_entry(user, r)]


@app.get("/api/documents/{doc_id}")
def get_document(doc_id: str, request: Request, user: User = Depends(current_user)):
    return document_public(visible_doc_or_404(user, doc_id, request, need_read=False), user)


VERSION_RE = re.compile(r"^v?\d+(\.\d+){0,3}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _clean_optional(value: str | None, pattern: re.Pattern | None, name: str, max_len: int = 200) -> str | None:
    if value is None or not value.strip():
        return None
    value = value.strip()
    if len(value) > max_len or (pattern and not pattern.match(value)):
        raise HTTPException(status_code=422, detail=f"Invalid {name}")
    return value


async def _read_upload(file: UploadFile) -> bytes:
    data = await file.read(settings.max_upload_bytes + 1)
    if len(data) > settings.max_upload_bytes:
        raise HTTPException(status_code=413, detail=f"File exceeds the {settings.max_upload_bytes // (1024 * 1024)} MB upload limit.")
    return data


def _rejected(exc: UploadRejected, user: User, request: Request, filename: str):
    audit("document.upload_failed", user_id=user.id, request_id=rid(request), decision=exc.code, filename=filename[:200])
    status = {"duplicate": 409, "too_large": 413, "unsupported_type": 415, "type_mismatch": 415}.get(exc.code, 422)
    raise HTTPException(status_code=status, detail={"code": exc.code, "message": str(exc)})


@app.post("/api/documents", status_code=201)
async def upload_document(request: Request, file: UploadFile = File(...), allowed_roles: str = Form(...),
                          title: str | None = Form(None), product: str | None = Form(None),
                          product_version: str | None = Form(None), effective_date: str | None = Form(None),
                          user: User = Depends(current_user)):
    rate_limiter.check(f"upload:{user.id}", settings.upload_rate_limit_per_minute)
    roles = auth.validate_upload_acl(user, [r.strip() for r in allowed_roles.split(",")])
    data = await _read_upload(file)
    try:
        return ingest(user, file.filename or "upload", data, roles, title=_clean_optional(title, None, "title"),
                      product=_clean_optional(product, None, "product", 60),
                      product_version=_clean_optional(product_version, VERSION_RE, "product version", 20),
                      effective_date=_clean_optional(effective_date, DATE_RE, "effective date", 10),
                      request_id=rid(request))
    except UploadRejected as exc:
        _rejected(exc, user, request, file.filename or "")


@app.put("/api/documents/{doc_id}/file")
async def replace_document(doc_id: str, request: Request, file: UploadFile = File(...),
                           user: User = Depends(require_admin)):
    row = visible_doc_or_404(user, doc_id, request)
    data = await _read_upload(file)
    try:
        return ingest(user, file.filename or "upload", data, json.loads(row["allowed_roles"]),
                      collection=row["collection"], replace_doc_id=doc_id, request_id=rid(request))
    except UploadRejected as exc:
        _rejected(exc, user, request, file.filename or "")


class PermissionsIn(BaseModel):
    allowed_roles: list[str] = Field(min_length=1, max_length=len(ROLES))

    @field_validator("allowed_roles")
    @classmethod
    def known_roles(cls, v):
        bad = [r for r in v if r not in ROLES]
        if bad:
            raise ValueError(f"Unknown roles: {bad}")
        return sorted(set(v) | {"admin"})


@app.patch("/api/documents/{doc_id}/permissions")
def update_permissions(doc_id: str, body: PermissionsIn, request: Request, user: User = Depends(require_admin)):
    visible_doc_or_404(user, doc_id, request)
    return set_permissions(user, doc_id, body.allowed_roles, rid(request))


class ApprovalIn(BaseModel):
    approve: bool


@app.post("/api/documents/{doc_id}/approval")
def approve_document(doc_id: str, body: ApprovalIn, request: Request, user: User = Depends(require_admin)):
    visible_doc_or_404(user, doc_id, request)
    try:
        return set_approval(user, doc_id, body.approve, rid(request))
    except UploadRejected as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@app.delete("/api/documents/{doc_id}")
def remove_document(doc_id: str, request: Request, user: User = Depends(current_user)):
    row = visible_doc_or_404(user, doc_id, request, need_read=False)
    if not (user.is_admin or (row["uploader_id"] == user.id and row["approval_status"] != "approved")):
        audit("access.denied", user_id=user.id, request_id=rid(request), resource=doc_id, decision="deny", op="delete")
        raise HTTPException(status_code=403, detail="Only administrators can delete published documents")
    delete_document(user, doc_id, rid(request))
    return {"ok": True}


@app.get("/api/documents/{doc_id}/download")
def download_document(doc_id: str, request: Request, user: User = Depends(current_user)):
    row = visible_doc_or_404(user, doc_id, request)
    path = stored_path(row)
    if row["status"] == "quarantined" or not path or not path.exists():
        raise HTTPException(status_code=404, detail="Document file not available")
    integrity = verify_integrity(row)
    if not integrity["matches_recorded_hash"]:
        audit("document.integrity_failure", user_id=user.id, request_id=rid(request), resource=doc_id, decision="deny")
        raise HTTPException(status_code=409, detail="Stored file failed its integrity check")
    audit("document.download", user_id=user.id, request_id=rid(request), resource=doc_id, decision="allow")
    safe_name = re.sub(r"[^\w.\-]", "_", row["original_filename"])
    return FileResponse(path, media_type=MIME[row["file_type"]], filename=safe_name,
                        content_disposition_type="attachment",
                        headers={"Content-Security-Policy": "sandbox; default-src 'none'"})


@app.get("/api/documents/{doc_id}/preview")
def preview_document(doc_id: str, request: Request, user: User = Depends(current_user)):
    row = visible_doc_or_404(user, doc_id, request)
    chunks = get_conn().execute("SELECT id, section, section_anchor, page, text, injection_flag FROM chunks "
                                "WHERE document_id=? ORDER BY ordinal", (doc_id,)).fetchall()
    audit("document.preview", user_id=user.id, request_id=rid(request), resource=doc_id, decision="allow")
    return {"document": document_public(row, user),
            "passages": [dict(c) | {"injection_flag": bool(c["injection_flag"])} for c in chunks]}


@app.get("/api/documents/{doc_id}/integrity")
def document_integrity(doc_id: str, request: Request, user: User = Depends(require_admin)):
    return verify_integrity(visible_doc_or_404(user, doc_id, request))


@app.get("/api/passages/{chunk_id}")
def get_passage(chunk_id: str, request: Request, user: User = Depends(current_user)):
    chunk = get_conn().execute("SELECT * FROM chunks WHERE id=?", (chunk_id,)).fetchone()
    doc_id = chunk["document_id"] if chunk else chunk_id.split(":")[0]
    row = visible_doc_or_404(user, doc_id, request)
    if chunk is None:
        raise HTTPException(status_code=404, detail="Passage not found")
    audit("passage.view", user_id=user.id, request_id=rid(request), resource=chunk_id, decision="allow")
    return {"chunk_id": chunk["id"], "document_id": doc_id, "title": row["title"], "section": chunk["section"],
            "section_anchor": chunk["section_anchor"], "page": chunk["page"], "text": chunk["text"],
            "product_version": row["product_version"], "source_id": row["source_id"],
            "injection_flag": bool(chunk["injection_flag"])}


# ------------------------------------------------------------------ Q&A
class AskIn(BaseModel):
    question: str = Field(min_length=1, max_length=settings.max_question_chars)
    top_k: int | None = Field(default=None, ge=1, le=settings.retrieval_max_k)
    debug: bool = False

    @field_validator("question")
    @classmethod
    def not_blank(cls, v):
        v = v.strip()
        if not v:
            raise ValueError("Question must not be blank")
        return v


@app.post("/api/ask")
def ask(body: AskIn, request: Request, user: User = Depends(current_user)):
    rate_limiter.check(f"ask:{user.id}", settings.qa_rate_limit_per_minute)
    return answer_question(user, body.question, top_k=body.top_k, debug=body.debug, request_id=rid(request))


@app.get("/api/history")
def history(user: User = Depends(current_user), limit: int = 20):
    rows = get_conn().execute("SELECT id, created_at, question, response_json FROM qa_history WHERE user_id=? "
                              "ORDER BY created_at DESC LIMIT ?", (user.id, max(1, min(limit, 100)))).fetchall()
    out = []
    for r in rows:
        resp = json.loads(r["response_json"])
        resp.pop("debug", None)
        out.append({"id": r["id"], "created_at": r["created_at"], "question": r["question"], "response": resp})
    return out


@app.get("/api/audit")
def audit_log(user: User = Depends(require_admin), limit: int = 200):
    rows = get_conn().execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (max(1, min(limit, 1000)),)).fetchall()
    return [dict(r) for r in rows]


# ------------------------------------------------------------------ frontend
if FRONTEND_DIST.is_dir():
    app.mount("/assets", StaticFiles(directory=FRONTEND_DIST / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str):
        if path.startswith("api/"):
            raise HTTPException(status_code=404)
        candidate = (FRONTEND_DIST / path).resolve()
        if path and candidate.is_file() and FRONTEND_DIST.resolve() in candidate.parents:
            return FileResponse(candidate)
        return FileResponse(FRONTEND_DIST / "index.html")
