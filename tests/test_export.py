import csv
import os
from pathlib import Path

import pytest

from mdfi_connect import server
from mdfi_connect.client import ConnectorError
from mdfi_connect.csv_export import write_table

TABLE = {
    "id": "p1-t1",
    "start_page": 1,
    "end_page": 1,
    "headers": ["a", "b"],
    "rows": [["1", "2"], ["3", "4"]],
    "notes": [],
    "uncertain": False,
}


def read(path):
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.reader(f))


def test_default_path_created_under_cwd(tmp_path):
    csv_path, notes = write_table(
        TABLE, source_stem="report", cwd=tmp_path, path=None, overwrite=False
    )
    assert csv_path == tmp_path.resolve() / "mdfi-tables" / "report-p1-t1.csv"
    assert notes is None
    assert read(csv_path) == [["a", "b"], ["1", "2"], ["3", "4"]]
    assert csv_path.read_bytes().startswith(b"\xef\xbb\xbf")
    assert not Path(str(csv_path) + ".notes.txt").exists()


def test_csv_round_trips_awkward_cells(tmp_path):
    table = {
        **TABLE,
        "rows": [["x, y", 'say "hi"'], ["line1\nline2", "café ✓"]],
    }
    csv_path, _ = write_table(table, source_stem="doc", cwd=tmp_path, path=None, overwrite=False)
    assert read(csv_path) == [table["headers"], *table["rows"]]


@pytest.mark.parametrize("bad", ["../out.csv", "/tmp/mdfi-outside-test.csv"])
def test_refuses_path_outside_cwd(tmp_path, bad):
    with pytest.raises(ConnectorError) as e:
        write_table(TABLE, source_stem="d", cwd=tmp_path, path=bad, overwrite=False)
    assert e.value.message.startswith("Refusing to write outside the working directory: ")


def test_refuses_symlink_escape(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    cwd = tmp_path / "proj"
    cwd.mkdir()
    (cwd / "link").symlink_to(outside)
    with pytest.raises(ConnectorError, match="outside the working directory"):
        write_table(TABLE, source_stem="d", cwd=cwd, path="link/x.csv", overwrite=False)
    assert not (outside / "x.csv").exists()


@pytest.mark.parametrize("bad", [".git/x.csv", "sub/.ssh/x.csv", ".hidden.csv"])
def test_refuses_hidden_component(tmp_path, bad):
    with pytest.raises(ConnectorError) as e:
        write_table(TABLE, source_stem="d", cwd=tmp_path, path=bad, overwrite=False)
    assert e.value.message.startswith("Refusing to write into hidden file or folder: ")
    assert not (tmp_path / ".git").exists()


def test_cwd_itself_may_be_hidden(tmp_path):
    cwd = tmp_path / ".work"
    cwd.mkdir()
    csv_path, _ = write_table(TABLE, source_stem="d", cwd=cwd, path=None, overwrite=False)
    assert csv_path.exists()


def test_refuses_overwrite_without_flag_and_overwrites_with_it(tmp_path):
    first, _ = write_table(TABLE, source_stem="d", cwd=tmp_path, path="o.csv", overwrite=False)
    with pytest.raises(ConnectorError) as e:
        write_table(TABLE, source_stem="d", cwd=tmp_path, path="o.csv", overwrite=False)
    assert e.value.message == f"{first} exists — pass overwrite=true to replace it"
    assert read(first) == [["a", "b"], ["1", "2"], ["3", "4"]]
    newer = {**TABLE, "rows": [["9", "9"]]}
    write_table(newer, source_stem="d", cwd=tmp_path, path="o.csv", overwrite=True)
    assert read(first) == [["a", "b"], ["9", "9"]]


def test_notes_file_written_with_one_note_per_line(tmp_path):
    table = {**TABLE, "notes": ["read by vision — verify", "cell p2 r3 unclear"], "uncertain": True}
    csv_path, notes = write_table(table, source_stem="d", cwd=tmp_path, path=None, overwrite=False)
    assert notes == Path(str(csv_path) + ".notes.txt")
    assert notes.read_text(encoding="utf-8").splitlines() == table["notes"]


def test_overwrite_replaces_or_removes_stale_notes(tmp_path):
    noisy = {**TABLE, "notes": ["old problem"], "uncertain": True}
    _csv_path, notes = write_table(
        noisy, source_stem="d", cwd=tmp_path, path="o.csv", overwrite=False
    )
    newer = {**TABLE, "notes": ["new note"]}
    _, notes2 = write_table(newer, source_stem="d", cwd=tmp_path, path="o.csv", overwrite=True)
    assert notes2.read_text(encoding="utf-8").splitlines() == ["new note"]
    _, notes3 = write_table(TABLE, source_stem="d", cwd=tmp_path, path="o.csv", overwrite=True)
    assert notes3 is None
    assert not notes.exists()


def test_orphan_notes_file_is_not_silently_clobbered(tmp_path):
    (tmp_path / "o.csv.notes.txt").write_text("keep me", encoding="utf-8")
    with pytest.raises(ConnectorError, match="overwrite=true"):
        write_table(TABLE, source_stem="d", cwd=tmp_path, path="o.csv", overwrite=False)
    assert not (tmp_path / "o.csv").exists()


class FakeClient:
    def __init__(self, table=None, error=None, source="report.pdf"):
        self._table, self._error, self._source = table, error, source

    def table(self, doc_id, table_id):
        if self._error:
            raise ConnectorError(self._error)
        return self._table

    def document(self, doc_id):
        return {"id": doc_id, "status": "ready", "source": self._source}


def use(monkeypatch, tmp_path, fake):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(server, "_client", lambda: fake)


def test_export_table_clean_reply(tmp_path, monkeypatch):
    use(monkeypatch, tmp_path, FakeClient(TABLE))
    out = server.export_table("1b4e28ba-2fa1-41d2-883f-0016d3cca427", "p1-t1")
    assert "mdfi-tables/report-p1-t1.csv" in out
    assert "2 rows × 2 columns" in out
    assert "uncertain" not in out.lower()
    assert (tmp_path / "mdfi-tables" / "report-p1-t1.csv").exists()


def test_uncertain_table_writes_notes_and_reply_leads_with_warning(tmp_path, monkeypatch):
    table = {
        **TABLE,
        "uncertain": True,
        "notes": ["read by vision — verify", "row 2 has an unreadable cell"],
    }
    use(monkeypatch, tmp_path, FakeClient(table))
    out = server.export_table("1b4e28ba-2fa1-41d2-883f-0016d3cca427", "p1-t1")
    assert out.startswith("Uncertain table")
    assert "read by vision — verify" in out
    assert "report-p1-t1.csv.notes.txt" in out
    notes = tmp_path / "mdfi-tables" / "report-p1-t1.csv.notes.txt"
    assert notes.read_text(encoding="utf-8").splitlines() == table["notes"]


def test_informational_notes_are_kept_but_reply_not_alarming(tmp_path, monkeypatch):
    table = {**TABLE, "notes": ["two-level header combined"]}
    use(monkeypatch, tmp_path, FakeClient(table))
    out = server.export_table("1b4e28ba-2fa1-41d2-883f-0016d3cca427", "p1-t1")
    assert not out.startswith("Uncertain")
    assert "two-level header combined" in out
    assert (tmp_path / "mdfi-tables" / "report-p1-t1.csv.notes.txt").exists()


def test_export_while_enhancing_returns_server_message(tmp_path, monkeypatch):
    msg = "table p1-t1 is still being enhanced — ask again shortly"
    use(monkeypatch, tmp_path, FakeClient(error=msg))
    assert server.export_table("1b4e28ba-2fa1-41d2-883f-0016d3cca427", "p1-t1") == msg
    assert not (tmp_path / "mdfi-tables").exists()


def test_export_unknown_table_returns_server_message(tmp_path, monkeypatch):
    use(monkeypatch, tmp_path, FakeClient(error="table p9-t9 not found"))
    assert (
        server.export_table("1b4e28ba-2fa1-41d2-883f-0016d3cca427", "p9-t9")
        == "table p9-t9 not found"
    )


def test_export_refusal_and_exists_are_replies(tmp_path, monkeypatch):
    use(monkeypatch, tmp_path, FakeClient(TABLE))
    assert server.export_table(
        "1b4e28ba-2fa1-41d2-883f-0016d3cca427", "p1-t1", path="../x.csv"
    ).startswith("Refusing to write outside")
    server.export_table("1b4e28ba-2fa1-41d2-883f-0016d3cca427", "p1-t1", path="x.csv")
    assert "overwrite=true" in server.export_table(
        "1b4e28ba-2fa1-41d2-883f-0016d3cca427", "p1-t1", path="x.csv"
    )
    assert "x.csv" in server.export_table(
        "1b4e28ba-2fa1-41d2-883f-0016d3cca427", "p1-t1", path="x.csv", overwrite=True
    )


def test_missing_source_falls_back_to_document(tmp_path, monkeypatch):
    use(monkeypatch, tmp_path, FakeClient(TABLE, source=None))
    server.export_table("1b4e28ba-2fa1-41d2-883f-0016d3cca427", "p1-t1")
    assert (tmp_path / "mdfi-tables" / "document-p1-t1.csv").exists()


# ---- formula injection, symlinks, partial writes ----


def _w(table, tmp_path, **kw):
    kw.setdefault("path", None)
    kw.setdefault("overwrite", False)
    return write_table(table, source_stem="d", cwd=tmp_path, **kw)


def test_formula_cells_are_prefixed_and_counted(tmp_path):
    table = {
        **TABLE,
        "headers": ["=h", "ok"],
        "rows": [
            ["=1+1", "@SUM(A1)"],
            ["+cmd|x", "\t=x"],
            ["-5", "+3.5"],
            ["-1 234", "-"],
            ["—", "–"],
            ["12%", "-.5"],
        ],
    }
    csv_path, notes = _w(table, tmp_path)
    got = read(csv_path)
    assert got[0] == ["'=h", "ok"]
    assert got[1:] == [
        ["'=1+1", "'@SUM(A1)"],
        ["'+cmd|x", "'\t=x"],
        ["-5", "+3.5"],
        ["-1 234", "-"],
        ["—", "–"],
        ["12%", "-.5"],
    ]
    text = notes.read_text(encoding="utf-8")
    assert (
        "5 cells began with =, +, -, @, tab or carriage return and were prefixed with ' "
        "so spreadsheets do not run them as formulas"
    ) in text


def test_single_formula_cell_singular_note(tmp_path):
    table = {**TABLE, "rows": [["=x", "1"]]}
    _, notes = _w(table, tmp_path)
    assert notes.read_text(encoding="utf-8").startswith("1 cell began")


def test_formula_reply_mentions_note_and_is_not_uncertain(tmp_path, monkeypatch):
    use(monkeypatch, tmp_path, FakeClient({**TABLE, "rows": [["=x", "1"]]}))
    out = server.export_table("1b4e28ba-2fa1-41d2-883f-0016d3cca427", "p1-t1")
    assert not out.startswith("Uncertain")
    assert "prefixed with '" in out


def test_non_csv_path_refused(tmp_path):
    for bad in ["x.txt", "x", "x.sh", "x.csv.exe"]:
        with pytest.raises(ConnectorError) as e:
            _w(TABLE, tmp_path, path=bad)
        assert e.value.message == f"export_table writes .csv files only: {bad}"
    _w(TABLE, tmp_path, path="UP.CSV")


def test_symlink_at_csv_path_refused(tmp_path):
    victim = tmp_path / "victim.txt"
    victim.write_text("keep")
    (tmp_path / "o.csv").symlink_to(victim)
    for ow in (False, True):
        with pytest.raises(ConnectorError) as e:
            _w(TABLE, tmp_path, path="o.csv", overwrite=ow)
        assert e.value.message.startswith("Refusing to write through a symlink: ")
    assert victim.read_text() == "keep"


def test_dangling_symlink_at_notes_path_refused(tmp_path):
    target = tmp_path / "gone.txt"
    (tmp_path / "o.csv.notes.txt").symlink_to(target)
    table = {**TABLE, "notes": ["n"]}
    for ow in (False, True):
        with pytest.raises(ConnectorError, match="through a symlink"):
            _w(table, tmp_path, path="o.csv", overwrite=ow)
    assert not target.exists()
    assert not (tmp_path / "o.csv").exists()


def test_notes_symlink_escaping_cwd_refused(tmp_path):
    cwd = tmp_path / "proj"
    cwd.mkdir()
    outside = tmp_path / "outside.txt"
    (cwd / "o.csv.notes.txt").symlink_to(outside)
    with pytest.raises(ConnectorError, match="symlink"):
        _w({**TABLE, "notes": ["n"]}, cwd, path="o.csv", overwrite=True)
    assert not outside.exists()


def _is_csv_temp(path):
    return path.name.endswith(".tmp") and ".notes.txt." not in path.name


def test_nothing_is_left_when_the_csv_cannot_be_created(tmp_path, monkeypatch):
    import mdfi_connect.csv_export as ce

    real = ce._open
    calls = []

    def fake(path, overwrite):
        calls.append(path.name)
        if _is_csv_temp(path):
            raise OSError(28, "No space left on device")
        return real(path, overwrite)

    monkeypatch.setattr(ce, "_open", fake)
    with pytest.raises(ConnectorError) as e:
        _w({**TABLE, "notes": ["n"], "uncertain": True}, tmp_path, path="o.csv")
    assert len(calls) == 1 and calls[0].startswith("o.csv.")
    assert e.value.message.startswith("Could not write ")
    assert "No space left" in e.value.message
    assert list(tmp_path.iterdir()) == []


def test_notes_rename_comes_before_the_csv_rename(tmp_path, monkeypatch):
    import mdfi_connect.csv_export as ce

    order = []
    real = os.replace

    def spy(src, dst):
        order.append(Path(dst).name)
        return real(src, dst)

    monkeypatch.setattr(ce.os, "replace", spy)
    _w({**TABLE, "notes": ["n"]}, tmp_path, path="o.csv")
    assert order == ["o.csv.notes.txt", "o.csv"]


def test_failed_csv_rename_removes_the_new_notes(tmp_path, monkeypatch):
    import mdfi_connect.csv_export as ce

    real = os.replace

    def fake(src, dst):
        if Path(dst).name == "o.csv":
            raise OSError(5, "I/O error")
        return real(src, dst)

    monkeypatch.setattr(ce.os, "replace", fake)
    with pytest.raises(ConnectorError):
        _w({**TABLE, "notes": ["n"]}, tmp_path, path="o.csv")
    assert list(tmp_path.iterdir()) == []


def test_ragged_rows_noted_and_written_as_is(tmp_path, monkeypatch):
    table = {**TABLE, "rows": [["1", "2"], ["1"], ["1", "2"], ["1", "2", "3"]]}
    csv_path, notes = _w(table, tmp_path)
    assert read(csv_path)[1:] == table["rows"]
    assert "rows 2, 4 have a different number of cells than the header" in notes.read_text(
        encoding="utf-8"
    )
    use(monkeypatch, tmp_path, FakeClient(table))
    assert "rows 2, 4 have" in server.export_table(
        "1b4e28ba-2fa1-41d2-883f-0016d3cca427", "p1-t1", path="r.csv"
    )


def test_directory_at_target_refused(tmp_path):
    (tmp_path / "d.csv").mkdir()
    with pytest.raises(ConnectorError, match="is a directory"):
        _w(TABLE, tmp_path, path="d.csv", overwrite=True)


def test_oserror_becomes_reply(tmp_path, monkeypatch):
    (tmp_path / "blocker").write_text("x")
    use(monkeypatch, tmp_path, FakeClient(TABLE))
    out = server.export_table("1b4e28ba-2fa1-41d2-883f-0016d3cca427", "p1-t1", path="blocker/x.csv")
    assert out.startswith("Could not write ")


def test_pluralisation(tmp_path, monkeypatch):
    t = {**TABLE, "headers": ["a"], "rows": [["1"]]}
    use(monkeypatch, tmp_path, FakeClient(t))
    assert "1 row × 1 column)" in server.export_table(
        "1b4e28ba-2fa1-41d2-883f-0016d3cca427", "p1-t1"
    )


def test_open_uses_o_binary_and_csv_has_crlf_only(tmp_path, monkeypatch):
    sentinel = 1 << 29
    monkeypatch.setattr(os, "O_BINARY", sentinel, raising=False)
    seen = []
    real = os.open

    def spy(path, flags, mode=0o777):
        seen.append(flags)
        return real(path, flags & ~sentinel, mode)

    monkeypatch.setattr(os, "open", spy)
    table = {**TABLE, "rows": [["line1\nline2", "2"], ["3", "4"]], "notes": ["n"]}
    csv_path, _ = _w(table, tmp_path)
    assert seen and all(f & sentinel for f in seen)
    raw = csv_path.read_bytes()
    assert b"\r\r\n" not in raw
    assert raw.count(b"\r\n") == 3
    assert b"line1\nline2" in raw


def test_note_wording_singular_plural(tmp_path):
    t = {**TABLE, "rows": [["=a", "1"], ["1"]]}
    _, notes = _w(t, tmp_path)
    text = notes.read_text(encoding="utf-8")
    assert "1 cell began with =, +, -, @, tab or carriage return and was prefixed" in text
    assert "so spreadsheets do not run it as a formula" in text
    assert "row 2 has a different number of cells than the header" in text


def test_notes_oserror_names_notes_path(tmp_path, monkeypatch):
    import mdfi_connect.csv_export as ce

    real = ce._open

    def fake(path, overwrite):
        if "notes" in path.name:
            raise OSError(13, "Permission denied")
        return real(path, overwrite)

    monkeypatch.setattr(ce, "_open", fake)
    with pytest.raises(ConnectorError) as e:
        _w({**TABLE, "notes": ["n"]}, tmp_path, path="o.csv")
    assert e.value.message.startswith("Could not write ")
    assert "o.csv.notes.txt" in e.value.message.split(":")[0]


def test_overwrite_csv_failure_keeps_previous_notes(tmp_path, monkeypatch):
    import mdfi_connect.csv_export as ce

    old = {**TABLE, "notes": ["old note"], "uncertain": True}
    _w(old, tmp_path, path="o.csv")
    real = ce._open

    def fake(path, overwrite):
        if _is_csv_temp(path):
            raise OSError(5, "I/O error")
        return real(path, overwrite)

    monkeypatch.setattr(ce, "_open", fake)
    with pytest.raises(ConnectorError):
        _w({**TABLE, "notes": ["new note"]}, tmp_path, path="o.csv", overwrite=True)
    assert (tmp_path / "o.csv.notes.txt").read_text(encoding="utf-8") == "old note\n"
    assert not list(tmp_path.glob("*.tmp"))


def test_uncertain_reply_names_problems_first_and_informational_notes_apart(tmp_path, monkeypatch):
    table = {
        **TABLE,
        "uncertain": True,
        "notes": ["read by vision — verify", "row 2 has an unreadable cell"],
    }
    use(monkeypatch, tmp_path, FakeClient(table))
    lines = server.export_table("1b4e28ba-2fa1-41d2-883f-0016d3cca427", "p1-t1").splitlines()
    assert lines[0] == (
        "Uncertain table — check it against the source before relying on it: "
        "row 2 has an unreadable cell"
    )
    assert "Notes: read by vision — verify" in lines


def _full_disk_during_rows(monkeypatch):
    import mdfi_connect.csv_export as ce

    real = ce.csv.writer

    class Writer:
        def __init__(self, f, **kw):
            self._inner = real(f, **kw)

        def writerow(self, row):
            self._inner.writerow(row)

        def writerows(self, rows):
            self._inner.writerow(next(iter(rows)))
            raise OSError(28, "No space left on device")

    monkeypatch.setattr(ce.csv, "writer", Writer)


def test_full_disk_mid_write_leaves_no_partial_csv(tmp_path, monkeypatch):
    _full_disk_during_rows(monkeypatch)
    with pytest.raises(ConnectorError, match="No space left"):
        _w({**TABLE, "notes": ["n"], "uncertain": True}, tmp_path, path="o.csv")
    assert sorted(p.name for p in tmp_path.iterdir()) == []


def test_full_disk_mid_overwrite_keeps_the_previous_csv_and_notes(tmp_path, monkeypatch):
    _w({**TABLE, "notes": ["old note"], "uncertain": True}, tmp_path, path="o.csv")
    before = (tmp_path / "o.csv").read_bytes()
    _full_disk_during_rows(monkeypatch)
    with pytest.raises(ConnectorError, match="No space left"):
        _w(
            {**TABLE, "rows": [["9", "9"]] * 3, "notes": ["new"]},
            tmp_path,
            path="o.csv",
            overwrite=True,
        )
    assert (tmp_path / "o.csv").read_bytes() == before
    assert (tmp_path / "o.csv.notes.txt").read_text(encoding="utf-8") == "old note\n"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["o.csv", "o.csv.notes.txt"]


def test_target_created_meanwhile_is_not_replaced(tmp_path, monkeypatch):
    import mdfi_connect.csv_export as ce

    real = ce._open

    def racing(path, overwrite):
        fd = real(path, overwrite)
        (tmp_path / "o.csv").write_text("someone else's", encoding="utf-8")
        return fd

    monkeypatch.setattr(ce, "_open", racing)
    with pytest.raises(ConnectorError, match="exists"):
        _w(TABLE, tmp_path, path="o.csv")
    assert (tmp_path / "o.csv").read_text(encoding="utf-8") == "someone else's"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["o.csv"]


def test_problem_notes_are_shown_even_if_not_marked_uncertain(tmp_path, monkeypatch):
    table = {**TABLE, "uncertain": False, "notes": ["row 2: 3 cells, header has 2"]}
    use(monkeypatch, tmp_path, FakeClient(table))
    out = server.export_table("1b4e28ba-2fa1-41d2-883f-0016d3cca427", "p1-t1")
    assert out.splitlines()[0] == (
        "Uncertain table — check it against the source before relying on it: "
        "row 2: 3 cells, header has 2"
    )


@pytest.mark.skipif(not hasattr(os, "fchmod"), reason="no POSIX permissions")
def test_overwrite_keeps_the_existing_files_permissions(tmp_path):
    csv_path, notes = _w({**TABLE, "notes": ["n"]}, tmp_path, path="o.csv")
    csv_path.chmod(0o600)
    notes.chmod(0o600)
    _w({**TABLE, "notes": ["m"]}, tmp_path, path="o.csv", overwrite=True)
    assert csv_path.stat().st_mode & 0o777 == 0o600
    assert notes.stat().st_mode & 0o777 == 0o600
    fresh, _ = _w(TABLE, tmp_path, path="new.csv")
    assert fresh.stat().st_mode & 0o777 == 0o644 & ~_umask()


def _umask() -> int:
    mask = os.umask(0)
    os.umask(mask)
    return mask


def test_unencodable_cell_leaves_no_temp_file(tmp_path):
    table = {**TABLE, "rows": [["\ud800", "2"]]}  # a lone surrogate cannot be UTF-8
    with pytest.raises(UnicodeEncodeError):
        _w(table, tmp_path, path="out.csv")
    assert list(tmp_path.iterdir()) == []


def test_fd_is_closed_and_temp_removed_when_keeping_the_mode_fails(tmp_path, monkeypatch):
    import mdfi_connect.csv_export as ce

    _w(TABLE, tmp_path, path="o.csv")
    opened = []
    real_open = ce._open

    def spy(path, overwrite):
        fd = real_open(path, overwrite)
        opened.append(fd)
        return fd

    def broken(fd, target, overwrite):
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(ce, "_open", spy)
    monkeypatch.setattr(ce, "_keep_mode", broken)
    with pytest.raises(ConnectorError, match="not permitted"):
        _w(TABLE, tmp_path, path="o.csv", overwrite=True)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["o.csv"]
    with pytest.raises(OSError):
        os.fstat(opened[0])  # closed
