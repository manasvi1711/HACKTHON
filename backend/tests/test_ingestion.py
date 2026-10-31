"""FR-01/FR-02 and SEC-03/SEC-04/SEC-05: ingestion, metadata, safe upload handling."""
import io
import uuid

from pypdf import PdfWriter
from reportlab.pdfgen import canvas

from app.config import settings
from app.db import get_conn
from conftest import upload


def _uniq(text: str) -> bytes:
    return (text + f"\n\nUnique test marker {uuid.uuid4().hex}.\n").encode()


def test_all_sample_formats_ingested_with_metadata(docs):
    expected = {"Account Access Guide": "pdf", "Billing FAQ": "html", "Lumora Workspace Product Manual": "markdown",
                "Lumora Workspace Release Notes v3": "markdown", "Lumora Workspace Troubleshooting Guide": "txt",
                "HR Compensation Policy": "pdf"}
    for title, ftype in expected.items():
        d = docs[title]
        assert d["file_type"] == ftype and d["status"] == "ready" and d["chunk_count"] > 0
        assert len(d["content_hash"]) == 64 and d["uploader_id"] == "u-adam" and d["uploaded_at"]
    assert docs["Lumora Workspace Product Manual"]["product_version"] == "2"
    assert docs["Lumora Workspace Release Notes v3"]["product_version"] == "3"
    assert docs["Billing FAQ"]["effective_date"] == "2026-01-10"
    # Not invented when absent:
    assert docs["Account Access Guide"]["product_version"] is None
    assert docs["Lumora Workspace Troubleshooting Guide"]["effective_date"] is None


def test_pdf_pages_and_sections_preserved_and_noise_removed(docs):
    rows = get_conn().execute("SELECT * FROM chunks WHERE document_id=? ORDER BY ordinal",
                              (docs["Account Access Guide"]["id"],)).fetchall()
    assert {r["page"] for r in rows} == {1, 2}
    reset = next(r for r in rows if "Password Reset" in r["section"])
    assert reset["page"] == 1 and "expire after 30 minutes" in reset["text"]
    # ordered steps stay together in one chunk
    assert all(f"{n}." in reset["text"] for n in (1, 2, 3, 4))
    joined = " ".join(r["text"] for r in rows)
    assert "Page 1 of 2" not in joined and "FICTIONAL DEMONSTRATION MATERIAL" not in joined  # repeated footer


def test_non_pdf_formats_have_no_fabricated_pages(docs):
    for title in ("Billing FAQ", "Lumora Workspace Product Manual", "Lumora Workspace Troubleshooting Guide"):
        rows = get_conn().execute("SELECT page, section_anchor FROM chunks WHERE document_id=?", (docs[title]["id"],)).fetchall()
        assert all(r["page"] is None for r in rows) and all(r["section_anchor"] for r in rows)


def test_html_scripts_navigation_not_indexed(docs):
    text = " ".join(r["text"] for r in get_conn().execute(
        "SELECT text FROM chunks WHERE document_id=?", (docs["Billing FAQ"]["id"],)))
    assert "SCRIPT_CONTENT_SHOULD_NOT_BE_INDEXED" not in text and "__billingFaqScriptRan" not in text
    assert "Home | Products" not in text and "Privacy | Terms" not in text
    assert "Plan: Starter; Price: $8 per user" in text  # table context preserved


def test_unsupported_and_mismatched_types_rejected(client, tokens):
    r = upload(client, tokens["adam"], "tool.exe", b"MZ\x90\x00binary")
    assert r.status_code == 415 and r.json()["detail"]["code"] == "unsupported_type"
    r = upload(client, tokens["adam"], "fake.pdf", b"just some text pretending to be a pdf")
    assert r.status_code == 415 and r.json()["detail"]["code"] == "type_mismatch"
    r = upload(client, tokens["adam"], "binary.txt", b"abc\x00\x01\x02def")
    assert r.status_code == 415
    r = upload(client, tokens["adam"], "page.html", b"no markup here at all")
    assert r.status_code == 415


def test_empty_malformed_encrypted_and_scanned_pdfs_fail_safely(client, tokens):
    assert upload(client, tokens["adam"], "empty.md", b"   \n ").json()["detail"]["code"] == "empty"
    r = upload(client, tokens["adam"], "broken.pdf", b"%PDF-1.4\n" + uuid.uuid4().bytes + b"\nnot really a pdf")
    assert r.status_code == 422 and r.json()["detail"]["code"] in ("malformed", "empty", "no_text")

    w = PdfWriter()
    w.add_blank_page(200, 200)
    w.encrypt("secret-pw")
    buf = io.BytesIO()
    w.write(buf)
    r = upload(client, tokens["adam"], "locked.pdf", buf.getvalue())
    assert r.status_code == 422 and r.json()["detail"]["code"] == "encrypted"

    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    c.rect(100, 100, 300, 300, fill=1)  # image-like page, no text layer
    c.drawString(1, 1, uuid.uuid4().hex[:4])
    c.showPage()
    c.save()
    r = upload(client, tokens["adam"], "scan.pdf", buf.getvalue())
    assert r.status_code == 422 and r.json()["detail"]["code"] == "no_text"
    assert "OCR is not supported" in r.json()["detail"]["message"]


def test_oversized_upload_rejected(client, tokens, monkeypatch):
    monkeypatch.setattr(settings, "max_upload_bytes", 1000)
    r = upload(client, tokens["adam"], "big.txt", b"a" * 2000)
    assert r.status_code == 413


def test_extracted_text_limit(client, tokens, monkeypatch):
    monkeypatch.setattr(settings, "max_extracted_chars", 100)
    r = upload(client, tokens["adam"], "long.md", _uniq("# T\n\n" + "word " * 100))
    assert r.status_code == 413 and r.json()["detail"]["code"] == "too_large"


def test_parse_timeout_kills_hung_parser(client, tokens, monkeypatch):
    import time

    import slow_parser
    from app import documents

    monkeypatch.setattr(settings, "parse_timeout_s", 1.0)
    monkeypatch.setattr(documents, "parse_document", slow_parser.hanging_parse)
    start = time.monotonic()
    r = upload(client, tokens["adam"], "slow.md", _uniq("# Slow\n\ncontent"))
    assert r.status_code == 422 and r.json()["detail"]["code"] == "timeout"
    assert time.monotonic() - start < 15
    monkeypatch.undo()
    # the worker pool was killed and is recreated for the next upload
    assert upload(client, tokens["adam"], "after.md", _uniq("# After\n\ncontent")).status_code == 201


def test_duplicate_detected(client, tokens):
    data = _uniq("# Dup\n\nDuplicate detection test.")
    assert upload(client, tokens["adam"], "dup1.md", data).status_code == 201
    r = upload(client, tokens["adam"], "dup2.md", data)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "duplicate"


def test_path_traversal_filename_neutralised(client, tokens):
    r = upload(client, tokens["adam"], "../../../etc/evil<script>.md", _uniq("# Evil\n\nharmless"))
    assert r.status_code == 201
    doc = r.json()
    assert "/" not in doc["original_filename"] and "<" not in doc["original_filename"]
    row = get_conn().execute("SELECT stored_filename FROM documents WHERE id=?", (doc["id"],)).fetchone()
    assert row["stored_filename"] == f"{doc['id']}.md"
    assert (settings.files_dir / row["stored_filename"]).exists()


def test_secret_quarantined_and_not_searchable(client, tokens):
    fake_key = "AKIA" + "ABCDEFGHIJKLMNOP"  # dummy, synthetic
    r = upload(client, tokens["adam"], "leaky.md", _uniq(f"# Deploy notes\n\nUse key {fake_key} for the quokka bucket."))
    assert r.status_code == 201
    doc = r.json()
    assert doc["status"] == "quarantined" and doc["chunk_count"] == 0
    assert any(f.startswith("possible_secret:aws_access_key_id") for f in doc["security_flags"])
    from conftest import ask
    res = ask(client, tokens["emma"], "Which key is used for the quokka bucket?")
    assert res["state"] == "not_found" and fake_key not in str(res)
    # nobody but admins can see the quarantined doc
    assert all(d["id"] != doc["id"] for d in client.get("/api/documents", headers=tokens["emma"]).json())
    assert client.get(f"/api/documents/{doc['id']}/download", headers=tokens["adam"]).status_code == 404


def test_secret_reject_policy(client, tokens, monkeypatch):
    monkeypatch.setattr(settings, "secret_policy", "reject")
    r = upload(client, tokens["adam"], "leaky2.txt", _uniq("password = Sup3rS3cretValue123"))
    assert r.status_code == 422 and r.json()["detail"]["code"] == "secret_detected"


def test_only_authorized_roles_publish(client, tokens):
    r = upload(client, tokens["emma"], "emp.md", _uniq("# Employee upload"))
    assert r.status_code == 403
    r = upload(client, tokens["sam"], "sales.md", _uniq("# Sales doc\n\nThe zebra discount is 7 percent."), roles="hr")
    assert r.status_code == 422  # uploader must keep access
    r = upload(client, tokens["sam"], "sales.md", _uniq("# Sales doc\n\nThe zebra discount is 7 percent."), roles="sales")
    assert r.status_code == 201 and r.json()["approval_status"] == "pending"


def test_non_admin_upload_requires_approval_before_search(client, tokens):
    from conftest import ask
    r = upload(client, tokens["hana"], "hr_note.md", _uniq("# HR note\n\nThe walrus allowance is 300 credits."), roles="hr,employee")
    doc = r.json()
    assert doc["approval_status"] == "pending"
    assert ask(client, tokens["emma"], "What is the walrus allowance?")["state"] == "not_found"
    ok = client.post(f"/api/documents/{doc['id']}/approval", json={"approve": True}, headers=tokens["hana"])
    assert ok.status_code == 403  # only admins approve
    ok = client.post(f"/api/documents/{doc['id']}/approval", json={"approve": True}, headers=tokens["adam"])
    assert ok.status_code == 200 and ok.json()["baseline_hash"] == doc["content_hash"]
    res = ask(client, tokens["emma"], "What is the walrus allowance?")
    assert res["state"] == "answered" and "300 credits" in res["answer"][0]["text"]


def test_upload_metadata_form_validated(client, tokens):
    r = upload(client, tokens["adam"], "m.md", _uniq("# M"), product_version="<script>")
    assert r.status_code == 422
    r = upload(client, tokens["adam"], "m.md", _uniq("# M"), product_version="4.1", effective_date="2026-05-01")
    assert r.status_code == 201 and r.json()["product_version"] == "4.1"
