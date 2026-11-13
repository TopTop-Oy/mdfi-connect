import json

import httpx
import pytest

from mdfi_connect.client import ApiClient, ConnectorError, client_from_env

BASE = "https://api.test"


def make(handler):
    return ApiClient(BASE, "mdfi_key", transport=httpx.MockTransport(handler))


def test_submit_hash_only_then_upload_on_upload_required(tmp_path):
    f = tmp_path / "a.txt"
    f.write_bytes(b"hello")
    calls = []

    def handler(req: httpx.Request):
        calls.append(req)
        assert req.headers["authorization"] == "Bearer mdfi_key"
        if b'name="file"' not in req.content:
            return httpx.Response(404, json={"detail": "upload required"})
        return httpx.Response(202, json={"id": "d1", "status": "converting"})

    out = make(handler).submit(f)
    assert out == {"id": "d1", "status": "converting"}
    assert len(calls) == 2
    assert b"2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824" in calls[0].content
    assert b'name="file"' not in calls[0].content
    assert calls[0].url.path == "/v1/documents"


def test_submit_known_hash_does_not_upload(tmp_path):
    f = tmp_path / "a.txt"
    f.write_bytes(b"hello")
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(200, json={"id": "d1", "status": "ready"})

    assert make(handler).submit(f)["status"] == "ready"
    assert len(calls) == 1


def test_missing_key_message(monkeypatch):
    monkeypatch.delenv("MDFI_API_KEY", raising=False)
    with pytest.raises(ConnectorError) as e:
        client_from_env()
    assert e.value.message == (
        "Set MDFI_API_KEY — get one on the Connect page of your mdfi dashboard."
    )


def test_client_from_env_defaults(monkeypatch):
    monkeypatch.setenv("MDFI_API_KEY", "k")
    monkeypatch.delenv("MDFI_API_URL", raising=False)
    assert client_from_env().base_url == "https://api.mdfi.ai"
    monkeypatch.setenv("MDFI_API_URL", "http://x:1/")
    assert client_from_env().base_url == "http://x:1"


@pytest.mark.parametrize(
    "status,detail,expected",
    [
        (
            402,
            "Subscription required. Choose a plan at x",
            "Subscription required. Choose a plan at x",
        ),
        (
            429,
            "Not enough pages left this month: needed 5, 4 left.",
            "Not enough pages left this month: needed 5, 4 left.",
        ),
        (413, "too big", "too big"),
        (400, "bad hash", "bad hash"),
        (409, "not ready", "not ready"),
        (401, "", "MDFI_API_KEY was rejected — create a new key on the Connect page."),
    ],
)
def test_status_errors_become_messages(status, detail, expected):
    def handler(req):
        return httpx.Response(status, json={"detail": detail} if detail else {})

    with pytest.raises(ConnectorError) as e:
        make(handler).document("d1")
    assert e.value.message == expected


EXPIRED = "Document expired — call read_document again (it will be re-converted and charged)."


def test_404_document_and_pages_say_expired():
    c = make(lambda req: httpx.Response(404, json={"detail": "Document not found or expired"}))
    for call in (lambda: c.document("d"), lambda: c.pages("d", range="1")):
        with pytest.raises(ConnectorError) as e:
            call()
        assert e.value.message == EXPIRED


def test_table_404_passes_server_detail():
    c = make(lambda req: httpx.Response(404, json={"detail": "table p9-t1 not found"}))
    with pytest.raises(ConnectorError) as e:
        c.table("d", "p9-t1")
    assert e.value.message == "table p9-t1 not found"


def test_submit_404_other_than_upload_required_passes_detail(tmp_path):
    f = tmp_path / "a.txt"
    f.write_bytes(b"x")
    c = make(lambda req: httpx.Response(404, json={"detail": "gone"}))
    with pytest.raises(ConnectorError) as e:
        c.submit(f)
    assert e.value.message == "gone"


def test_section_404_is_not_reported_as_expired():
    c = make(lambda req: httpx.Response(404, json={"detail": "Section not found in outline: Foo"}))
    with pytest.raises(ConnectorError) as e:
        c.pages("d", section="Foo")
    assert e.value.message == "Section not found in outline: Foo"


def test_network_and_server_errors_become_messages():
    def boom(req):
        raise httpx.ConnectError("nope")

    with pytest.raises(ConnectorError) as e:
        make(boom).document("d")
    assert e.value.message == (
        f"mdfi API unreachable at {BASE} — check MDFI_API_URL and your network."
    )
    with pytest.raises(ConnectorError) as e:
        make(lambda req: httpx.Response(503, text="<html>Traceback</html>")).document("d")
    assert e.value.message == "mdfi API error (status 503) — try again in a minute."


def test_pages_and_table_requests():
    seen = []

    def handler(req):
        seen.append(req)
        return httpx.Response(200, json={"ok": True})

    c = make(handler)
    c.pages("d", range="3,7")
    c.pages("d", section="Intro")
    c.table("d", "p1-t1")
    assert dict(seen[0].url.params) == {"range": "3,7"}
    assert dict(seen[1].url.params) == {"section": "Intro"}
    assert seen[0].url.path == "/v1/documents/d/pages"
    assert seen[2].url.path == "/v1/documents/d/tables/p1-t1"
    json.dumps(c.document("d"))


def test_non_json_success_body_is_an_error():
    c = make(lambda req: httpx.Response(200, text="<html>captive portal</html>"))
    with pytest.raises(ConnectorError) as e:
        c.document("d")
    assert e.value.message == "mdfi API returned an unexpected response — try again in a minute."


def test_redirect_is_an_error():
    c = make(lambda req: httpx.Response(302, headers={"location": "http://x"}))
    with pytest.raises(ConnectorError):
        c.document("d")


def test_submit_file_errors_become_messages(tmp_path, monkeypatch):
    c = make(lambda req: httpx.Response(200, json={}))
    missing = tmp_path / "nope.pdf"
    with pytest.raises(ConnectorError) as e:
        c.submit(missing)
    assert e.value.message == f"File not found: {missing}"
    with pytest.raises(ConnectorError) as e:
        c.submit(tmp_path)
    assert e.value.message == f"Not a file: {tmp_path}"
    locked = tmp_path / "locked.pdf"
    locked.write_bytes(b"x")
    locked.chmod(0)
    try:
        with pytest.raises(ConnectorError) as e:
            c.submit(locked)
        assert e.value.message == f"Cannot read {locked}: permission denied"
    finally:
        locked.chmod(0o600)


def test_ambiguous_section_409_detail_is_passed_through():
    detail = (
        "Section 'Introduction' matches 2 outline entries; pass one as \"Parent > Title\": "
        "Chapter 1 Filing Information > Introduction (pages 9–10); "
        "Chapter 10 Standard Deduction > Introduction (pages 94–94)"
    )

    def handler(req):
        return httpx.Response(409, json={"detail": detail})

    with pytest.raises(ConnectorError) as e:
        make(handler).pages("d1", section="Introduction")
    assert e.value.message == detail
