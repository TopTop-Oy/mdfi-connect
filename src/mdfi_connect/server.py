import logging
import os
import re
import time
from pathlib import Path

try:
    from mcp.server.fastmcp import FastMCP
except ImportError:  # mcp 2.x renamed FastMCP
    from mcp.server.mcpserver import MCPServer as FastMCP

from mdfi_connect.client import ApiClient, ConnectorError, client_from_env
from mdfi_connect.csv_export import all_notes, write_table
from mdfi_connect.render import is_problem_note, render_document, render_pages

POLL_BUDGET = 20.0
POLL_STEPS = (1.0, 2.0)
_sleep = time.sleep

for _name in ("httpx", "httpcore"):
    logging.getLogger(_name).setLevel(logging.WARNING)

ALLOWED_EXTENSIONS = {
    ".pdf", ".docx", ".pptx", ".xlsx", ".html", ".htm", ".eml", ".png", ".jpg", ".jpeg",
    ".tif", ".tiff", ".bmp", ".gif", ".webp", ".txt", ".md",
}  # fmt: skip

# The server's document ids are UUIDs; table ids are p<page>-t<n>.
_DOCUMENT_ID = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE
)
_TABLE_ID = re.compile(r"p\d+-t\d+")

mcp = FastMCP("mdfi")
log = logging.getLogger("mdfi_connect")


def _unexpected(exc: Exception) -> str:
    # A malformed 2xx body or a bug: never raise into the agent's session. Only the type
    # is logged: an exception's message could carry document content.
    log.error("mdfi-connect tool failed: %s", type(exc).__name__)
    return f"mdfi-connect error: {type(exc).__name__} — please report this"


def _bad_document_id(document_id: str) -> str | None:
    if _DOCUMENT_ID.fullmatch(document_id or ""):
        return None
    return (
        f"Invalid document_id: {document_id!r} — pass the document_id that read_document "
        "printed (a UUID such as 1b4e28ba-2fa1-41d2-883f-0016d3cca427)."
    )


def _client() -> ApiClient:
    return client_from_env()


_FILE_ERRORS = ("File not found:", "Not a file:", "Cannot read ")


def _worth_returning(doc: dict) -> bool:
    if doc.get("status") in ("ready", "failed"):
        return True
    manifest = doc.get("manifest") or {}
    if any(p.get("state") == "ready" for p in manifest.get("pages", [])):
        return True
    return bool(manifest.get("outline"))


def _check_path(path: str) -> tuple[Path | None, str | None]:
    cwd = Path(os.getcwd()).resolve()
    given = Path(path).expanduser()
    resolved = (cwd / given).resolve()
    try:
        parts = resolved.relative_to(cwd).parts
    except ValueError:
        parts = resolved.parts
    if any(part.startswith(".") for part in parts):
        return None, f"Refusing to read hidden file or folder: {resolved}"
    if resolved.suffix.lower() not in ALLOWED_EXTENSIONS:
        return None, (
            f"Unsupported file type: {resolved.suffix or '(none)'} — mdfi reads PDF, Word, "
            "PowerPoint, Excel, HTML, email, images and text files"
        )
    return resolved, None


@mcp.tool(
    description=(
        "Read a local PDF, Word, PowerPoint, Excel, HTML, email or image file as clean text. "
        "Handles scans and complex tables, and marks anything uncertain. "
        "Prefer this over reading such files directly."
    )
)
def read_document(path: str) -> str:
    try:
        resolved, refusal = _check_path(path)
        if refusal:
            return refusal
        client = _client()
        try:
            doc = client.submit(resolved)
        except ConnectorError as exc:
            if str(resolved) != path and exc.message.startswith(_FILE_ERRORS):
                return f"{exc.message} (you gave: {path})"
            raise
        waited = 0.0
        steps = iter(POLL_STEPS)
        while not _worth_returning(doc) and waited < POLL_BUDGET:
            step = next(steps, POLL_STEPS[-1])
            step = min(step, POLL_BUDGET - waited)
            _sleep(step)
            waited += step
            doc = client.document(doc["id"])
        return render_document(doc, resolved.name)
    except ConnectorError as exc:
        return exc.message
    except Exception as exc:  # noqa: BLE001 — last resort: a tool never raises into the agent
        return _unexpected(exc)


@mcp.tool(
    description=(
        "Read specific pages of a document already opened with read_document. Pass pages "
        '(e.g. "3,7,9-11") or section (a heading from the outline), not both. Free: '
        "never charges."
    )
)
def read_pages(document_id: str, pages: str | None = None, section: str | None = None) -> str:
    if refusal := _bad_document_id(document_id):
        return refusal
    if (pages is None) == (section is None):
        return (
            'Pass exactly one of pages (e.g. "3,7,9-11") or section (a heading from the outline).'
        )
    try:
        client = _client()
        resp = client.pages(document_id, range=pages, section=section)
        try:
            manifest = client.document(document_id).get("manifest")
        except ConnectorError as exc:
            # The text is still worth returning, but without the manifest no ⚠ lines can
            # be added: say so rather than presenting the pages as clean.
            return (
                f"⚠ page quality flags could not be loaded ({exc.message}) — call read_pages "
                f"again before relying on these pages\n\n{render_pages(resp, None)}"
            )
        return render_pages(resp, manifest)
    except ConnectorError as exc:
        return exc.message
    except Exception as exc:  # noqa: BLE001 — last resort: a tool never raises into the agent
        return _unexpected(exc)


@mcp.tool(
    description=(
        "Write one table from a document already opened with read_document to a CSV file in "
        "the working directory (default ./mdfi-tables/<file-stem>-<table-id>.csv, UTF-8 with a "
        "header row). The table id comes from the table's label in the page text, e.g. "
        "[table p13-t1 — …]. A table with notes gets a .notes.txt file beside the CSV; never "
        "overwrites unless overwrite=true. Only once the document is ready. Free: never charges."
    )
)
def export_table(
    document_id: str, table: str, path: str | None = None, overwrite: bool = False
) -> str:
    if refusal := _bad_document_id(document_id):
        return refusal
    if not _TABLE_ID.fullmatch(table or ""):
        return (
            f"Invalid table id: {table!r} — use the id from the table's label in the page "
            "text, e.g. p13-t1 from [table p13-t1 — …]."
        )
    try:
        return _export_table(document_id, table, path, overwrite)
    except ConnectorError as exc:
        return exc.message
    except Exception as exc:  # noqa: BLE001 — last resort: a tool never raises into the agent
        return _unexpected(exc)


def _export_table(document_id: str, table: str, path: str | None, overwrite: bool) -> str:
    client = _client()
    data = client.table(document_id, table)
    source = (client.document(document_id).get("source") or "").strip()
    stem = Path(source).stem if source else ""
    csv_path, notes_path = write_table(
        data,
        source_stem=stem or "document",
        cwd=Path(os.getcwd()),
        path=path,
        overwrite=overwrite,
    )
    notes = all_notes(data)
    # Server problem notes lead; informational ones (vision, combined header, page
    # continuation, the CSV's own prefixed/ragged-row notes) follow as plain notes.
    server_notes = notes[: len(data.get("notes") or [])]  # all_notes keeps their order
    problems = [n for n in server_notes if is_problem_note(n)]
    info = [n for n in notes if n not in problems]
    nrows, ncols = len(data["rows"]), len(data["headers"])
    shape = f"{nrows} row{'' if nrows == 1 else 's'} × {ncols} column{'' if ncols == 1 else 's'}"
    lines = []
    if data.get("uncertain") or problems:
        lines.append(
            f"Uncertain table — check it against the source before relying on it: "
            f"{'; '.join(problems)}"
            if problems
            else "Uncertain table — check it against the source before relying on it."
        )
    lines.append(f"Wrote {csv_path} ({shape}).")
    if info:
        lines.append(f"Notes: {'; '.join(info)}")
    if notes_path:
        lines.append(f"Notes saved to {notes_path}.")
    return "\n".join(lines)


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
