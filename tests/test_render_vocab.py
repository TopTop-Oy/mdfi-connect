"""Flag and note vocabulary: only real concerns get ⚠, so the agent learns to trust it."""

from test_tools import manifest, page

from mdfi_connect.render import _time_left, is_problem_note, render_document, render_pages


def _table(tid, start, end, uncertain, notes):
    return {
        "id": tid,
        "start_page": start,
        "end_page": end,
        "rows": 3,
        "cols": 2,
        "uncertain": uncertain,
        "notes": notes,
    }


def _ready(m):
    return render_document({"id": "d", "status": "ready", "manifest": m}, "scan.pdf")


def test_clean_40_page_scan_has_no_warning_and_one_scan_line():
    m = manifest(
        page_count=40,
        weighted_pages=40,
        pages=[page(n, flags=["ocr"]) for n in range(1, 41)],
        outline=[{"title": "Report", "level": 1, "start_page": 1, "end_page": 40}],
    )
    out = _ready(m)
    assert "⚠" not in out
    assert out.splitlines().count("read from scans: pages 1–40") == 1
    assert "read by OCR" not in out
    assert "uncertainty: none flagged" in out
    assert "no pages flagged" in out


def test_low_scoring_scan_page_is_still_a_concern():
    m = manifest(pages=[page(1, flags=["ocr"]), page(2, score=60, flags=["ocr", "low_quality"])])
    head = _ready(m).split("outline:")[0]
    assert "read by OCR on page(s) 2" in head and "low quality on page(s) 2" in head
    assert "read from scans: pages 1" in head
    assert "- Results (p2) ⚠" in _ready(m) and "- Intro (p1) ⚠" not in _ready(m)


def test_enhanced_and_empty_flags_are_neutral():
    m = manifest(
        pages=[page(1, flags=["enhanced_tier2"]), page(2, flags=["enhanced_tier3", "empty"])]
    )
    out = _ready(m)
    assert "⚠" not in out and "flag enhanced" not in out and "uncertainty: none flagged" in out


def test_page_lists_are_ranges():
    m = manifest(
        page_count=6,
        pages=[page(n, flags=["low_quality"] if n != 4 else []) for n in range(1, 7)],
    )
    assert "low quality on page(s) 1–3, 5–6" in _ready(m)
    assert render_pages({"text": "", "not_ready": [2, 3, 4, 7]}) == (
        "still converting — ask again shortly: 2–4, 7"
    )


def test_uncertain_table_page_gets_a_warning_with_its_problem_note():
    m = manifest(
        pages=[page(1, flags=["table_uncertain"]), page(2)],
        tables=[
            _table("p1-t1", 1, 2, True, ["continues on page 2", "row 3: 4 cells, header has 3"])
        ],
    )
    out = _ready(m)
    assert "table p1-t1 on p1-2: row 3: 4 cells, header has 3" in out
    assert "- Intro (p1) ⚠" in out
    text = "--- page 1 ---\nHi\n--- page 2 ---\nYo"
    pages = render_pages({"text": text, "not_ready": []}, m).splitlines()
    assert pages[1].startswith("⚠ ") and "row 3: 4 cells" in pages[1]


def test_informational_table_notes_have_no_warning():
    notes = [
        "read by vision — verify",
        "two-level header combined",
        "continues on page 2",
        "may continue on page 2 (still converting)",
    ]
    m = manifest(tables=[_table("p1-t1", 1, 1, False, notes)])
    out = _ready(m)
    assert "⚠" not in out
    assert "p1-t1 (may continue on page 2 (still converting))" in out
    lines = render_pages({"text": "--- page 1 ---\nHi", "not_ready": []}, m).splitlines()
    assert lines[1] == "note: table p1-t1: " + "; ".join(notes)


def test_problem_note_classification_mirrors_the_server():
    for info in (
        "read by vision — verify",
        "two-level header combined",
        "continues on page 4",
        "may continue on page 4 (still converting)",
    ):
        assert not is_problem_note(info)
    for problem in (
        "may continue as p4-t1 on page 4 (headers differ)",
        "header row looks like data",
        '"Total" row 3 does not add up in column "X"',
        "page read with low confidence (score 40)",
    ):
        assert is_problem_note(problem)


def test_converting_document_says_which_ready_pages_are_shown():
    m = manifest(
        page_count=5,
        pages=[page(1), page(2), page(3), page(4, state="converting"), page(5, state="converting")],
        first_text_pages=[1, 2],
        complete=False,
    )
    out = render_document({"id": "d", "status": "converting", "manifest": m}, "x.pdf")
    assert "shown below: pages 1–2 of 5 (use read_pages for the rest)" in out
    m["first_text_pages"] = [1, 2, 3]
    out = render_document({"id": "d", "status": "converting", "manifest": m}, "x.pdf")
    assert "shown below" not in out


def test_uncertain_table_line_counts_the_problems_left_out():
    t = _table("p1-t1", 1, 1, True, ["first", "continues on page 2", "second", "third"])
    out = _ready(manifest(tables=[t]))
    assert "table p1-t1 on p1: first (+2 more)" in out
    out = render_pages({"text": "--- page 1 ---\nx", "not_ready": []}, manifest(tables=[t]))
    assert "table p1-t1: first (+2 more)" in out


def test_text_missing_is_a_concern_even_on_a_full_score_page():
    m = manifest(pages=[page(1, score=100, flags=["empty", "text_missing"]), page(2)])
    out = render_pages({"text": "--- page 1 ---\n\n--- page 2 ---\nx", "not_ready": []}, m)
    assert "⚠ text layer present but little or nothing extracted" in out
    assert "text layer present but little or nothing extracted on page(s) 1" in _ready(m)


def test_glued_words_flag_is_a_concern():
    m = manifest(pages=[page(1, score=60, flags=["glued_words"]), page(2)])
    out = render_pages(
        {"text": "--- page 1 ---\nthetimeof thesale.This\n--- page 2 ---\nx", "not_ready": []}, m
    )
    assert "⚠ words run together (spacing lost)" in out


def test_table_missed_flag_is_a_concern():
    m = manifest(pages=[page(1, score=100, flags=["table_missed"]), page(2)])
    out = render_pages(
        {"text": "--- page 1 ---\nTable 10-1. Chart\n--- page 2 ---\nx", "not_ready": []}, m
    )
    assert "⚠ a captioned table was read as text (no table id, so it cannot be exported)" in out


# Same list as the server's TIME_LEFT_TEXT (tests/test_document_manifest.py).
TIME_LEFT_TEXT = [
    (20, "under a minute"),
    (60, "about 1 minute"),
    (89, "about 1 minute"),
    (90, "about 2 minutes"),
    (3600, "about 60 minutes"),
]


def test_time_left_matches_the_server_wording():
    assert _time_left(None) is None
    for seconds, text in TIME_LEFT_TEXT:
        assert _time_left(seconds) == text, seconds


def test_not_converted_flag_is_a_concern():
    m = manifest(pages=[page(1, score=0, flags=["not_converted"]), page(2)])
    out = render_pages({"text": "--- page 1 ---\n\n--- page 2 ---\nx", "not_ready": []}, m)
    assert "⚠ page could not be converted" in out
