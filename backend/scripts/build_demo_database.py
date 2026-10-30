"""Build the synthetic demo database that ships in the student package. Developer use only.

Creates a brand-new SQLite database and fills it with fictional data: the documented demo accounts,
the synthetic CSE curriculum (8 semesters, 24 subjects, sections A and B, fictional students,
faculty, attendance, marks, materials and assignments), and their uploads. It refuses to touch an
existing file. No real student or staff data is used.

Usage (from backend/):  python scripts/build_demo_database.py ../EDUNEXUS_STAGING/seed
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from sqlalchemy import func, select  # noqa: E402

from app.config import Settings  # noqa: E402
from app.database import add_missing_columns, build_session_factory  # noqa: E402
from app.models import Base, Course, Enrollment, User  # noqa: E402
from app.services.seed import seed_database, seed_demo_students  # noqa: E402
from seed_cse_curriculum import seed_cse_curriculum  # noqa: E402

DEMO_PASSWORD = "EduDemo2026!"


def build(seed_folder: Path) -> Path:
    seed_folder = seed_folder.resolve()
    database = seed_folder / "edunexus-demo.db"
    uploads = seed_folder / "uploads"
    if database.exists():
        raise SystemExit(f"{database} already exists. Choose an empty folder so no existing file is replaced.")
    uploads.mkdir(parents=True, exist_ok=True)
    settings = Settings(
        database_url=f"sqlite:///{database.as_posix()}",
        jwt_secret="build-only-0123456789abcdef0123456789abcdef",
        seed_on_start=False,
        seed_demo_accounts=False,
        upload_dir=uploads,
        cors_origins=["http://127.0.0.1:8001"],
        app_url="http://127.0.0.1:8001",
    )
    factory = build_session_factory(settings)
    engine = factory.kw["bind"]
    Base.metadata.create_all(engine)
    add_missing_columns(engine)
    with factory() as db:
        seed_database(db, settings)
        seed_demo_students(db)
        seed_cse_curriculum(db, settings, password=DEMO_PASSWORD, seed_resources=True)
        users = db.scalar(select(func.count()).select_from(User))
        courses = db.scalar(select(func.count()).select_from(Course))
        enrolments = db.scalar(select(func.count()).select_from(Enrollment))
    engine.dispose()
    print(f"Demo database: {users} accounts, {courses} subject sections, {enrolments} enrolments -> {database}")
    return database


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("Usage: build_demo_database.py <empty-folder>")
    build(Path(sys.argv[1]))
