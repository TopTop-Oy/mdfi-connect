import csv
import os
import re
import secrets
import stat
from pathlib import Path

from mdfi_connect.client import ConnectorError

_NUMERIC = re.compile(r"^[+-]?(\d[\d,.   ]*|\.\d+)([eE][+-]?\d+)?%?$")
_DASHES = re.compile(r"^[-—–]+$")
_TRIGGERS = ("=", "+", "-", "@", "\t", "\r")


def _safe(name: str) -> str:
    return re.sub(r"[^\w.\-]+", "_", name).strip("._") or "table"


def _defuse(cell: object) -> tuple[object, bool]:
    if not isinstance(cell, str) or not cell.startswith(_TRIGGERS):
        return cell, False
    if _NUMERIC.match(cell) or _DASHES.match(cell):
        return cell, False
    return "'" + cell, True


def _prepare(table: dict) -> tuple[list, list[list], list[str]]:
    count = 0
    headers = []
    for cell in table["headers"]:
        cell, hit = _defuse(cell)
        headers.append(cell)
        count += hit
    rows = []
    for row in table["rows"]:
        out = []
        for cell in row:
            cell, hit = _defuse(cell)
            out.append(cell)
            count += hit
        rows.append(out)
    extra = []
    if count:
        many = count != 1
        extra.append(
            f"{count} cell{'s' if many else ''} began with =, +, -, @, tab or carriage return "
            f"and {'were' if many else 'was'} prefixed with ' "
            f"so spreadsheets do not run {'them' if many else 'it'} as {'formulas' if many else 'a formula'}"
        )
    width = len(table["headers"])
    ragged = [str(i) for i, row in enumerate(table["rows"], 1) if len(row) != width]
    if ragged:
        one = len(ragged) == 1
        extra.append(
            f"row{'' if one else 's'} {', '.join(ragged)} "
            f"{'has' if one else 'have'} a different number of cells than the header"
        )
    return headers, rows, extra


def all_notes(table: dict) -> list[str]:
    return [str(n).replace("\r", " ").replace("\n", " ") for n in table.get("notes") or []] + (
        _prepare(table)[2]
    )


def _open(path: Path, overwrite: bool) -> int:
    flags = os.O_WRONLY | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    flags |= os.O_TRUNC if overwrite else os.O_EXCL
    try:
        return os.open(path, flags, 0o644)
    except FileExistsError:
        raise ConnectorError(f"{path} exists — pass overwrite=true to replace it") from None


def write_table(
    table: dict,
    *,
    source_stem: str,
    cwd: Path,
    path: str | None,
    overwrite: bool,
) -> tuple[Path, Path | None]:
    root = cwd.resolve()
    if path is None:
        joined = root / "mdfi-tables" / f"{_safe(source_stem)}-{_safe(str(table['id']))}.csv"
    else:
        joined = root / Path(path).expanduser()
    if joined.suffix.lower() != ".csv":
        raise ConnectorError(f"export_table writes .csv files only: {path}")
    target = joined.parent.resolve() / joined.name
    notes_path = target.with_name(target.name + ".notes.txt")

    for p in (target, notes_path):
        try:
            parts = p.relative_to(root).parts
        except ValueError:
            raise ConnectorError(f"Refusing to write outside the working directory: {p}") from None
        if any(part.startswith(".") for part in parts):
            raise ConnectorError(f"Refusing to write into hidden file or folder: {p}")
    for p in (target, notes_path):
        if p.is_symlink():
            raise ConnectorError(f"Refusing to write through a symlink: {p}")
    if target.is_dir():
        raise ConnectorError(f"{target} is a directory")
    if not overwrite:
        for p in (target, notes_path):
            if p.exists():
                raise ConnectorError(f"{p} exists — pass overwrite=true to replace it")

    headers, rows, extra = _prepare(table)
    notes = [str(n).replace("\r", " ").replace("\n", " ") for n in table.get("notes") or []]
    notes += extra
    # Both files are written to fresh temp files beside their targets and renamed into
    # place only once complete, so a failure mid-write (a full disk) never leaves a
    # partial CSV or loses the previous one. The notes go in first: a CSV is never
    # visible without its notes.
    staged: list[Path] = []
    current = target
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        csv_tmp = _temp_beside(target)
        with _new_file(csv_tmp, target, overwrite, staged, encoding="utf-8-sig", newline="") as f:
            writer = csv.writer(f, quoting=csv.QUOTE_MINIMAL)
            writer.writerow(headers)
            writer.writerows(rows)
        notes_tmp = None
        if notes:
            current = notes_path
            notes_tmp = _temp_beside(notes_path)
            with _new_file(
                notes_tmp, notes_path, overwrite, staged, encoding="utf-8", newline="\n"
            ) as f:
                f.write("\n".join(notes) + "\n")
        if not overwrite:
            # Checked again just before the rename: a file created meanwhile is not
            # replaced. (A file created between this check and the rename still is.)
            for p in (target, notes_path):
                if os.path.lexists(p):
                    raise ConnectorError(f"{p} exists — pass overwrite=true to replace it")
        placed_notes = False
        if notes_tmp is not None:
            current = notes_path
            os.replace(notes_tmp, notes_path)  # renames over a symlink, never through it
            staged.remove(notes_tmp)
            placed_notes = True
        current = target
        try:
            os.replace(csv_tmp, target)
        except OSError:
            if placed_notes and not overwrite:
                notes_path.unlink(missing_ok=True)  # never notes without their CSV
            raise
        staged.remove(csv_tmp)
        if not notes:
            notes_path.unlink(missing_ok=True)
    except OSError as exc:
        _discard(staged)
        raise ConnectorError(f"Could not write {current}: {exc.strerror or exc}") from None
    except BaseException:
        # ConnectorError, and anything else (a cell UTF-8 cannot encode): no temp left.
        _discard(staged)
        raise
    return target, notes_path if notes else None


def _new_file(tmp: Path, target: Path, overwrite: bool, staged: list[Path], **text):
    """Open a fresh temp file (recorded in `staged` once it is ours) for writing."""
    fd = _open(tmp, overwrite=False)
    staged.append(tmp)
    try:
        _keep_mode(fd, target, overwrite)
        return os.fdopen(fd, "w", **text)
    except BaseException:
        os.close(fd)
        raise


def _keep_mode(fd: int, target: Path, overwrite: bool) -> None:
    # The rename replaces the file, permissions included: a 0o600 file being
    # overwritten must not come back 0o644.
    if not overwrite or not hasattr(os, "fchmod"):
        return
    try:
        mode = stat.S_IMODE(os.lstat(target).st_mode)
    except FileNotFoundError:
        return
    os.fchmod(fd, mode)


def _temp_beside(path: Path) -> Path:
    return path.with_name(f"{path.name}.{secrets.token_hex(4)}.tmp")


def _discard(paths: list[Path]) -> None:
    for p in paths:
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass
