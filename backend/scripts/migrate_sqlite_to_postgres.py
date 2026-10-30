#!/usr/bin/env python
"""Copy the EduNexus SQLite database and uploaded files into PostgreSQL, safely.

What the script does, in order:

1. Verifies the source file is a SQLite database and records its SHA-256.
2. Writes a consistent snapshot of the source (SQLite backup API) and a copy of the
   uploads folder into the backup directory. The original database is opened read-only,
   is checked to be unchanged afterwards, and is never modified, moved, or deleted.
3. Inside one PostgreSQL transaction, creates the schema from the SQLAlchemy models
   (no seed data is created) and copies every table in foreign-key order, keeping all
   primary and foreign keys. Stored file paths become portable keys relative to
   UPLOAD_DIR, and the files are copied with checksum verification.
4. Validates the target before committing: row counts, order-independent row digests
   (these cover password hashes, institutional IDs, attendance, marks, enrolments, and
   quiz attempts), foreign-key integrity, and file checksums.
5. Commits only if every check passes. Otherwise the whole transaction is rolled back.

Rows are inserted with ON CONFLICT DO NOTHING, so a repeated run never overwrites a row
that already exists in the target. A target that already contains rows is refused unless
--allow-existing-rows is given; the digest check then confirms that the target still
matches the source.

Usage (from the backend folder):

    python scripts/migrate_sqlite_to_postgres.py --dry-run
    python scripts/migrate_sqlite_to_postgres.py

The target comes from --target-url, or DATABASE_URL when that is not set. The target must
be PostgreSQL unless --allow-sqlite-target is given, which exists only for automated tests.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from sqlalchemy import DateTime, create_engine, func, inspect, select  # noqa: E402
from sqlalchemy.exc import IntegrityError  # noqa: E402
from sqlalchemy.dialects.postgresql import insert as pg_insert  # noqa: E402
from sqlalchemy.dialects.sqlite import insert as sqlite_insert  # noqa: E402

from app.models import Base  # noqa: E402
from app.services.storage import legacy_relative_key  # noqa: E402

SQLITE_MAGIC = b"SQLite format 3\x00"
BATCH_SIZE = 500
# table -> (column holding the stored path, column holding the expected SHA-256 of that file)
STORAGE_COLUMNS = {
    "documents": ("storage_path", "sha256"),
    "previous_papers": ("storage_path", "sha256"),
    "submissions": ("file_storage_path", None),
}


class MigrationFailed(RuntimeError):
    """Validation failed. The transaction is rolled back and nothing is committed."""


class DryRunComplete(Exception):
    """Every check passed in dry-run mode. The transaction is rolled back on purpose."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def verify_sqlite_file(path: Path) -> None:
    if not path.is_file():
        raise FileNotFoundError(f"Source database not found: {path}")
    with path.open("rb") as handle:
        if handle.read(16) != SQLITE_MAGIC:
            raise ValueError(f"Source is not a SQLite database: {path}")


def snapshot_source(source: Path, backup_dir: Path, uploads_source: Path, skip_uploads: bool) -> dict[str, Any]:
    """Write a consistent copy of the database and uploads before anything is migrated."""
    verify_sqlite_file(source)
    backup_dir.mkdir(parents=True, exist_ok=True)
    snapshot = backup_dir / source.name
    if snapshot.exists():
        raise FileExistsError(f"{snapshot} already exists. Choose a new --backup-dir so earlier backups are never overwritten.")
    before = sha256_file(source)
    reader = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)
    try:
        writer = sqlite3.connect(snapshot)
        try:
            reader.backup(writer)
        finally:
            writer.close()
    finally:
        reader.close()
    after = sha256_file(source)
    if before != after:
        raise RuntimeError("The source database changed while it was being backed up. Stop the old local app and retry.")
    uploads_copy = None
    if uploads_source.is_dir() and not skip_uploads:
        uploads_copy = backup_dir / "uploads"
        shutil.copytree(uploads_source, uploads_copy)
    return {
        "source": str(source),
        "sourceSha256Before": before,
        "sourceSha256After": after,
        "snapshot": str(snapshot),
        "snapshotSha256": sha256_file(snapshot),
        "uploadsBackup": str(uploads_copy) if uploads_copy else None,
    }


def to_key(raw: str) -> str:
    """Return a relative storage key. Absolute laptop paths are mapped below their uploads folder."""
    text = raw.replace("\\", "/").strip()
    absolute = text.startswith("/") or (len(text) > 1 and text[1] == ":")
    if absolute:
        key = legacy_relative_key(text)
        if key is None:
            raise ValueError(f"Cannot map stored path to uploads: {raw}")
        return key
    return text


def is_safe_key(key: str) -> bool:
    return bool(key) and not any(part in {"", ".", ".."} for part in key.split("/"))


def normalize(value: Any) -> str:
    if value is None:
        return "\\N"
    if isinstance(value, datetime):
        aware = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return aware.astimezone(timezone.utc).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def row_digest(row: dict[str, Any], columns: list[str]) -> str:
    line = "|".join(f"{name}={normalize(row.get(name))}" for name in columns)
    return hashlib.sha256(line.encode("utf-8")).hexdigest()


def set_digest(hashes: list[str]) -> str:
    return hashlib.sha256("\n".join(sorted(hashes)).encode("utf-8")).hexdigest()


def copy_stored_file(source_root: Path, target_root: Path, key: str, expected_sha: str | None, dry_run: bool) -> str | None:
    """Copy one stored file. Returns a problem description, or None when the file is in place."""
    source_file = source_root / key
    target_file = target_root / key
    if not source_file.is_file():
        return f"missing source file: {key}"
    source_sha = sha256_file(source_file)
    if expected_sha and source_sha != expected_sha:
        return f"checksum mismatch for source file: {key}"
    if dry_run:
        return None
    if target_file.is_file() and sha256_file(target_file) == source_sha:
        return None
    target_file.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_file, target_file)
    if sha256_file(target_file) != source_sha:
        return f"copy verification failed: {key}"
    return None


def _insert_ignore(connection, table, rows: list[dict[str, Any]]) -> None:
    if connection.dialect.name == "postgresql":
        statement = pg_insert(table).values(rows).on_conflict_do_nothing()
    else:
        statement = sqlite_insert(table).values(rows).on_conflict_do_nothing()
    connection.execute(statement)


def _write_report(backup_dir: Path, report: dict[str, Any]) -> None:
    backup_dir.mkdir(parents=True, exist_ok=True)
    (backup_dir / "migration-report.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")


def migrate(
    *,
    source: Path,
    target_url: str,
    source_uploads: Path,
    target_uploads: Path,
    backup_dir: Path,
    dry_run: bool = False,
    allow_existing_rows: bool = False,
    allow_missing_files: bool = False,
    allow_sqlite_target: bool = False,
    skip_upload_backup: bool = False,
    quarantine_orphans: bool = False,
) -> dict[str, Any]:
    """Run the migration and return the report. Raises MigrationFailed when validation fails.

    Orphans are rows whose parent row is missing from the source. SQLite does not enforce foreign
    keys, so such rows can exist in the source. By default they block the migration. With
    quarantine_orphans they are left out of the target and written to the report verbatim; the
    source snapshot still contains them.
    """
    source = Path(source).resolve()
    target_engine = create_engine(target_url, future=True, pool_pre_ping=True)
    dialect = target_engine.dialect.name
    if dialect != "postgresql" and not allow_sqlite_target:
        raise ValueError("The migration target must be PostgreSQL. Set DATABASE_URL or --target-url to a postgresql+psycopg:// URL.")
    if dialect == "sqlite" and Path(target_engine.url.database or "").resolve() == source:
        raise ValueError("The target database must not be the source SQLite file.")

    report: dict[str, Any] = {
        "startedAt": datetime.now(timezone.utc).isoformat(),
        "dryRun": dry_run,
        "target": target_engine.url.render_as_string(hide_password=True),
    }
    report["snapshot"] = snapshot_source(source, backup_dir, source_uploads, skip_upload_backup)
    snapshot_engine = create_engine(f"sqlite:///{Path(report['snapshot']['snapshot']).as_posix()}", future=True)
    tables = Base.metadata.sorted_tables
    table_reports: dict[str, Any] = {}
    file_problems: list[str] = []
    errors: list[str] = []
    orphans_found: list[str] = []
    quarantined: list[dict[str, Any]] = []
    files_copied = 0
    referenced_columns: dict[str, set[str]] = {}
    for table in tables:
        for foreign_key in table.foreign_keys:
            referenced_columns.setdefault(foreign_key.column.table.name, set()).add(foreign_key.column.name)
    copied_values: dict[tuple[str, str], set[Any]] = {
        (table_name, column_name): set() for table_name, columns in referenced_columns.items() for column_name in columns
    }

    try:
        with snapshot_engine.connect() as source_conn, target_engine.begin() as target_conn:
            Base.metadata.create_all(target_conn)
            source_inspector = inspect(source_conn)
            source_tables = set(source_inspector.get_table_names())
            common_by_table: dict[str, list[str]] = {}
            for table in tables:
                if table.name in source_tables:
                    source_columns = {column["name"] for column in source_inspector.get_columns(table.name)}
                    common_by_table[table.name] = [column.name for column in table.columns if column.name in source_columns]

            populated = {}
            for table in tables:
                count = target_conn.execute(select(func.count()).select_from(table)).scalar_one()
                if count:
                    populated[table.name] = count
            if populated and not allow_existing_rows:
                raise ValueError(f"The target already contains rows in {sorted(populated)}. Use an empty PostgreSQL database, or pass --allow-existing-rows to resume a run.")

            for table in tables:
                if table.name not in common_by_table:
                    table_reports[table.name] = {"sourceRows": 0, "note": "absent in source"}
                    continue
                names = common_by_table[table.name]
                datetime_names = [name for name in names if isinstance(table.c[name].type, DateTime)]
                storage_name, sha_name = STORAGE_COLUMNS.get(table.name, (None, None))
                pk_names = [column.name for column in table.primary_key.columns]
                ordered = select(*[table.c[name] for name in names]).order_by(*[table.c[name] for name in pk_names])
                source_hashes: list[str] = []
                batch: list[dict[str, Any]] = []
                quarantined_here = 0
                for mapping in source_conn.execute(ordered).mappings():
                    row = dict(mapping)
                    orphan_reason = None
                    for foreign_key in table.foreign_keys:
                        value = row.get(foreign_key.parent.name)
                        parent_ref = (foreign_key.column.table.name, foreign_key.column.name)
                        if value is not None and value not in copied_values[parent_ref]:
                            orphan_reason = f"{foreign_key.parent.name} references a {parent_ref[0]} row that is not in the source"
                            break
                    if orphan_reason:
                        if quarantine_orphans:
                            quarantined.append({"table": table.name, "primaryKey": {name: normalize(row.get(name)) for name in pk_names}, "reason": orphan_reason, "row": {name: normalize(row.get(name)) for name in names}})
                            quarantined_here += 1
                        else:
                            orphans_found.append(f"{table.name} {'/'.join(normalize(row.get(name)) for name in pk_names)}: {orphan_reason}")
                        continue
                    for name in datetime_names:
                        if row[name] is not None and row[name].tzinfo is None:
                            row[name] = row[name].replace(tzinfo=timezone.utc)
                    if storage_name and row.get(storage_name):
                        try:
                            key = to_key(str(row[storage_name]))
                        except ValueError as error:
                            file_problems.append(str(error))
                            key = ""
                        if key and not is_safe_key(key):
                            file_problems.append(f"unsafe stored path in {table.name}: {row[storage_name]}")
                        elif key:
                            expected = row.get(sha_name) if sha_name else None
                            problem = copy_stored_file(source_uploads, target_uploads, key, expected, dry_run)
                            if problem:
                                file_problems.append(problem)
                            elif not dry_run:
                                files_copied += 1
                        row[storage_name] = key
                    source_hashes.append(row_digest(row, names))
                    for column_name in referenced_columns.get(table.name, ()):
                        copied_values[(table.name, column_name)].add(row[column_name])
                    batch.append(row)
                    if len(batch) >= BATCH_SIZE:
                        _insert_ignore(target_conn, table, batch)
                        batch = []
                if batch:
                    _insert_ignore(target_conn, table, batch)
                table_reports[table.name] = {"sourceRows": len(source_hashes), "sourceDigest": set_digest(source_hashes), "quarantinedRows": quarantined_here}

            for table in tables:
                if table.name not in common_by_table:
                    continue
                names = common_by_table[table.name]
                report_row = table_reports[table.name]
                target_hashes = [row_digest(dict(mapping), names) for mapping in target_conn.execute(select(*[table.c[name] for name in names])).mappings()]
                report_row["targetRows"] = len(target_hashes)
                report_row["targetDigest"] = set_digest(target_hashes)
                report_row["digestMatch"] = report_row["sourceDigest"] == report_row["targetDigest"]
                if report_row["sourceRows"] != report_row["targetRows"]:
                    errors.append(f"{table.name}: {report_row['sourceRows']} rows in source, {report_row['targetRows']} in target")
                elif not report_row["digestMatch"]:
                    errors.append(f"{table.name}: row contents differ between source and target")

            for table in tables:
                for foreign_key in table.foreign_keys:
                    child_column = table.c[foreign_key.parent.name]
                    orphans = target_conn.execute(
                        select(func.count()).select_from(table).where(child_column.is_not(None), child_column.not_in(select(foreign_key.column)))
                    ).scalar_one()
                    if orphans:
                        errors.append(f"{table.name}.{foreign_key.parent.name}: {orphans} orphaned rows")

            if orphans_found:
                errors.append(f"{len(orphans_found)} orphaned source rows reference missing parents (re-run with --quarantine-orphans to park them in the report)")
            if file_problems and not allow_missing_files:
                errors.extend(file_problems)
            report.update({
                "tables": table_reports,
                "filesCopied": files_copied,
                "fileProblems": file_problems,
                "orphanRows": orphans_found,
                "quarantined": quarantined,
                "errors": errors,
            })
            if errors:
                raise MigrationFailed("; ".join(errors))
            if dry_run:
                raise DryRunComplete()
    except DryRunComplete:
        report.update({"status": "DRY_RUN_PASSED", "finishedAt": datetime.now(timezone.utc).isoformat()})
    except MigrationFailed as failure:
        report.update({"status": "FAILED", "failureReason": str(failure), "finishedAt": datetime.now(timezone.utc).isoformat()})
        _write_report(backup_dir, report)
        raise
    else:
        report.update({"status": "PASSED", "finishedAt": datetime.now(timezone.utc).isoformat()})
    finally:
        snapshot_engine.dispose()
        target_engine.dispose()
    _write_report(backup_dir, report)
    return report


def main(argv: list[str] | None = None) -> int:
    from app.config import load_settings

    settings = load_settings()
    parser = argparse.ArgumentParser(description="Migrate EduNexus SQLite data and uploads into PostgreSQL.")
    parser.add_argument("--source", default=str(BACKEND_ROOT / "data" / "edunexus.db"), help="SQLite database to copy (never modified).")
    parser.add_argument("--target-url", default=settings.database_url, help="PostgreSQL SQLAlchemy URL. Defaults to DATABASE_URL.")
    parser.add_argument("--source-uploads", default=str(BACKEND_ROOT / "data" / "uploads"), help="Folder that holds the SQLite-era uploads.")
    parser.add_argument("--target-uploads", default=str(settings.upload_dir), help="Persistent UPLOAD_DIR on the server.")
    parser.add_argument("--backup-dir", default=str(BACKEND_ROOT.parent / "backups" / f"cutover-{utc_stamp()}"), help="Where the snapshot, uploads copy, and report are written.")
    parser.add_argument("--dry-run", action="store_true", help="Run every check and roll back without committing rows or copying files.")
    parser.add_argument("--allow-existing-rows", action="store_true", help="Permit a target that already has rows; existing rows are never overwritten.")
    parser.add_argument("--allow-missing-files", action="store_true", help="Migrate rows even when some stored files are missing (recorded in the report).")
    parser.add_argument("--skip-upload-backup", action="store_true", help="Do not copy the uploads folder into the backup directory.")
    parser.add_argument("--quarantine-orphans", action="store_true", help="Leave source rows whose parent is missing out of the target; list them in the report (the snapshot keeps them).")
    parser.add_argument("--allow-sqlite-target", action="store_true", help="Testing only: allow a SQLite target.")
    args = parser.parse_args(argv)
    backup_dir = Path(args.backup_dir)
    try:
        report = migrate(
            source=Path(args.source),
            target_url=args.target_url,
            source_uploads=Path(args.source_uploads),
            target_uploads=Path(args.target_uploads),
            backup_dir=backup_dir,
            dry_run=args.dry_run,
            allow_existing_rows=args.allow_existing_rows,
            allow_missing_files=args.allow_missing_files,
            allow_sqlite_target=args.allow_sqlite_target,
            skip_upload_backup=args.skip_upload_backup,
            quarantine_orphans=args.quarantine_orphans,
        )
    except MigrationFailed as failure:
        print(f"Migration rolled back, nothing was committed: {failure}")
        print(f"Report: {backup_dir / 'migration-report.json'}")
        return 1
    except IntegrityError as error:
        print(f"Migration rolled back, nothing was committed: the target rejected a row ({str(error.orig).splitlines()[0]}).")
        print(f"Report: {backup_dir / 'migration-report.json'}")
        return 1
    except (ValueError, FileNotFoundError, FileExistsError, RuntimeError) as error:
        print(f"Migration refused: {error}")
        return 2
    print(f"Status: {report['status']}")
    print(f"{'table':34} {'source':>8} {'target':>8}  digest")
    for name, row in report["tables"].items():
        status = "match" if row.get("digestMatch") else ("n/a" if "digestMatch" not in row else "DIFFERENT")
        print(f"{name:34} {row.get('sourceRows', 0):>8} {row.get('targetRows', 0):>8}  {status}")
    print(f"Files copied: {report.get('filesCopied', 0)}; file problems: {len(report.get('fileProblems', []))}")
    print(f"Quarantined orphan rows: {len(report.get('quarantined', []))}")
    print(f"Report: {backup_dir / 'migration-report.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
