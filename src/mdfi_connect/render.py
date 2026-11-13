import re

FLAG_LABELS = {
    "table_uncertain": "uncertain tables",
    "low_quality": "low quality",
    "ocr": "read by OCR",
    "not_enhanced_cap": "could not be enhanced (escalation cap reached)",
    "text_missing": "text layer present but little or nothing extracted",
    "glued_words": "words run together (spacing lost)",
    "not_converted": "page could not be converted",
    "table_missed": "a captioned table was read as text (no table id, so it cannot be exported)",
    "no_text_recovered": "no text recovered from scan",
    "degenerate": "garbled output",
    "ocr_noise": "OCR noise",
    "table_issues": "table problems",
    "vision_unavailable": "vision reader unavailable",
}
NEUTRAL_FLAGS = {"empty", "enhanced_tier2", "enhanced_tier3"}
# A page read by OCR that still scores this well is a clean scan, not a concern.
SCAN_ACCEPT_SCORE = 85

# Mirrors mdfi.tables.is_problem_note: these table notes inform, they do not warn.
INFO_TABLE_NOTES = {"read by vision — verify", "two-level header combined"}
INFO_TABLE_PREFIXES = ("continues on page", "may continue on page ")


def is_problem_note(note: str) -> bool:
    return note not in INFO_TABLE_NOTES and not note.startswith(INFO_TABLE_PREFIXES)


def _concerns(page: dict) -> list[str]:
    clean_scan = (page.get("score") or 0) >= SCAN_ACCEPT_SCORE
    return [
        f
        for f in page.get("flags", [])
        if f not in NEUTRAL_FLAGS and not (f == "ocr" and clean_scan)
    ]


def _clean_scans(manifest: dict) -> list[int]:
    return [
        p["number"]
        for p in manifest.get("pages", [])
        if "ocr" in p.get("flags", []) and "ocr" not in _concerns(p)
    ]


def _problem(table: dict) -> str:
    problems = [n for n in table.get("notes") or [] if is_problem_note(n)]
    if not problems:
        return "possible damage"
    more = f" (+{len(problems) - 1} more)" if len(problems) > 1 else ""
    return problems[0] + more


def _info_notes(table: dict) -> list[str]:
    return [n for n in table.get("notes") or [] if not is_problem_note(n)]


def _label(flag: str) -> str:
    return FLAG_LABELS.get(flag, f"flag {flag}")


def _ranges(numbers: list[int]) -> str:
    out, run = [], []
    for n in sorted(numbers):
        if run and n == run[-1] + 1:
            run.append(n)
        else:
            if run:
                out.append(run)
            run = [n]
    if run:
        out.append(run)
    return ", ".join(str(r[0]) if len(r) == 1 else f"{r[0]}–{r[-1]}" for r in out)


def _pages_text(numbers: list[int]) -> str:
    return _ranges(numbers)


def _span(start: int, end: int) -> str:
    return f"p{start}" if start == end else f"p{start}-{end}"


def _uncertainty(manifest: dict) -> list[str]:
    lines = []
    for t in manifest.get("tables", []):
        if t.get("uncertain"):
            span = _span(t["start_page"], t["end_page"])
            lines.append(f"table {t['id']} on {span}: {_problem(t)}")
    by_flag: dict[str, list[int]] = {}
    for p in manifest.get("pages", []):
        for flag in _concerns(p):
            by_flag.setdefault(flag, []).append(p["number"])
    for flag, nums in by_flag.items():
        lines.append(f"{_label(flag)} on page(s) {_pages_text(nums)}")
    lines.extend(manifest.get("warnings", []))
    return lines


def _outline(manifest: dict) -> list[str]:
    pages = {p["number"]: p for p in manifest.get("pages", [])}
    uncertain_tables = [t for t in manifest.get("tables", []) if t.get("uncertain")]
    out = []
    for e in manifest.get("outline", []):
        rng = range(e["start_page"], e["end_page"] + 1)
        converting = any(pages.get(n, {}).get("state") == "converting" for n in rng)
        warn = any(_concerns(pages.get(n, {})) for n in rng) or any(
            t["start_page"] <= e["end_page"] and t["end_page"] >= e["start_page"]
            for t in uncertain_tables
        )
        indent = "  " * max(e.get("level", 1) - 1, 0)
        marks = (" ⚠" if warn else "") + (" (converting)" if converting else "")
        out.append(f"{indent}- {e['title']} ({_span(e['start_page'], e['end_page'])}){marks}")
    return out


def _quality(manifest: dict) -> str:
    pages = manifest.get("pages", [])
    scored = [p["score"] for p in pages if p.get("score") is not None]
    if not scored:
        return "not scored yet"
    avg = sum(scored) / len(scored)
    flagged = sum(1 for p in pages if _concerns(p))
    text = f"average page score {avg:.0f}/100 over {len(scored)} scored page(s)"
    return text + (f"; {flagged} page(s) need care" if flagged else "; no pages flagged")


def _time_left(seconds) -> str | None:
    # Mirrors the server's format_time_left; the connector imports nothing from mdfi.
    if not isinstance(seconds, int):
        return None
    if seconds < 60:
        return "under a minute"
    minutes = round(seconds / 60)
    return f"about {minutes} minute" if minutes == 1 else f"about {minutes} minutes"


def render_document(doc: dict, source_name: str) -> str:
    doc_id = doc.get("id", "")
    status = doc.get("status")
    name = doc.get("source") or source_name
    if status == "failed":
        return (
            f"mdfi could not read {name}: {doc.get('error') or 'conversion failed'}\n"
            f"document_id: {doc_id}\nstatus: failed\n"
            "Nothing was charged. Do not rely on this file's content."
        )
    manifest = doc.get("manifest")
    if not manifest:
        return (
            f"mdfi is preparing {name}\ndocument_id: {doc_id}\nstatus: {status}\n"
            "Nothing is readable yet — call read_document again with the same path shortly "
            "(no new upload, no extra charge)."
        )
    total = manifest["page_count"]
    ready_n = sum(1 for p in manifest.get("pages", []) if p.get("state") == "ready")
    ready = status == "ready"
    if ready:
        ready_n = total
    lines = [
        f"mdfi document: {name}",
        f"document_id: {doc_id}",
        f"pages: {total} (ready {ready_n} of {total})",
        f"status: {status}",
        f"quality: {_quality(manifest)}",
    ]
    if ready:
        lines.append(
            f"charged: {manifest['weighted_pages']} weighted pages (once; re-reads are free)"
        )
    else:
        lines.append("charged: nothing yet — charged once when conversion completes")
    shown = manifest.get("first_text_pages", [])
    ready_set = {p["number"] for p in manifest.get("pages", []) if p.get("state") == "ready"}
    partial = (ready and not manifest.get("complete")) or set(shown) < ready_set
    if shown and partial:
        lines.append(
            f"shown below: pages {_ranges(shown)} of {total} (use read_pages for the rest)"
        )
    unc = _uncertainty(manifest)
    if unc:
        lines.append("⚠ uncertain — verify before relying on these:")
        lines.extend(f"  - {u}" for u in unc)
    elif ready:
        lines.append("uncertainty: none flagged")
    scans = _clean_scans(manifest)
    if scans:
        lines.append(f"read from scans: pages {_ranges(scans)}")
    notes = [f"{t['id']} ({n})" for t in manifest.get("tables", []) for n in _info_notes(t)]
    if notes:
        lines.append("table notes: " + "; ".join(notes))
    outline = _outline(manifest)
    if outline:
        lines.append("outline:")
        lines.extend(outline)
    first = manifest.get("first_text") or ""
    if first:
        lines.append("")
        lines.append(first.rstrip())
        if ready and not manifest.get("complete") and shown:
            lines.append("")
            lines.append(
                f"[Shown: pages {_pages_text(shown)} of {total}. Use read_pages with "
                'pages="…" or section="<heading>" for the rest.]'
            )
    if not ready:
        lines.append("")
        ready_pages = [p["number"] for p in manifest.get("pages", []) if p.get("state") == "ready"]
        left = _time_left(manifest.get("estimated_seconds_left"))
        eta = f"should be ready in {left} (estimate)" if left else None
        if ready_pages:
            verb = "is" if len(ready_pages) == 1 else "are"
            noun = "Page" if len(ready_pages) == 1 else "Pages"
            them = "it" if len(ready_pages) == 1 else "them"
            rest = (
                f"the rest {eta}; call read_document again then."
                if eta
                else "or read_document again in ~30 s for the rest."
            )
            hint = f"{noun} {_ranges(ready_pages)} {verb} ready; call read_pages for {them} now, {rest}"
        else:
            wait = (
                f"they {eta}; call read_document again then"
                if eta
                else "call read_document again in ~30 s"
            )
            hint = f"No pages are readable yet; {wait} (same path: no re-upload, no extra charge)."
        lines.append("STILL CONVERTING — this is partial. " + hint)
    return "\n".join(lines)


def _page_notes(manifest: dict) -> tuple[dict[int, str], dict[int, str]]:
    """Per page: concerns (shown with ⚠) and informational table notes (shown plain)."""
    warn: dict[int, list[str]] = {}
    info: dict[int, list[str]] = {}
    for p in manifest.get("pages", []):
        for flag in _concerns(p):
            warn.setdefault(p["number"], []).append(_label(flag))
    for t in manifest.get("tables", []):
        pages = range(t["start_page"], t["end_page"] + 1)
        if t.get("uncertain"):
            for n in pages:
                warn.setdefault(n, []).append(f"table {t['id']}: {_problem(t)}")
        if _info_notes(t):
            for n in pages:
                info.setdefault(n, []).append(f"table {t['id']}: {'; '.join(_info_notes(t))}")
    return _joined(warn), _joined(info)


def _joined(notes: dict[int, list[str]]) -> dict[int, str]:
    return {n: "; ".join(v) for n, v in notes.items()}


def render_pages(resp: dict, manifest: dict | None = None) -> str:
    text = (resp.get("text") or "").rstrip()
    if manifest:
        warn, info = _page_notes(manifest)
        out = []
        for line in text.splitlines():
            out.append(line)
            m = re.fullmatch(r"--- page (\d+) ---", line)
            if m and int(m.group(1)) in warn:
                out.append(f"⚠ {warn[int(m.group(1))]}")
            if m and int(m.group(1)) in info:
                out.append(f"note: {info[int(m.group(1))]}")
        text = "\n".join(out)
    not_ready = resp.get("not_ready") or []
    if not_ready:
        left = _time_left((manifest or {}).get("estimated_seconds_left"))
        status = (
            f"still converting ({left} left)" if left else "still converting — ask again shortly"
        )
        tail = f"{status}: {_pages_text(not_ready)}"
        return f"{text}\n\n{tail}" if text else tail
    return text
