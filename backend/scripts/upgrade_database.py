"""Bring this laptop's database up to the current schema. Additive only; never deletes or rewrites rows.

Runs the same schema steps the API performs at start-up (create missing tables, add missing columns,
add missing indexes). It refuses to run unless a verified backup exists, and it checks the database
before and after. Called by Update_EduNexus.bat, and it can be run by hand from backend/.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.config import Settings  # noqa: E402
from app.database import add_missing_columns, build_session_factory  # noqa: E402
from app.models import Base  # noqa: E402
from backup_restore import DB_NAME, list_backups, package_root  # noqa: E402


class UpgradeError(RuntimeError):
    pass


def _integrity(path: Path) -> str:
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        return connection.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        connection.close()


def upgrade(root: Path, require_backup: bool = True) -> dict:
    database = root / "backend" / "data" / DB_NAME
    if not database.is_file():
        raise UpgradeError("No database to upgrade yet. Start EduNexus once first.")
    if require_backup and not list_backups(root):
        raise UpgradeError("No verified backup exists. Run Backup_EduNexus.bat before upgrading.")
    before = _integrity(database)
    if before != "ok":
        raise UpgradeError(f"The database failed its integrity check before the upgrade ({before}). Nothing was changed.")
    settings = Settings(
        database_url=f"sqlite:///{database.as_posix()}",
        jwt_secret="upgrade-only-0123456789abcdef0123456789abcdef",
        seed_on_start=False,
        seed_demo_accounts=False,
        upload_dir=root / "backend" / "data" / "uploads",
        cors_origins=["http://127.0.0.1:8001"],
    )
    factory = build_session_factory(settings)
    engine = factory.kw["bind"]
    Base.metadata.create_all(engine)
    add_missing_columns(engine)
    engine.dispose()
    after = _integrity(database)
    if after != "ok":
        raise UpgradeError(f"The database failed its integrity check after the upgrade ({after}). Restore the latest backup.")
    return {"integrity": after}


if __name__ == "__main__":
    try:
        result = upgrade(package_root())
        print(f"Database upgrade complete. Integrity: {result['integrity']}")
    except UpgradeError as error:
        print(f"Stopped: {error}")
        sys.exit(1)
