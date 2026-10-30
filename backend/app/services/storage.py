"""Portable keys for uploaded files.

Files are stored under UPLOAD_DIR and recorded in the database as relative POSIX keys
(for example ``question-papers/<id>.pdf``). Keys stay valid when the upload volume is
mounted at a different path or moved to another host. Older rows that stored an absolute
path from a laptop are mapped into UPLOAD_DIR by their trailing ``uploads/`` segment.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath


def _posix(raw: str) -> str:
    return raw.replace("\\", "/").strip()


def legacy_relative_key(raw: str) -> str | None:
    """Return the key that follows the first ``uploads`` segment of an absolute path, if any."""
    parts = PurePosixPath(_posix(raw)).parts
    if not parts:
        return None
    lowered = [part.casefold() for part in parts]
    if "uploads" not in lowered:
        return None
    index = lowered.index("uploads")
    remainder = parts[index + 1 :]
    if not remainder:
        return None
    return PurePosixPath(*remainder).as_posix()


def storage_key(upload_dir: Path, path: Path) -> str:
    """Return the relative key for a file that has been written under upload_dir."""
    root = Path(upload_dir).resolve()
    resolved = Path(path).resolve()
    try:
        return resolved.relative_to(root).as_posix()
    except ValueError as error:
        raise ValueError("Uploaded files must be stored under UPLOAD_DIR.") from error


def resolve_storage_path(upload_dir: Path, stored: str | None) -> Path | None:
    """Map a stored key to a file inside upload_dir. Returns None for empty or escaping values."""
    if not stored:
        return None
    root = Path(upload_dir).resolve()
    text = _posix(stored)
    absolute = PurePosixPath(text).is_absolute() or (len(text) > 1 and text[1] == ":")
    if absolute:
        key = legacy_relative_key(text)
        if key is None:
            return None
    else:
        key = text
    candidate = (root / key).resolve()
    if root != candidate and root not in candidate.parents:
        return None
    return candidate
