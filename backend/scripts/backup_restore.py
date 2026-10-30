"""Local backup and restore for one EduNexus laptop installation.

Backs up everything that holds academic data in this installation:
  backend/data/edunexus.db   (copied with SQLite's backup API, so it is consistent even while EduNexus runs)
  backend/data/uploads/      (materials, question papers, submissions, tutor sources)
  any other file directly inside backend/data/

Each backup is a new timestamped folder under backups/. Existing backups are never overwritten.
Every backup gets a MANIFEST.json with the SHA-256 of each file, and is re-verified after it is written.
Restore needs the exact word RESTORE, refuses to run while EduNexus is running, and first makes a
verified backup of the current data.

Command line (run from the EduNexus folder, normally through Backup_EduNexus.bat / Restore_EduNexus.bat):
  backup_restore.py backup
  backup_restore.py list
  backup_restore.py restore [backup-folder]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Callable

DB_NAME = "edunexus.db"
MANIFEST = "MANIFEST.json"
KEY_TABLES = ("users", "student_profiles", "courses", "enrollments", "attendance_records", "marks", "quiz_attempts", "documents", "previous_papers", "submissions", "announcements")
PORT_RANGE = range(8001, 8011)
CONFIRMATION = "RESTORE"


class BackupError(RuntimeError):
    """A backup could not be created or verified. Nothing has been replaced."""


class RestoreError(RuntimeError):
    """A restore was refused or could not be verified."""


def package_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_database_side_file(name: str) -> bool:
    return name.startswith(DB_NAME) and name != DB_NAME or name in {f"{DB_NAME}-journal", f"{DB_NAME}-wal", f"{DB_NAME}-shm"}


def _copy_database(source: Path, destination: Path) -> None:
    reader = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)
    try:
        writer = sqlite3.connect(destination)
        try:
            reader.backup(writer)
        finally:
            writer.close()
    finally:
        reader.close()


def _database_report(db_path: Path) -> dict:
    connection = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    try:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        counts = {name: connection.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0] for name in KEY_TABLES if name in tables}
    finally:
        connection.close()
    return {"integrity": integrity, "counts": counts}


def _copy_verified(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    if _sha256(source) != _sha256(destination):
        raise BackupError(f"Copy verification failed for {source.name}.")


def _copy_tree_verified(source: Path, destination: Path) -> int:
    count = 0
    for path in sorted(source.rglob("*")):
        if path.is_file():
            _copy_verified(path, destination / path.relative_to(source))
            count += 1
    destination.mkdir(parents=True, exist_ok=True)
    return count


def _data_paths(root: Path) -> tuple[Path, Path, Path, Path]:
    data = root / "backend" / "data"
    return data, data / DB_NAME, data / "uploads", root / "backups"


def create_backup(root: Path, label: str = "manual") -> Path:
    data, db, uploads, backups = _data_paths(root)
    if not db.is_file():
        raise BackupError(f"No database was found at {db}. Start EduNexus once first.")
    backups.mkdir(parents=True, exist_ok=True)
    base = f"{label}-{_stamp()}"
    destination = backups / base
    counter = 2
    while destination.exists():
        destination = backups / f"{base}-{counter}"
        counter += 1
    destination.mkdir()
    staged = destination / "data"
    staged.mkdir()
    try:
        _copy_database(db, staged / DB_NAME)
        for entry in sorted(data.iterdir()):
            if entry.is_file() and entry.name != DB_NAME and not _is_database_side_file(entry.name):
                _copy_verified(entry, staged / entry.name)
        if uploads.is_dir():
            _copy_tree_verified(uploads, staged / "uploads")
        files = [
            {"path": path.relative_to(destination).as_posix(), "size": path.stat().st_size, "sha256": _sha256(path)}
            for path in sorted(destination.rglob("*"))
            if path.is_file()
        ]
        report = _database_report(staged / DB_NAME)
        if report["integrity"] != "ok":
            raise BackupError(f"The copied database failed its integrity check: {report['integrity']}")
        manifest = {
            "format": 1,
            "label": label,
            "createdAt": datetime.now().astimezone().isoformat(timespec="seconds"),
            "database": report,
            "files": files,
        }
        (destination / MANIFEST).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        verify_backup(destination)
    except Exception:
        shutil.rmtree(destination, ignore_errors=True)
        raise
    (destination / "README.txt").write_text(
        "EduNexus backup. Do not edit these files.\n"
        "data/edunexus.db is your database; data/uploads holds your uploaded files.\n"
        "Restore with Restore_EduNexus.bat and choose this folder.\n",
        encoding="utf-8",
    )
    return destination


def verify_backup(backup: Path) -> dict:
    manifest_path = backup / MANIFEST
    if not manifest_path.is_file():
        raise BackupError(f"{backup.name} is not a complete backup (MANIFEST.json is missing).")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    expected = {item["path"]: item for item in manifest["files"]}
    for relative, item in expected.items():
        path = backup / relative
        if not path.is_file():
            raise BackupError(f"Backup file is missing: {relative}")
        if path.stat().st_size != item["size"] or _sha256(path) != item["sha256"]:
            raise BackupError(f"Backup file does not match its checksum: {relative}")
    present = {path.relative_to(backup).as_posix() for path in backup.rglob("*") if path.is_file()}
    extra = present - set(expected) - {MANIFEST, "README.txt"}
    if extra:
        raise BackupError(f"Backup contains unexpected files: {sorted(extra)[:3]}")
    if f"data/{DB_NAME}" not in expected:
        raise BackupError("Backup does not contain the database.")
    report = _database_report(backup / "data" / DB_NAME)
    if report["integrity"] != "ok":
        raise BackupError("Backup database failed its integrity check.")
    return manifest


def list_backups(root: Path) -> list[Path]:
    _, _, _, backups = _data_paths(root)
    if not backups.is_dir():
        return []
    return sorted((path for path in backups.iterdir() if (path / MANIFEST).is_file()), key=lambda path: path.name, reverse=True)


def _port_answers_as_edunexus(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=1) as response:
            return response.status == 200 and b'"status":"ok"' in response.read().replace(b" ", b"")
    except Exception:
        return False


def is_app_running(root: Path) -> bool:
    return any(_port_answers_as_edunexus(port) for port in PORT_RANGE)


def restore_backup(root: Path, backup: Path, confirmation: str, running_check: Callable[[Path], bool] = is_app_running) -> dict:
    if confirmation != CONFIRMATION:
        raise RestoreError(f"Restore was not confirmed. Type {CONFIRMATION} exactly to continue.")
    if running_check(root):
        raise RestoreError("EduNexus is running. Close the EduNexus window (or run Stop_EduNexus.bat) and try again.")
    manifest = verify_backup(backup)
    pre_restore = create_backup(root, "before-restore")
    data, db, uploads, _ = _data_paths(root)
    stamp = _stamp()

    temporary = data / f"{DB_NAME}.restore-tmp"
    _copy_database(backup / "data" / DB_NAME, temporary)
    if _database_report(temporary)["integrity"] != "ok":
        temporary.unlink(missing_ok=True)
        raise RestoreError("The backup database failed its integrity check; nothing was replaced.")
    for suffix in ("-wal", "-shm", "-journal"):
        Path(str(db) + suffix).unlink(missing_ok=True)
    os.replace(temporary, db)

    if uploads.exists():
        uploads.rename(data / f"uploads.before-restore-{stamp}")
    uploads.mkdir(parents=True, exist_ok=True)
    for item in manifest["files"]:
        relative = item["path"]
        if not relative.startswith("data/") or relative == f"data/{DB_NAME}":
            continue
        _copy_verified(backup / relative, root / "backend" / relative)

    for item in manifest["files"]:
        path = root / "backend" / item["path"]
        if not path.is_file() or _sha256(path) != item["sha256"]:
            raise RestoreError(f"Restored file failed verification: {item['path']}. The previous data is in {pre_restore}.")
    return {"restoredFrom": str(backup), "preRestoreBackup": str(pre_restore)}


def _choose_backup(root: Path) -> Path:
    backups = list_backups(root)
    if not backups:
        raise RestoreError("No verified backups were found in the backups folder.")
    print("Available backups (newest first):")
    for index, path in enumerate(backups, start=1):
        print(f"  {index}. {path.name}")
    choice = input("Enter the number of the backup to restore (or press Enter to cancel): ").strip()
    if not choice:
        raise RestoreError("Restore cancelled. Nothing was changed.")
    if not choice.isdigit() or not 1 <= int(choice) <= len(backups):
        raise RestoreError("That is not a number from the list. Nothing was changed.")
    return backups[int(choice) - 1]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Back up and restore EduNexus local data.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("backup")
    sub.add_parser("list")
    restore = sub.add_parser("restore")
    restore.add_argument("folder", nargs="?")
    args = parser.parse_args(argv)
    root = package_root()
    try:
        if args.command == "backup":
            path = create_backup(root, "manual")
            print(f"Backup created and verified: {path}")
        elif args.command == "list":
            for path in list_backups(root):
                print(path.name)
            return 0
        elif args.command == "restore":
            backup = Path(args.folder).resolve() if args.folder else _choose_backup(root)
            print(f"You are about to replace your current EduNexus data with: {backup.name}")
            print("Your current data will be backed up first, so you can restore it again later.")
            typed = input(f"Type {CONFIRMATION} and press Enter to continue: ").strip()
            result = restore_backup(root, backup, typed)
            print("Restore complete and verified.")
            print(f"Your data before the restore is saved in: {result['preRestoreBackup']}")
            print("Start EduNexus again with Start_EduNexus.bat.")
    except (BackupError, RestoreError) as error:
        print(f"Stopped: {error}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
