"""Security audit log. Stores metadata only: never credentials, tokens, or document text."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

from .config import settings
from .db import get_conn

log = logging.getLogger("audit")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def audit(action: str, *, user_id: str | None, request_id: str | None = None, resource: str | None = None,
          decision: str | None = None, **details) -> None:
    payload = json.dumps(details, default=str, sort_keys=True) if details else None
    get_conn().execute(
        "INSERT INTO audit_log(ts, request_id, user_id, action, resource, decision, details) VALUES (?,?,?,?,?,?,?)",
        (now_iso(), request_id, user_id, action, resource, decision, payload),
    )
    log.info("audit action=%s user=%s resource=%s decision=%s", action, user_id, resource, decision)


def purge_expired() -> int:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=settings.audit_retention_days)).isoformat(timespec="seconds")
    cur = get_conn().execute("DELETE FROM audit_log WHERE ts < ?", (cutoff,))
    return cur.rowcount
