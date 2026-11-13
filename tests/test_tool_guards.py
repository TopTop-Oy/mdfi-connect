"""Input validation and the last-resort error guard: a tool never raises into the agent."""

import pytest

from mdfi_connect import server

DOC = "1b4e28ba-2fa1-41d2-883f-0016d3cca427"


class Boom:
    """A client whose 2xx bodies are malformed or whose calls raise unexpectedly."""

    def __init__(self, submit=None, document=None, pages=None, table=None):
        self._submit, self._document, self._pages, self._table = submit, document, pages, table

    def submit(self, path):
        return self._submit

    def document(self, doc_id):
        return self._document

    def pages(self, doc_id, *, range=None, section=None):
        if isinstance(self._pages, Exception):
            raise self._pages
        return self._pages

    def table(self, doc_id, table_id):
        return self._table


def use(monkeypatch, fake):
    monkeypatch.setattr(server, "_client", lambda: fake)


@pytest.mark.parametrize("bad", ["d1", "../../etc", "1b4e28ba", DOC + "/pages", ""])
def test_malformed_document_id_is_refused_before_any_request(monkeypatch, bad):
    use(monkeypatch, None)  # any call on the client would fail
    for out in (server.read_pages(bad, pages="1"), server.export_table(bad, "p1-t1")):
        assert out.startswith(f"Invalid document_id: {bad!r}")
        assert "read_document" in out


@pytest.mark.parametrize("bad", ["t1", "p1t1", "p1-t1/../x", "P1-T1x", ""])
def test_malformed_table_id_is_refused(monkeypatch, bad):
    use(monkeypatch, None)
    out = server.export_table(DOC, bad)
    assert out.startswith(f"Invalid table id: {bad!r}") and "[table p13-t1 — …]" in out


def test_uppercase_document_id_is_accepted(monkeypatch):
    use(monkeypatch, Boom(pages={"text": "--- page 1 ---\nHi", "not_ready": []}, document={}))
    assert server.read_pages(DOC.upper(), pages="1").startswith("--- page 1 ---")


def test_malformed_document_body_returns_a_report_message(tmp_path, monkeypatch):
    f = tmp_path / "a.pdf"
    f.write_bytes(b"x")
    use(monkeypatch, Boom(submit={"id": DOC, "status": "ready", "manifest": {"pages": []}}))
    out = server.read_document(str(f))
    assert out == "mdfi-connect error: KeyError — please report this"


def test_unexpected_exception_in_read_pages_returns_a_report_message(monkeypatch):
    use(monkeypatch, Boom(pages=RuntimeError("boom")))
    assert server.read_pages(DOC, pages="1") == (
        "mdfi-connect error: RuntimeError — please report this"
    )


def test_malformed_table_body_returns_a_report_message(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    use(monkeypatch, Boom(table={"id": "p1-t1"}, document={"source": "r.pdf"}))
    out = server.export_table(DOC, "p1-t1")
    assert out == "mdfi-connect error: KeyError — please report this"
