"""Update the EduNexus program files on this laptop without touching academic data.

Usage (through Update_EduNexus.bat):  update_app.py "<folder of the newer extracted EduNexus package>"

Replaced, from the new package:  backend/app, backend/scripts, backend/seed, backend/requirements.txt,
                                 backend/.env.example, dist, the .bat files, README_STUDENT.txt
Never touched:                   backend/data (database and uploads), backend/.env (this laptop's settings),
                                 backend/.venv, backups, logs, runtime

Steps: refuse while EduNexus runs -> verified backup of all data -> snapshot of the current program files
into backups/code-before-update-<time> -> copy in the new files -> additive database upgrade (checked).
If copying fails, the snapshot is copied back. Dependencies are reinstalled by the next start when
requirements.txt changed.
"""

from __future__ import annotations

import shutil
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from backup_restore import BackupError, RestoreError, create_backup, is_app_running, package_root  # noqa: E402
from upgrade_database import UpgradeError, upgrade  # noqa: E402

REPLACEABLE = [
    "backend/app",
    "backend/scripts",
    "backend/seed",
    "backend/requirements.txt",
    "backend/.env.example",
    "dist",
    "Start_EduNexus.bat",
    "Stop_EduNexus.bat",
    "Backup_EduNexus.bat",
    "Restore_EduNexus.bat",
    "Update_EduNexus.bat",
    "README_STUDENT.txt",
]


class UpdateError(RuntimeError):
    pass


def _check_source(root: Path, source: Path) -> None:
    if source.resolve() == root.resolve():
        raise UpdateError("Choose the NEW package folder, not this installed EduNexus folder.")
    required = ["backend/app/main.py", "backend/requirements.txt", "backend/scripts/launcher.py", "dist/index.html"]
    missing = [item for item in required if not (source / item).exists()]
    if missing:
        raise UpdateError(f"That folder is not a complete EduNexus package (missing {missing[0]}).")


def _remove(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()


def _copy(source: Path, destination: Path) -> None:
    if source.is_dir():
        shutil.copytree(source, destination, ignore=shutil.ignore_patterns("__pycache__"))
    else:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)


def update(root: Path, source: Path) -> dict:
    source = source.resolve()
    _check_source(root, source)
    if is_app_running(root):
        raise UpdateError("EduNexus is running. Close the EduNexus window (or run Stop_EduNexus.bat) and try again.")

    backup = create_backup(root, "before-update")
    snapshot = root / "backups" / f"code-before-update-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    snapshot.mkdir(parents=True)
    present = [item for item in REPLACEABLE if (root / item).exists()]
    for item in present:
        _copy(root / item, snapshot / item)

    try:
        for item in REPLACEABLE:
            if (source / item).exists():
                _remove(root / item)
                _copy(source / item, root / item)
    except Exception as error:
        for item in REPLACEABLE:
            _remove(root / item)
        for item in present:
            _copy(snapshot / item, root / item)
        raise UpdateError(f"The update could not be copied ({error}). The previous program files were restored. Your data was not changed.") from error

    try:
        result = upgrade(root, require_backup=True)
    except UpgradeError as error:
        raise UpdateError(f"Program files were updated, but the database upgrade stopped: {error} Your backup is in {backup}.") from error
    return {"backup": str(backup), "codeSnapshot": str(snapshot), "integrity": result["integrity"]}


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("Usage: update_app.py <folder of the new EduNexus package>")
        return 2
    try:
        result = update(package_root(), Path(args[0]))
    except (UpdateError, BackupError, RestoreError) as error:
        print(f"Update stopped: {error}")
        return 1
    print("Update complete. Your data was kept and checked.")
    print(f"Backup of your data before the update: {result['backup']}")
    print(f"Previous program files saved in: {result['codeSnapshot']}")
    print("Start EduNexus with Start_EduNexus.bat.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
