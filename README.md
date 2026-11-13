# mdfi-connect

<!-- mcp-name: io.github.TopTop-Oy/mdfi-connect -->

MCP stdio server that lets coding agents (Claude Code, Cursor) read documents through the mdfi cloud API: PDFs and scans, Word, PowerPoint, Excel, HTML, email, images and text. Tables come back structured, anything uncertain is marked, and tables can be exported to CSV.

## Install

Claude Code:

    claude mcp add mdfi --env MDFI_API_KEY=mdfi_... --env MDFI_API_URL=https://api.mdfi.ai -- uvx mdfi-connect

Cursor (`~/.cursor/mcp.json`):

    {
      "mcpServers": {
        "mdfi": {
          "command": "uvx",
          "args": ["mdfi-connect"],
          "env": { "MDFI_API_KEY": "mdfi_...", "MDFI_API_URL": "https://api.mdfi.ai" }
        }
      }
    }

Claude Code plugin (asks for your key when you install it and keeps it as a sensitive setting; runs the plugin's own locked project with `uv run --locked`):

    /plugin marketplace add TopTop-Oy/mdfi-connect
    /plugin install mdfi@mdfi

Cursor plugin (once listed in the Cursor Marketplace): install **mdfi** and set `MDFI_API_KEY` under Plugins → Configure.

The Connect page of your mdfi dashboard (https://app.mdfi.ai) shows these snippets with your key filled in. Plans and pricing: https://mdfi.ai/docs/pricing/

## Configuration

- `MDFI_API_KEY` (required): create one on the Connect page of your mdfi dashboard.
- `MDFI_API_URL` (optional): defaults to `https://api.mdfi.ai`.

Nothing else is configurable.

## Tools

- `read_document(path)`: uploads the file (only if mdfi has not seen it before) and returns the outline (before conversion only for PDFs with bookmarks and PowerPoint decks with slide titles; for other files once their pages are converted), the text of the ready pages and a header with the document id, quality and uncertainty. It waits up to 20 seconds; a large document keeps converting, so call it again with the same path (no re-upload, no extra charge).
- `read_pages(document_id, pages | section)`: specific pages (`"3,7,9-11"`) or the pages under one outline heading. Free. If the page quality flags cannot be loaded, the text still comes back with a ⚠ line saying so.
- `export_table(document_id, table, path, overwrite)`: writes one table to CSV. The table id comes from the table's label in the page text, e.g. `p13-t1` from `[table p13-t1 — 42 rows × 6 cols]`. Default `./mdfi-tables/<file-stem>-<table-id>.csv`. Free. Only once the document is ready (while it converts, a page still being enhanced can extend a table).

Only real concerns get a ⚠. Clean scans are listed once (`read from scans: pages 1–40`), and informational table notes (read by vision, combined header, continues on a page) are shown plain. The header does not show the pages left this month (the API does not expose it). `document_id` must be the id `read_document` printed.

A document is charged once, when conversion finishes. `read_pages` and `export_table` never charge.

## Guards

- Reads: only supported file types (pdf, docx, pptx, xlsx, html, htm, eml, png, jpg, jpeg, tif, tiff, bmp, gif, webp, txt, md). Symbolic links are resolved first. Hidden files and folders are refused.
- CSV export: `.csv` paths only, inside the working directory. Existing files are never overwritten unless `overwrite=true`; symbolic links are refused. UTF-8 with a byte-order mark, header row, standard quoting.
- A table with notes (problems or informational) gets a `<name>.csv.notes.txt` beside the CSV. Both files are written to temporary files and renamed into place once complete (the notes first), so a failed write never leaves a partial CSV or a notes file without its CSV; an overwritten file keeps its permissions. Non-numeric cells starting with `=`, `+`, `-` or `@` are written with a leading `'` so spreadsheets do not run them as formulas; the notes file states how many.

## Development

    uv run pytest -q
    uv run ruff check src tests

Dependencies: `mcp>=1.2,<3` and `httpx` only. Licence: Apache-2.0. This is the client only: it holds no conversion code and calls the hosted mdfi API.
