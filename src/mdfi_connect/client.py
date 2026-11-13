import hashlib
import os
from pathlib import Path

import httpx

DEFAULT_API_URL = "https://api.mdfi.ai"
EXPIRED_DETAIL = "Document not found or expired"
EXPIRED = "Document expired — call read_document again (it will be re-converted and charged)."


class ConnectorError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def _detail(resp: httpx.Response) -> str:
    try:
        detail = resp.json().get("detail")
    except (ValueError, AttributeError):
        detail = None
    return (
        detail
        if isinstance(detail, str) and detail
        else f"mdfi API error (status {resp.status_code})"
    )


class ApiClient:
    def __init__(
        self, base_url: str, api_key: str, *, transport: httpx.BaseTransport | None = None
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._http = httpx.Client(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {api_key}"},
            transport=transport,
            timeout=30,
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        upload_ok: bool = False,
        timeout: float = 30,
        **kw,
    ) -> httpx.Response:
        try:
            resp = self._http.request(method, path, timeout=timeout, **kw)
        except httpx.TransportError:
            raise ConnectorError(
                f"mdfi API unreachable at {self.base_url} — check MDFI_API_URL and your network."
            ) from None
        status = resp.status_code
        if status < 300:
            return resp
        if status == 401:
            raise ConnectorError(
                "MDFI_API_KEY was rejected — create a new key on the Connect page."
            )
        if status >= 500:
            raise ConnectorError(f"mdfi API error (status {status}) — try again in a minute.")
        if status == 404 and upload_ok and _detail(resp) == "upload required":
            return resp
        if status == 404 and _detail(resp).startswith(EXPIRED_DETAIL):
            raise ConnectorError(EXPIRED)
        raise ConnectorError(_detail(resp))

    def _json(self, resp: httpx.Response) -> dict:
        try:
            return resp.json()
        except ValueError:
            raise ConnectorError(
                "mdfi API returned an unexpected response — try again in a minute."
            ) from None

    def submit(self, path: Path) -> dict:
        digest = hashlib.sha256()
        try:
            with open(path, "rb") as fh:
                for block in iter(lambda: fh.read(1 << 20), b""):
                    digest.update(block)
        except FileNotFoundError:
            raise ConnectorError(f"File not found: {path}") from None
        except IsADirectoryError:
            raise ConnectorError(f"Not a file: {path}") from None
        except PermissionError:
            raise ConnectorError(f"Cannot read {path}: permission denied") from None
        except OSError as exc:
            raise ConnectorError(f"Cannot read {path}: {exc.strerror or 'read error'}") from None
        sha = digest.hexdigest()
        resp = self._request("POST", "/v1/documents", data={"sha256": sha}, upload_ok=True)
        if resp.status_code != 404:
            return self._json(resp)
        with open(path, "rb") as fh:
            up = self._request(
                "POST",
                "/v1/documents",
                data={"sha256": sha},
                files={"file": (Path(path).name, fh)},
                timeout=120,
            )
        return self._json(up)

    def document(self, doc_id: str) -> dict:
        return self._json(self._request("GET", f"/v1/documents/{doc_id}"))

    def pages(self, doc_id: str, *, range: str | None = None, section: str | None = None) -> dict:
        params = {}
        if range is not None:
            params["range"] = range
        if section is not None:
            params["section"] = section
        return self._json(self._request("GET", f"/v1/documents/{doc_id}/pages", params=params))

    def table(self, doc_id: str, table_id: str) -> dict:
        return self._json(self._request("GET", f"/v1/documents/{doc_id}/tables/{table_id}"))


def client_from_env() -> ApiClient:
    key = os.environ.get("MDFI_API_KEY", "").strip()
    if not key:
        raise ConnectorError(
            "Set MDFI_API_KEY — get one on the Connect page of your mdfi dashboard."
        )
    return ApiClient(os.environ.get("MDFI_API_URL") or DEFAULT_API_URL, key)
