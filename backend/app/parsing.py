"""Text extraction for PDF, HTML, Markdown and TXT.

Produces ordered *blocks* (paragraph / list / table) each tagged with its heading path
and, for PDFs only, its 1-based page number. Nothing here executes document content:
HTML is parsed as inert markup (no scripts, no network), PDFs are read with pypdf's
text extractor (no JavaScript, no form actions).
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from io import BytesIO

from .security import UploadRejected


@dataclass
class Block:
    text: str
    headings: list[str]
    page: int | None = None
    kind: str = "para"  # para | list | table | code


@dataclass
class ParsedDocument:
    title: str | None
    blocks: list[Block]
    page_count: int | None = None
    metadata: dict = field(default_factory=dict)  # product, product_version, effective_date (explicit only)

    @property
    def text(self) -> str:
        return "\n\n".join(b.text for b in self.blocks)


class ParseLimits:
    def __init__(self, max_pages: int, max_chars: int):
        self.max_pages = max_pages
        self.max_chars = max_chars


# ------------------------------------------------------------ explicit metadata
META_PATTERNS = {
    "product_version": re.compile(r"^\s*(?:product\s+)?version\s*[:=]\s*(v?\d+(?:\.\d+)*)\s*$", re.I | re.M),
    "effective_date": re.compile(r"^\s*effective(?:\s+date)?\s*[:=]\s*(\d{4}-\d{2}-\d{2})\s*$", re.I | re.M),
    "product": re.compile(r"^\s*product\s*[:=]\s*([A-Za-z][\w .\-]{1,60}?)\s*$", re.I | re.M),
}


def extract_explicit_metadata(text: str) -> dict:
    head = text[:3000]
    out = {}
    for key, pat in META_PATTERNS.items():
        m = pat.search(head)
        if m:
            out[key] = m.group(1).strip()
    return out


def normalize_version(v: str | None) -> str | None:
    if not v:
        return None
    v = v.strip().lower().lstrip("v")
    while v.endswith(".0"):
        v = v[:-2]
    return v or None


def _clean_ws(s: str) -> str:
    s = s.replace(" ", " ").replace("\r", "")
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r" +([.,;:!?])(?=\s|$)", r"\1", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def _check_size(total: int, limits: ParseLimits) -> None:
    if total > limits.max_chars:
        raise UploadRejected(f"Extracted text exceeds the limit of {limits.max_chars} characters.", "too_large")


# ------------------------------------------------------------ PDF
PAGE_NO_RE = re.compile(r"^\s*(page\s+)?\d+(\s*(of|/)\s*\d+)?\s*$", re.I)


def _looks_like_heading(line: str) -> bool:
    s = line.strip()
    if not s or len(s) > 70 or s[-1] in ".,;:!?)" or not s[0].isupper():
        return False
    if re.match(r"^\d+[.)]\s", s):  # numbered step, not a heading
        return False
    words = s.split()
    return 1 <= len(words) <= 9 and sum(w[0].isupper() for w in words if w[0].isalpha()) >= max(1, len(words) // 2)


def parse_pdf(data: bytes, limits: ParseLimits) -> ParsedDocument:
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    try:
        reader = PdfReader(BytesIO(data), strict=False)
        if reader.is_encrypted:
            try:
                ok = reader.decrypt("")
            except Exception:
                ok = 0
            if not ok:
                raise UploadRejected("The PDF is encrypted/password-protected and cannot be indexed.", "encrypted")
        n_pages = len(reader.pages)
    except UploadRejected:
        raise
    except (PdfReadError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise UploadRejected(f"The PDF could not be parsed ({type(exc).__name__}).", "malformed") from None
    if n_pages == 0:
        raise UploadRejected("The PDF has no pages.", "empty")
    if n_pages > limits.max_pages:
        raise UploadRejected(f"The PDF has {n_pages} pages; the limit is {limits.max_pages}.", "too_large")

    pages: list[list[str]] = []
    total = 0
    for page in reader.pages:
        try:
            txt = page.extract_text() or ""
        except Exception:
            txt = ""
        total += len(txt)
        _check_size(total, limits)
        pages.append([ln.rstrip() for ln in txt.replace("\r", "").split("\n")])

    if sum(len("".join(p).strip()) for p in pages) < 20 * n_pages:
        raise UploadRejected(
            "No extractable text was found. This looks like a scanned/image-only PDF; OCR is not supported "
            "in this prototype. Upload a text-based PDF instead.", "no_text")

    # Repeated header/footer removal: lines that recur at the top/bottom of most pages.
    edge_counts: Counter = Counter()
    for p in pages:
        lines = [ln.strip() for ln in p if ln.strip()]
        for ln in set(lines[:2] + lines[-2:]):
            edge_counts[ln] += 1
    repeated = {ln for ln, c in edge_counts.items() if n_pages >= 2 and c >= max(2, int(0.6 * n_pages))}

    title = None
    try:
        if reader.metadata and reader.metadata.title:
            title = str(reader.metadata.title).strip() or None
    except Exception:
        title = None

    blocks: list[Block] = []
    headings: list[str] = []
    for page_no, lines in enumerate(pages, start=1):
        buf: list[str] = []
        kind = "para"

        def flush():
            nonlocal buf, kind
            text = _clean_ws(" ".join(buf) if kind == "para" else "\n".join(buf))
            if text:
                blocks.append(Block(text, list(headings), page_no, kind))
            buf, kind = [], "para"

        for raw in lines:
            line = raw.strip()
            if not line:
                flush()
                continue
            if line in repeated or PAGE_NO_RE.match(line):
                continue
            if _looks_like_heading(line):
                flush()
                headings = [line] if not headings or len(blocks) == 0 else [headings[0], line]
                if title is None:
                    title = line
                    headings = [line]
                continue
            is_item = bool(re.match(r"^(\d+[.)]|[-*•])\s+", line))
            if is_item and kind != "list":
                flush()
                kind = "list"
            elif not is_item and kind == "list" and buf and not raw.startswith(" "):
                # continuation lines of a list item stay with the list
                if re.match(r"^[a-z(]", line):
                    buf[-1] += " " + line
                    continue
                flush()
            buf.append(line)
        flush()
    return ParsedDocument(title, blocks, n_pages, extract_explicit_metadata("\n".join("\n".join(p) for p in pages)))


# ------------------------------------------------------------ HTML
DROP_TAGS = ["script", "style", "noscript", "iframe", "object", "embed", "template", "svg", "canvas",
             "nav", "header", "footer", "aside", "form", "button", "input", "select", "textarea", "link", "meta"]
HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
CONTAINER_TAGS = {"div", "section", "article", "main", "body", "html", "span", "details", "summary", "center"}
TEXT_TAGS = {"p", "pre", "blockquote", "dd", "dt", "figcaption", "address"}


def parse_html(data: bytes, limits: ParseLimits) -> ParsedDocument:
    from bs4 import BeautifulSoup, NavigableString, Tag

    text = data.decode("utf-8-sig")
    _check_size(len(text), limits)
    # html.parser is a pure-Python tokenizer: no script execution, no resource fetching.
    soup = BeautifulSoup(text, "html.parser")

    meta = {}
    for name, key in (("product-version", "product_version"), ("version", "product_version"),
                      ("effective-date", "effective_date"), ("product", "product")):
        tag = soup.find("meta", attrs={"name": name})
        if tag and tag.get("content") and key not in meta:
            meta[key] = str(tag["content"]).strip()[:60]
    title = soup.title.get_text(strip=True) if soup.title else None

    for t in soup(DROP_TAGS):
        t.decompose()
    for el in soup.find_all(attrs={"role": ["navigation", "banner", "contentinfo"]}):
        el.decompose()

    blocks: list[Block] = []
    level_heads: dict[int, str] = {}

    def heads() -> list[str]:
        return [level_heads[k] for k in sorted(level_heads)]

    def add(text: str, kind: str = "para"):
        text = _clean_ws(text)
        if text:
            blocks.append(Block(text, heads(), None, kind))

    def table_text(table: Tag) -> str:
        rows = table.find_all("tr")
        header = [c.get_text(" ", strip=True) for c in rows[0].find_all(["th", "td"])] if rows else []
        has_header = bool(rows and rows[0].find("th"))
        out = []
        for r in rows[1:] if has_header else rows:
            cells = [c.get_text(" ", strip=True) for c in r.find_all(["th", "td"])]
            if has_header and len(header) == len(cells):
                out.append("; ".join(f"{h}: {v}" for h, v in zip(header, cells)))
            else:
                out.append(" | ".join(cells))
        cap = table.find("caption")
        return ((cap.get_text(" ", strip=True) + "\n") if cap else "") + "\n".join(out)

    def walk(node: Tag):
        inline_buf: list[str] = []
        for child in node.children:
            if isinstance(child, NavigableString):
                if type(child).__name__ == "NavigableString" and child.strip():
                    inline_buf.append(str(child))
                continue
            if not isinstance(child, Tag):
                continue
            name = child.name.lower()
            if name in {"a", "b", "strong", "em", "i", "code", "span", "small", "u", "br", "abbr", "kbd"}:
                inline_buf.append(child.get_text(" "))
                continue
            if inline_buf:
                add(" ".join(inline_buf))
                inline_buf = []
            if name in HEADING_TAGS:
                lvl = int(name[1])
                for k in [k for k in level_heads if k >= lvl]:
                    del level_heads[k]
                level_heads[lvl] = _clean_ws(child.get_text(" "))
            elif name == "table":
                add(table_text(child), "table")
            elif name in ("ul", "ol"):
                items = child.find_all("li", recursive=False)
                lines = [f"{i}. " if name == "ol" else "- " for i in range(1, len(items) + 1)]
                add("\n".join(p + _clean_ws(li.get_text(" ")) for p, li in zip(lines, items)), "list")
            elif name == "dl" or name in TEXT_TAGS:
                add(child.get_text(" "), "code" if name == "pre" else "para")
            else:
                walk(child)
        if inline_buf:
            add(" ".join(inline_buf))

    walk(soup.body or soup)
    if not title:
        h1 = next((b.headings[0] for b in blocks if b.headings), None)
        title = h1
    explicit = extract_explicit_metadata("\n".join(b.text for b in blocks[:10]))
    for k, v in explicit.items():
        meta.setdefault(k, v)
    return ParsedDocument(title, blocks, None, meta)


# ------------------------------------------------------------ Markdown
def parse_markdown(data: bytes, limits: ParseLimits) -> ParsedDocument:
    text = data.decode("utf-8-sig").replace("\r\n", "\n")
    _check_size(len(text), limits)
    meta: dict = {}
    fm = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    if fm:
        for line in fm.group(1).splitlines():
            if ":" in line:
                k, _, v = line.partition(":")
                k = k.strip().lower().replace(" ", "_").replace("-", "_")
                v = v.strip().strip('"').strip("'")
                if k in ("version", "product_version"):
                    meta["product_version"] = v
                elif k in ("effective_date", "effective"):
                    meta["effective_date"] = v
                elif k in ("title", "product"):
                    meta[k] = v
        text = text[fm.end():]

    blocks: list[Block] = []
    level_heads: dict[int, str] = {}
    title = meta.pop("title", None)
    buf: list[str] = []
    kind = "para"
    in_code = False

    def heads():
        return [level_heads[k] for k in sorted(level_heads)]

    def flush():
        nonlocal buf, kind
        t = "\n".join(buf).strip() if kind in ("list", "table", "code") else _clean_ws(" ".join(buf))
        if t:
            blocks.append(Block(t, heads(), None, kind))
        buf, kind = [], "para"

    for line in text.split("\n"):
        if line.strip().startswith("```"):
            if in_code:
                buf.append(line)
                flush()
                in_code = False
            else:
                flush()
                in_code, kind = True, "code"
                buf.append(line)
            continue
        if in_code:
            buf.append(line)
            continue
        m = re.match(r"^(#{1,6})\s+(.*?)\s*#*\s*$", line)
        if m:
            flush()
            lvl = len(m.group(1))
            for k in [k for k in level_heads if k >= lvl]:
                del level_heads[k]
            level_heads[lvl] = m.group(2)
            if title is None and lvl == 1:
                title = m.group(2)
            continue
        if not line.strip():
            if kind != "list":
                flush()
            continue
        line_kind = ("list" if re.match(r"^\s*(\d+[.)]|[-*+])\s+", line)
                     else "table" if line.strip().startswith("|") else "para")
        if line_kind == "para" and kind == "list" and line.startswith((" ", "\t")):
            buf.append(line.strip())  # list item continuation
            continue
        if line_kind != kind:
            flush()
            kind = line_kind
        buf.append(line.rstrip())
    flush()
    explicit = extract_explicit_metadata("\n".join(b.text for b in blocks[:10]))
    for k, v in explicit.items():
        meta.setdefault(k, v)
    return ParsedDocument(title, blocks, None, meta)


# ------------------------------------------------------------ TXT
def parse_txt(data: bytes, limits: ParseLimits) -> ParsedDocument:
    text = data.decode("utf-8-sig").replace("\r\n", "\n")
    _check_size(len(text), limits)
    lines = text.split("\n")
    blocks: list[Block] = []
    headings: list[str] = []
    title = None
    buf: list[str] = []

    def flush():
        nonlocal buf
        is_list = buf and all(re.match(r"^\s*(\d+[.)]|[-*])\s+", b) for b in buf[:1])
        t = "\n".join(buf).strip() if is_list else _clean_ws(" ".join(buf))
        if t:
            blocks.append(Block(t, list(headings), None, "list" if is_list else "para"))
        buf = []

    i = 0
    while i < len(lines):
        line = lines[i].rstrip()
        nxt = lines[i + 1].strip() if i + 1 < len(lines) else ""
        underline = re.fullmatch(r"[=\-]{3,}", nxt) is not None
        caps = (line.strip() and line.strip().isupper() and len(line.strip()) <= 70
                and re.search(r"[A-Z]{3}", line) and not re.match(r"^[A-Z]\d{2,}", line.strip()))
        if line.strip() and (underline or caps):
            flush()
            h = line.strip().title() if caps and not underline else line.strip()
            if nxt.startswith("=") or title is None:
                title = title or h
                headings = [h]
            else:
                headings = [headings[0], h] if headings else [h]
            i += 2 if underline else 1
            continue
        if not line.strip():
            flush()
        else:
            if re.match(r"^\s*(\d+[.)]|[-*])\s+", line) and buf and not re.match(r"^\s*(\d+[.)]|[-*])\s+", buf[0]):
                flush()
            buf.append(line)
        i += 1
    flush()
    return ParsedDocument(title, blocks, None, extract_explicit_metadata(text))


PARSERS = {"pdf": parse_pdf, "html": parse_html, "markdown": parse_markdown, "txt": parse_txt}


def parse_document(file_type: str, data: bytes, max_pages: int, max_chars: int) -> ParsedDocument:
    """Entry point; also the function executed in the sandboxed worker process."""
    parsed = PARSERS[file_type](data, ParseLimits(max_pages, max_chars))
    parsed.blocks = [b for b in parsed.blocks if b.text.strip()]
    if not parsed.blocks:
        raise UploadRejected("No readable text content was found in the document.", "empty")
    return parsed
