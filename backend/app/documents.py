"""Document ingestion, replacement, deletion and approval."""
from __future__ import annotations

import concurrent.futures as cf
import hashlib
import json
import logging
import os
import threading
import uuid

import numpy as np

from .audit import audit, now_iso
from .auth import User
from .chunking import chunk_blocks
from .config import settings
from .db import bump_index_epoch, get_conn, tx
from .embeddings import get_embedder
from .parsing import ParsedDocument, normalize_version, parse_document
from .security import UploadRejected, detect_injection, detect_secrets, display_filename, sniff_file_type

log = logging.getLogger(__name__)
EXT = {"pdf": ".pdf", "html": ".html", "markdown": ".md", "txt": ".txt"}
MIME = {"pdf": "application/pdf", "html": "text/html", "markdown": "text/markdown", "txt": "text/plain"}


# ------------------------------------------------------------- sandboxed parsing
def _limit_worker_resources() -> None:  # pragma: no cover - runs in the child process
    try:
        import resource  # POSIX only

        mem = 1024 * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (mem, mem))
    except Exception:
        pass  # Windows: rely on the timeout and input limits only (documented)


_pool: cf.ProcessPoolExecutor | None = None
_pool_lock = threading.Lock()


def _get_pool() -> cf.ProcessPoolExecutor:
    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = cf.ProcessPoolExecutor(max_workers=1, initializer=_limit_worker_resources)
        return _pool


def _kill_pool() -> None:
    global _pool
    with _pool_lock:
        if _pool is not None:
            for p in list(getattr(_pool, "_processes", {}).values()):
                p.terminate()
            _pool.shutdown(wait=False, cancel_futures=True)
            _pool = None


def parse_with_limits(file_type: str, data: bytes) -> ParsedDocument:
    args = (file_type, data, settings.max_pdf_pages, settings.max_extracted_chars)
    if not settings.parse_in_subprocess:
        return parse_document(*args)
    fut = _get_pool().submit(parse_document, *args)
    try:
        return fut.result(timeout=settings.parse_timeout_s)
    except cf.TimeoutError:
        _kill_pool()
        raise UploadRejected(f"Parsing exceeded the {settings.parse_timeout_s:.0f}s time limit.", "timeout")
    except UploadRejected:
        raise
    except cf.process.BrokenProcessPool:
        _kill_pool()
        raise UploadRejected("The parser process failed (resource limit or malformed file).", "malformed")
    except Exception as exc:
        raise UploadRejected(f"The document could not be parsed ({type(exc).__name__}).", "malformed")


# ------------------------------------------------------------- helpers
def _doc_row(doc_id: str):
    return get_conn().execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()


def stored_path(row):
    return settings.files_dir / row["stored_filename"] if row and row["stored_filename"] else None


def _write_file(doc_id: str, file_type: str, data: bytes) -> str:
    settings.files_dir.mkdir(parents=True, exist_ok=True)
    name = f"{doc_id}{EXT[file_type]}"  # generated; never derived from the user's filename
    path = settings.files_dir / name
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)
    return name


def _prepare_chunks(parsed: ParsedDocument) -> list[tuple]:
    """Chunk + embed outside the DB transaction. Returns rows minus document id."""
    chunks = chunk_blocks(parsed.blocks, settings.chunk_size_chars, settings.chunk_overlap_chars)
    # Heading path is prepended for embedding only; stored text stays an exact excerpt.
    vecs = get_embedder().embed_documents([(c.section + "\n" + c.text) if c.section else c.text for c in chunks])
    return [(c, np.asarray(v, dtype=np.float32).tobytes(), bool(detect_injection(c.text))) for c, v in zip(chunks, vecs)]


def _write_chunks(conn, doc_id: str, prepared: list[tuple]) -> None:
    conn.execute("DELETE FROM chunks WHERE document_id=?", (doc_id,))
    for c, blob, flag in prepared:
        conn.execute(
            "INSERT INTO chunks(id, document_id, ordinal, section, section_anchor, page, text, embedding, injection_flag) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (f"{doc_id}:c{c.ordinal}", doc_id, c.ordinal, c.section, c.section_anchor, c.page, c.text, blob, int(flag)))


# ------------------------------------------------------------- ingestion
def ingest(user: User, filename: str, data: bytes, allowed_roles: list[str], *, collection: str = "trusted",
           title: str | None = None, product: str | None = None, product_version: str | None = None,
           effective_date: str | None = None, replace_doc_id: str | None = None,
           request_id: str | None = None) -> dict:
    """Validates, parses, screens and indexes a document. Raises UploadRejected on
    validation failures (nothing is stored in that case)."""
    if len(data) > settings.max_upload_bytes:
        raise UploadRejected(f"File exceeds the {settings.max_upload_bytes // (1024 * 1024)} MB upload limit.", "too_large")
    safe_name = display_filename(filename)
    file_type = sniff_file_type(safe_name, data)
    content_hash = hashlib.sha256(data).hexdigest()

    dup = get_conn().execute("SELECT id, uploader_id FROM documents WHERE content_hash=? AND status!='deleted' AND id!=?",
                             (content_hash, replace_doc_id or "")).fetchone()
    if dup:
        raise UploadRejected("A document with identical content already exists in the collection.", "duplicate")

    parsed = parse_with_limits(file_type, data)
    full_text = parsed.text
    secret_types = detect_secrets(full_text)
    if secret_types and settings.secret_policy == "reject":
        audit("document.rejected", user_id=user.id, request_id=request_id, decision="secret_detected",
              filename=safe_name, secret_types=secret_types)
        raise UploadRejected("The file appears to contain credentials or secrets and was rejected. "
                             "Remove them and upload again.", "secret_detected")

    meta = parsed.metadata
    doc_id = replace_doc_id or f"doc-{uuid.uuid4().hex[:12]}"
    now = now_iso()
    flags: list[str] = []
    status = "ready"
    if secret_types:
        flags.append("possible_secret:" + ",".join(secret_types))
        status = "quarantined"

    record = {
        "title": (title or parsed.title or safe_name.rsplit(".", 1)[0])[:200],
        "product": (product or meta.get("product") or None),
        "product_version": (product_version or meta.get("product_version") or None),
        "effective_date": (effective_date or meta.get("effective_date") or None),
    }
    prepared = [] if status == "quarantined" else _prepare_chunks(parsed)
    n_chunks = len(prepared)
    if any(flag for _, _, flag in prepared):
        flags.append("possible_prompt_injection")
    old_path = stored_path(_doc_row(replace_doc_id)) if replace_doc_id else None
    stored = _write_file(doc_id, file_type, data)
    if old_path and old_path.name != stored and old_path.exists():
        old_path.unlink()
    with tx() as conn:
        if replace_doc_id:
            old = _doc_row(replace_doc_id)
            conn.execute("INSERT INTO document_versions(document_id, content_hash, replaced_at, replaced_by) VALUES (?,?,?,?)",
                         (replace_doc_id, old["content_hash"], now, user.id))
        auto_approve = user.is_admin and not flags
        approval = "approved" if auto_approve else "pending"
        values = (record["title"], safe_name, f"upload://{doc_id}/{safe_name}", file_type, stored, record["product"],
                  record["product_version"], record["effective_date"], now, content_hash,
                  content_hash if auto_approve else None, json.dumps(allowed_roles), status, approval,
                  user.id if auto_approve else None, now if auto_approve else None, collection,
                  parsed.page_count, n_chunks, json.dumps(flags))
        if replace_doc_id:
            conn.execute(
                "UPDATE documents SET title=?, original_filename=?, source_id=?, file_type=?, stored_filename=?, product=?, "
                "product_version=?, effective_date=?, updated_at=?, content_hash=?, baseline_hash=?, allowed_roles=?, "
                "status=?, approval_status=?, approved_by=?, approved_at=?, collection=?, page_count=?, chunk_count=?, "
                "security_flags=?, error=NULL WHERE id=?", values + (doc_id,))
        else:
            conn.execute(
                "INSERT INTO documents(title, original_filename, source_id, file_type, stored_filename, product, "
                "product_version, effective_date, updated_at, content_hash, baseline_hash, allowed_roles, status, "
                "approval_status, approved_by, approved_at, collection, page_count, chunk_count, security_flags, "
                "uploader_id, uploaded_at, id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                values + (user.id, now, doc_id))
        _write_chunks(conn, doc_id, prepared)
    bump_index_epoch()
    audit("document.replaced" if replace_doc_id else "document.uploaded", user_id=user.id, request_id=request_id,
          resource=doc_id, decision=status, approval=approval, file_type=file_type, chunks=n_chunks,
          content_hash=content_hash, flags=flags)
    return document_public(_doc_row(doc_id), user)


def delete_document(user: User, doc_id: str, request_id: str | None = None) -> None:
    row = _doc_row(doc_id)
    path = stored_path(row)
    with tx() as conn:
        conn.execute("DELETE FROM chunks WHERE document_id=?", (doc_id,))
        conn.execute("UPDATE documents SET status='deleted', chunk_count=0, stored_filename=NULL, updated_at=? WHERE id=?",
                     (now_iso(), doc_id))
    if path and path.exists():
        path.unlink()
    bump_index_epoch()
    audit("document.deleted", user_id=user.id, request_id=request_id, resource=doc_id, decision="deleted")


def set_permissions(user: User, doc_id: str, roles: list[str], request_id: str | None = None) -> dict:
    get_conn().execute("UPDATE documents SET allowed_roles=?, updated_at=? WHERE id=?", (json.dumps(roles), now_iso(), doc_id))
    bump_index_epoch()
    audit("document.permissions_changed", user_id=user.id, request_id=request_id, resource=doc_id, decision="updated",
          allowed_roles=roles)
    return document_public(_doc_row(doc_id), user)


def set_approval(user: User, doc_id: str, approve: bool, request_id: str | None = None) -> dict:
    row = _doc_row(doc_id)
    if approve and row["status"] != "ready":
        raise UploadRejected(f"Only successfully processed documents can be approved (status: {row['status']}).", "invalid_state")
    if approve:
        get_conn().execute("UPDATE documents SET approval_status='approved', approved_by=?, approved_at=?, baseline_hash=content_hash "
                           "WHERE id=?", (user.id, now_iso(), doc_id))
    else:
        get_conn().execute("UPDATE documents SET approval_status='rejected', approved_by=?, approved_at=? WHERE id=?",
                           (user.id, now_iso(), doc_id))
    bump_index_epoch()
    audit("document.approval", user_id=user.id, request_id=request_id, resource=doc_id,
          decision="approved" if approve else "rejected")
    return document_public(_doc_row(doc_id), user)


def verify_integrity(row) -> dict:
    path = stored_path(row)
    if not path or not path.exists():
        return {"ok": False, "reason": "stored file missing"}
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    return {"ok": actual == row["content_hash"] and (row["baseline_hash"] in (None, actual)),
            "matches_recorded_hash": actual == row["content_hash"],
            "matches_approved_baseline": row["baseline_hash"] == actual if row["baseline_hash"] else None}


def document_public(row, user: User) -> dict:
    d = {
        "id": row["id"], "title": row["title"], "original_filename": row["original_filename"],
        "source_id": row["source_id"], "file_type": row["file_type"], "product": row["product"],
        "product_version": row["product_version"], "effective_date": row["effective_date"],
        "uploaded_at": row["uploaded_at"], "updated_at": row["updated_at"], "status": row["status"],
        "approval_status": row["approval_status"], "page_count": row["page_count"],
        "chunk_count": row["chunk_count"], "content_hash": row["content_hash"],
        "collection": row["collection"], "security_flags": json.loads(row["security_flags"] or "[]"),
        "uploader_id": row["uploader_id"], "error": row["error"],
        "normalized_version": normalize_version(row["product_version"]),
    }
    if user.is_admin or row["uploader_id"] == user.id:
        d["allowed_roles"] = json.loads(row["allowed_roles"])
        d["approved_by"] = row["approved_by"]
        d["baseline_hash"] = row["baseline_hash"]
    return d
