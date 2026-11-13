import httpx
import pytest

from mdfi_connect import server
from mdfi_connect.client import ApiClient
from mdfi_connect.render import render_document, render_pages


def page(n, state="ready", score=95, flags=()):
    return {
        "number": n,
        "state": state,
        "score": score if state == "ready" else None,
        "charged_tier": 1 if state == "ready" else None,
        "kept_tier": 1 if state == "ready" else None,
        "flags": list(flags),
    }


def manifest(**kw):
    base = {
        "page_count": 2,
        "weighted_pages": 2,
        "approx_tokens": 10,
        "pages": [page(1), page(2)],
        "outline": [
            {"title": "Intro", "level": 1, "start_page": 1, "end_page": 1},
            {"title": "Results", "level": 1, "start_page": 2, "end_page": 2},
        ],
        "warnings": [],
        "tables": [],
        "first_text": "--- page 1 ---\nHello\n--- page 2 ---\nWorld",
        "first_text_pages": [1, 2],
        "complete": True,
    }
    base.update(kw)
    return base


READY = {"id": "d1", "status": "ready", "source": "a.docx", "error": None, "manifest": manifest()}


class Fake:
    def __init__(self, docs, pages=None):
        self.docs = list(docs)
        self.submits = []
        self.doc_calls = 0
        self._pages = pages

    def submit(self, path):
        self.submits.append(path)
        return self.docs[0]

    def document(self, doc_id):
        self.doc_calls += 1
        if not self.docs:
            return {}
        return self.docs[min(self.doc_calls, len(self.docs) - 1)]

    def pages(self, doc_id, *, range=None, section=None):
        self.args = (doc_id, range, section)
        return self._pages


@pytest.fixture
def sleeps(monkeypatch):
    rec = []
    monkeypatch.setattr(server, "_sleep", rec.append)
    return rec


def use(monkeypatch, fake):
    monkeypatch.setattr(server, "_client", lambda: fake)


def test_read_document_small_ready_returns_whole_text(tmp_path, monkeypatch, sleeps):
    f = tmp_path / "a.docx"
    f.write_bytes(b"x")
    use(monkeypatch, Fake([READY]))
    out = server.read_document(str(f))
    assert "document_id: d1" in out
    assert "status: ready" in out
    assert "ready 2 of 2" in out
    assert "charged: 2 weighted pages" in out
    assert "--- page 2 ---\nWorld" in out
    assert "STILL CONVERTING" not in out
    assert sleeps == []


CONVERTING = {
    "id": "d2",
    "status": "converting",
    "source": "big.pdf",
    "error": None,
    "manifest": manifest(
        page_count=3,
        weighted_pages=1,
        pages=[page(1), page(2, "converting"), page(3, "converting")],
        outline=[
            {"title": "Intro", "level": 1, "start_page": 1, "end_page": 1},
            {"title": "Data", "level": 1, "start_page": 2, "end_page": 3},
        ],
        first_text="--- page 1 ---\nHello",
        first_text_pages=[1],
        complete=False,
    ),
}


def test_read_document_converting_returns_outline_and_hint(tmp_path, monkeypatch, sleeps):
    f = tmp_path / "big.pdf"
    f.write_bytes(b"x")
    use(monkeypatch, Fake([CONVERTING]))
    out = server.read_document(str(f))
    assert "ready 1 of 3" in out
    assert "- Data (p2-3) (converting)" in out
    assert "- Intro (p1)\n" in out
    assert "--- page 1 ---\nHello" in out
    assert "STILL CONVERTING" in out
    assert "charged: nothing yet" in out and "weighted pages" not in out
    assert sleeps == []  # page 1 is readable, so no waiting


def test_polling_is_bounded_and_stops_early(tmp_path, monkeypatch, sleeps):
    f = tmp_path / "a.docx"
    f.write_bytes(b"x")
    waiting = {
        "id": "d3",
        "status": "converting",
        "manifest": manifest(pages=[page(1, "converting")], outline=[]),
    }
    use(monkeypatch, Fake([waiting]))
    out = server.read_document(str(f))
    assert sum(sleeps) == 20 and sleeps[:2] == [1.0, 2.0]
    assert "STILL CONVERTING" in out
    sleeps.clear()
    use(monkeypatch, Fake([waiting, waiting, READY]))
    out = server.read_document(str(f))
    assert sleeps == [1.0, 2.0]
    assert "status: ready" in out


def test_second_read_while_converting_does_not_upload(tmp_path, monkeypatch, sleeps):
    f = tmp_path / "big.pdf"
    f.write_bytes(b"x")
    posts, uploaded = [], []

    def handler(req: httpx.Request):
        posts.append(req)
        if req.method == "POST" and b'name="file"' not in req.content:
            if not uploaded:
                return httpx.Response(404, json={"detail": "upload required"})
            return httpx.Response(200, json=CONVERTING)
        uploaded.append(1)
        return httpx.Response(202, json=CONVERTING)

    client = ApiClient("https://api.test", "k", transport=httpx.MockTransport(handler))
    monkeypatch.setattr(server, "_client", lambda: client)
    server.read_document(str(f))
    posts.clear()
    server.read_document(str(f))
    assert len(posts) == 1 and b'name="file"' not in posts[0].content


def test_read_document_resolves_relative_and_unicode_paths(tmp_path, monkeypatch, sleeps):
    (tmp_path / "Q3 räport.pdf").write_bytes(b"x")
    monkeypatch.chdir(tmp_path)
    fake = Fake([READY])
    use(monkeypatch, fake)
    server.read_document("Q3 räport.pdf")
    assert fake.submits == [(tmp_path / "Q3 räport.pdf").resolve()]


def test_missing_relative_path_shows_both_paths(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MDFI_API_KEY", "k")
    out = server.read_document("nope.pdf")
    assert out.startswith("File not found: ")
    assert "(you gave: nope.pdf)" in out and str(tmp_path) in out


def test_uncertainty_is_in_the_header():
    m = manifest(
        pages=[page(1, flags=["table_uncertain"]), page(2, score=60, flags=["ocr", "low_quality"])],
        tables=[
            {
                "id": "p1-t1",
                "start_page": 1,
                "end_page": 1,
                "rows": 4,
                "cols": 3,
                "uncertain": True,
                "notes": ["column count changes on row 3"],
            },
            {
                "id": "p2-t1",
                "start_page": 2,
                "end_page": 2,
                "rows": 2,
                "cols": 2,
                "uncertain": False,
                "notes": ["read by vision — verify"],
            },
        ],
        warnings=["Skipped pages: 9"],
    )
    out = render_document({"id": "d", "status": "ready", "manifest": m}, "x.pdf")
    head = out.split("outline:")[0]
    assert "table p1-t1 on p1: column count changes on row 3" in head
    assert "uncertain tables on page(s) 1" in head
    assert "read by OCR on page(s) 2" in head and "low quality on page(s) 2" in head
    assert "Skipped pages: 9" in head
    assert "read by vision — verify" in head
    assert "- Intro (p1) ⚠" in out and "- Results (p2) ⚠" in out
    assert "uncertainty: none" not in out


def test_failed_document_says_so():
    out = render_document(
        {"id": "d9", "status": "failed", "error": "Too large", "manifest": None}, "a.pdf"
    )
    assert "could not read a.pdf: Too large" in out and "document_id: d9" in out
    assert "Nothing was charged" in out


def test_read_pages_lists_not_ready(monkeypatch):
    fake = Fake(
        [], pages={"id": "d", "pages": [1], "text": "--- page 1 ---\nHi", "not_ready": [2, 3]}
    )
    use(monkeypatch, fake)
    out = server.read_pages("1b4e28ba-2fa1-41d2-883f-0016d3cca427", pages="1-3")
    assert out.startswith("--- page 1 ---\nHi")
    assert out.endswith("still converting — ask again shortly: 2–3")
    assert fake.args == ("1b4e28ba-2fa1-41d2-883f-0016d3cca427", "1-3", None)
    assert render_pages({"text": "", "not_ready": [4]}) == "still converting — ask again shortly: 4"


def test_read_pages_requires_exactly_one_selector(monkeypatch):
    use(monkeypatch, Fake([]))
    assert "exactly one" in server.read_pages("1b4e28ba-2fa1-41d2-883f-0016d3cca427")
    assert "exactly one" in server.read_pages(
        "1b4e28ba-2fa1-41d2-883f-0016d3cca427", pages="1", section="Intro"
    )


def test_errors_returned_as_text(tmp_path, monkeypatch, sleeps):
    def boom(req):
        return httpx.Response(402, json={"detail": "Subscription required. Choose a plan at x"})

    client = ApiClient("https://api.test", "k", transport=httpx.MockTransport(boom))
    monkeypatch.setattr(server, "_client", lambda: client)
    f = tmp_path / "a.pdf"
    f.write_bytes(b"x")
    assert server.read_document(str(f)) == "Subscription required. Choose a plan at x"
    assert (
        server.read_pages("1b4e28ba-2fa1-41d2-883f-0016d3cca427", pages="1")
        == "Subscription required. Choose a plan at x"
    )
    monkeypatch.undo()
    monkeypatch.delenv("MDFI_API_KEY", raising=False)
    assert server.read_pages("1b4e28ba-2fa1-41d2-883f-0016d3cca427", pages="1").startswith(
        "Set MDFI_API_KEY"
    )


def test_tools_registered_with_description():
    import asyncio

    tools = asyncio.run(server.mcp.list_tools())
    names = {t.name: t for t in tools}
    assert set(names) == {"read_document", "read_pages", "export_table"}
    assert names["read_document"].description.startswith(
        "Read a local PDF, Word, PowerPoint, Excel, HTML, email or image file as clean text."
    )


# ---- read guard: hidden files, unsupported types, paths outside the project ----


def test_refuses_hidden_files_and_folders(tmp_path, monkeypatch):
    monkeypatch.setenv("MDFI_API_KEY", "k")
    ssh = tmp_path / ".ssh"
    ssh.mkdir()
    (ssh / "id_rsa.pdf").write_bytes(b"x")
    (tmp_path / ".env").write_text("A=1")
    (tmp_path / "note.txt.bak").write_text("x")
    for p in (ssh / "id_rsa.pdf", tmp_path / ".env"):
        assert (
            server.read_document(str(p)) == f"Refusing to read hidden file or folder: {p.resolve()}"
        )


def test_refuses_symlink_to_hidden_target(tmp_path, monkeypatch):
    monkeypatch.setenv("MDFI_API_KEY", "k")
    (tmp_path / ".secret").mkdir()
    key = tmp_path / ".secret" / "key.pem"
    key.write_text("k")
    link = tmp_path / "doc.pdf"
    link.symlink_to(key)
    out = server.read_document(str(link))
    assert out.startswith("Refusing to read hidden file or folder: ")


def test_refuses_unsupported_extension(tmp_path, monkeypatch):
    monkeypatch.setenv("MDFI_API_KEY", "k")
    f = tmp_path / "key.pem"
    f.write_text("k")
    assert server.read_document(str(f)) == (
        "Unsupported file type: .pem — mdfi reads PDF, Word, PowerPoint, Excel, HTML, "
        "email, images and text files"
    )


def test_allows_file_outside_cwd_and_uppercase_extension(tmp_path, monkeypatch, sleeps):
    other = tmp_path / "elsewhere"
    other.mkdir()
    f = other / "A.PDF"
    f.write_bytes(b"x")
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    fake = Fake([READY])
    use(monkeypatch, fake)
    server.read_document(str(f))
    assert fake.submits == [f.resolve()]


def _pages_fake(monkeypatch, doc, text="--- page 1 ---\nHi\n--- page 2 ---\nYo"):
    fake = Fake([doc], pages={"id": "d", "pages": [1, 2], "text": text, "not_ready": []})
    fake.document = lambda doc_id: (
        doc if not isinstance(doc, Exception) else (_ for _ in ()).throw(doc)
    )
    use(monkeypatch, fake)


def test_read_pages_adds_quality_notes(monkeypatch):
    m = manifest(
        pages=[page(1, score=60, flags=["ocr", "table_uncertain"]), page(2)],
        tables=[
            {
                "id": "p1-t1",
                "start_page": 1,
                "end_page": 1,
                "rows": 2,
                "cols": 2,
                "uncertain": True,
                "notes": ["row 3 has 4 cells, header has 3"],
            },
            {
                "id": "p2-t1",
                "start_page": 2,
                "end_page": 2,
                "rows": 2,
                "cols": 2,
                "uncertain": False,
                "notes": ["read by vision — verify"],
            },
        ],
    )
    _pages_fake(monkeypatch, {"id": "d", "status": "ready", "manifest": m})
    out = server.read_pages("1b4e28ba-2fa1-41d2-883f-0016d3cca427", pages="1-2")
    assert out.splitlines()[0] == "--- page 1 ---"
    assert "read by OCR" in out.splitlines()[1] and "uncertain tables" in out.splitlines()[1]
    assert "table p1-t1: row 3 has 4 cells, header has 3" in out.splitlines()[1]
    assert "table p2-t1: read by vision — verify" in out
    assert "Hi" in out and "Yo" in out


def test_read_pages_clean_page_has_no_note(monkeypatch):
    _pages_fake(monkeypatch, READY)
    out = server.read_pages("1b4e28ba-2fa1-41d2-883f-0016d3cca427", pages="1-2")
    assert out == "--- page 1 ---\nHi\n--- page 2 ---\nYo"


def test_read_pages_survives_document_failure_and_says_flags_are_missing(monkeypatch):
    from mdfi_connect.client import ConnectorError

    _pages_fake(monkeypatch, ConnectorError("boom"))
    out = server.read_pages("1b4e28ba-2fa1-41d2-883f-0016d3cca427", pages="1-2")
    assert out.splitlines()[0] == (
        "⚠ page quality flags could not be loaded (boom) — call read_pages again "
        "before relying on these pages"
    )
    assert out.endswith("--- page 1 ---\nHi\n--- page 2 ---\nYo")


def test_read_pages_409_returned_as_text(monkeypatch):
    client = ApiClient(
        "https://api.test",
        "k",
        transport=httpx.MockTransport(
            lambda r: httpx.Response(409, json={"detail": "Document is converting"})
        ),
    )
    monkeypatch.setattr(server, "_client", lambda: client)
    assert (
        server.read_pages("1b4e28ba-2fa1-41d2-883f-0016d3cca427", pages="1")
        == "Document is converting"
    )


def test_partial_ready_document_header_says_what_is_shown():
    m = manifest(page_count=200, first_text_pages=[1, 2, 3, 4, 5], complete=False)
    out = render_document({"id": "d", "status": "ready", "manifest": m}, "x.pdf")
    assert "shown below: pages 1–5 of 200 (use read_pages for the rest)" in out.split("outline:")[0]
    assert "[Shown: pages" in out
    whole = render_document(READY, "a.docx")
    assert "shown below" not in whole


def test_pyproject_pins_mcp_range():
    import pathlib

    text = (pathlib.Path(__file__).parent.parent / "pyproject.toml").read_text()
    assert '"mcp>=1.2,<3"' in text


def test_docx_style_early_stop_when_outline_exists_but_no_page_ready(tmp_path, monkeypatch, sleeps):
    f = tmp_path / "a.docx"
    f.write_bytes(b"x")
    outline_only = {
        "id": "d4",
        "status": "converting",
        "manifest": manifest(pages=[page(1, "converting"), page(2, "converting")]),
    }
    use(monkeypatch, Fake([outline_only]))
    out = server.read_document(str(f))
    assert sleeps == []
    assert "STILL CONVERTING" in out and "- Intro (p1) (converting)" in out


def test_failed_mid_poll_stops_and_renders_failure(tmp_path, monkeypatch, sleeps):
    f = tmp_path / "a.pdf"
    f.write_bytes(b"x")
    waiting = {"id": "d5", "status": "converting", "manifest": None}
    failed = {"id": "d5", "status": "failed", "error": "No content extracted", "manifest": None}
    use(monkeypatch, Fake([waiting, failed]))
    out = server.read_document(str(f))
    assert sleeps == [1.0]
    assert "could not read" in out and "No content extracted" in out


def test_flag_labels():
    m = manifest(
        pages=[
            page(1, flags=["text_missing", "no_text_recovered", "mystery"]),
            page(2, flags=["degenerate", "ocr_noise", "table_issues", "vision_unavailable"]),
        ]
    )
    head = render_document({"id": "d", "status": "ready", "manifest": m}, "x").split("outline:")[0]
    for label in (
        "text layer present but little or nothing extracted",
        "no text recovered from scan",
        "garbled output",
        "OCR noise",
        "table problems",
        "vision reader unavailable",
    ):
        assert label in head
    assert "mystery" not in head.replace("flag mystery", "")  # unknown flags shown by name
    assert "mystery" in head


def test_converting_hint_lists_ready_pages_and_charge_lines():
    out = render_document(CONVERTING, "big.pdf")
    assert (
        "Page 1 is ready; call read_pages for it now, or read_document again in ~30 s for the rest."
        in out
    )
    assert "charged: nothing yet — charged once when conversion completes" in out
    ready = render_document(READY, "a.docx")
    assert "charged: 2 weighted pages (once; re-reads are free)" in ready


def test_http_loggers_are_quiet():
    import logging

    assert logging.getLogger("httpx").level >= logging.WARNING
    assert logging.getLogger("httpcore").level >= logging.WARNING


def test_converting_hint_gives_the_server_estimate():
    doc = {**CONVERTING, "manifest": {**CONVERTING["manifest"], "estimated_seconds_left": 300}}
    out = render_document(doc, "big.pdf")
    assert (
        "the rest should be ready in about 5 minutes (estimate); call read_document again then."
        in out
    )
    assert "~30 s" not in out


def test_converting_hint_without_estimate_keeps_the_short_retry():
    doc = {**CONVERTING, "manifest": {**CONVERTING["manifest"], "estimated_seconds_left": None}}
    assert "~30 s" in render_document(doc, "big.pdf")


def test_not_ready_pages_show_the_estimate():
    m = manifest(pages=[page(1), page(2, state="converting")], estimated_seconds_left=45)
    out = render_pages({"text": "--- page 1 ---\nx", "not_ready": [2]}, m)
    assert out.endswith("still converting (under a minute left): 2")
