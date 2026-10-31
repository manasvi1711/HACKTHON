"""FR-02 chunking rules and FR-03 retrieval behaviour."""
from app.chunking import chunk_blocks
from app.parsing import Block, parse_markdown, ParseLimits
from conftest import ask


def test_chunks_never_cross_sections_or_pages():
    blocks = [Block("A " * 50, ["Doc", "One"], 1), Block("B " * 50, ["Doc", "Two"], 1), Block("C " * 50, ["Doc", "Two"], 2)]
    chunks = chunk_blocks(blocks, 1000, 100)
    assert [(c.section, c.page) for c in chunks] == [("Doc > One", 1), ("Doc > Two", 1), ("Doc > Two", 2)]


def test_size_and_overlap():
    sentences = [f"Sentence number {i} explains a configuration detail." for i in range(40)]
    chunks = chunk_blocks([Block(" ".join(sentences), ["S"], None)], 300, 80)
    assert len(chunks) > 3
    assert all(len(c.text) <= 300 + 80 for c in chunks)
    # consecutive chunks share overlapping sentence(s)
    assert any(set(a.text.split("\n")) & set(b.text.split("\n")) for a, b in zip(chunks, chunks[1:]))
    assert len({c.section_anchor for c in chunks}) == len(chunks)  # stable unique passage ids


def test_ordered_list_kept_intact_and_markdown_versions_isolated():
    md = b"---\nversion: 2\n---\n# Guide\n## Steps\n1. One\n2. Two\n3. Three\n## Other\nText."
    parsed = parse_markdown(md, ParseLimits(10, 10000))
    chunks = chunk_blocks(parsed.blocks, 900, 150)
    steps = next(c for c in chunks if c.section.endswith("Steps"))
    assert steps.text == "1. One\n2. Two\n3. Three"
    assert parsed.metadata["product_version"] == "2"


def test_paraphrase_retrieves_password_reset(client, tokens):
    res = ask(client, tokens["emma"], "I forgot my login password", debug=True)
    top = res["debug"]["retrieved"][0]
    assert "Password Reset" in top["section"]
    assert res["state"] == "answered" and res["citations"][0]["section"].endswith("Password Reset")


def test_exact_error_code_match(client, tokens):
    res = ask(client, tokens["emma"], "E104", debug=True)
    top = res["debug"]["retrieved"][0]
    assert top["exact_code_match"] and "E104" in top["text"]


def test_top_k_configurable(client, tokens):
    assert len(ask(client, tokens["emma"], "upload size", top_k=2, debug=True)["debug"]["retrieved"]) == 2
    assert len(ask(client, tokens["emma"], "upload size", top_k=9, debug=True)["debug"]["retrieved"]) == 9


def test_debug_view_contains_only_authorized_passages(client, tokens, docs):
    hr_id = docs["HR Compensation Policy"]["id"]
    res = ask(client, tokens["sam"], "salary band L3 annual bonus target", debug=True, top_k=20)
    assert all(r["document_id"] != hr_id for r in res["debug"]["retrieved"])
    res = ask(client, tokens["hana"], "salary band L3 annual bonus target", debug=True, top_k=20)
    assert any(r["document_id"] == hr_id for r in res["debug"]["retrieved"])
