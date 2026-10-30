from __future__ import annotations

from collections.abc import Iterator
import re

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings


def build_session_factory(settings: Settings) -> sessionmaker[Session]:
    if settings.database_url.startswith("sqlite"):
        engine = create_engine(settings.database_url, connect_args={"check_same_thread": False}, future=True)
    else:
        engine = create_engine(
            settings.database_url,
            future=True,
            pool_pre_ping=True,
            pool_recycle=1800,
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_max_overflow,
        )
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False, future=True)


def portable_ddl(engine, ddl: str) -> str:
    """Translate the SQLite-flavoured column DDL used below into the active dialect's syntax."""
    if engine.dialect.name == "sqlite":
        return ddl
    return ddl.replace("DATETIME", "TIMESTAMP WITH TIME ZONE").replace("BOOLEAN NOT NULL DEFAULT 0", "BOOLEAN NOT NULL DEFAULT FALSE")


def add_missing_columns(engine) -> None:
    """Non-destructive migration for columns added after a table was first created.

    Existing users default to 'approved' so accounts created before registration approval keep access.
    """
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    course_semester_added = False
    profile_semester_added = False
    if "users" in tables:
        columns = {column["name"] for column in inspector.get_columns("users")}
        if "approval_status" not in columns:
            with engine.begin() as connection:
                connection.execute(text("ALTER TABLE users ADD COLUMN approval_status VARCHAR(16) NOT NULL DEFAULT 'approved'"))
    if "attendance_records" in tables:
        columns = {column["name"] for column in inspector.get_columns("attendance_records")}
        if "status" not in columns:
            with engine.begin() as connection:
                connection.execute(text("ALTER TABLE attendance_records ADD COLUMN status VARCHAR(16) NOT NULL DEFAULT 'present'"))
                connection.execute(text("UPDATE attendance_records SET status = CASE WHEN present THEN 'present' ELSE 'absent' END"))
    if "submissions" in tables:
        columns = {column["name"] for column in inspector.get_columns("submissions")}
        additions = {
            "file_name": "VARCHAR(255)",
            "file_content_type": "VARCHAR(120)",
            "file_size_bytes": "INTEGER",
            "file_storage_path": "VARCHAR(500)",
        }
        missing = {name: ddl for name, ddl in additions.items() if name not in columns}
        if missing:
            with engine.begin() as connection:
                for name, ddl in missing.items():
                    connection.execute(text(f"ALTER TABLE submissions ADD COLUMN {name} {ddl}"))
    additions = {
        "courses": {
            "semester": "INTEGER NOT NULL DEFAULT 1",
            "academic_year": "INTEGER NOT NULL DEFAULT 1",
            "section": "VARCHAR(40) NOT NULL DEFAULT 'A'",
            "department": "VARCHAR(160) NOT NULL DEFAULT ''",
        },
        "student_profiles": {
            "semester": "INTEGER NOT NULL DEFAULT 1",
            "academic_year": "INTEGER NOT NULL DEFAULT 1",
            "department": "VARCHAR(160) NOT NULL DEFAULT ''",
            "section": "VARCHAR(40) NOT NULL DEFAULT 'A'",
            "institutional_id": "VARCHAR(40)",
            "institutional_email": "VARCHAR(255)",
            "admission_batch": "VARCHAR(40) NOT NULL DEFAULT ''",
            "regulation_year": "INTEGER",
            "academic_status": "VARCHAR(24) NOT NULL DEFAULT 'ACTIVE'",
            "graduation_year": "INTEGER",
            "graduation_date": "DATE",
            "final_program": "VARCHAR(160)",
        },
        "users": {
            "department": "VARCHAR(160) NOT NULL DEFAULT ''",
            "must_change_password": "BOOLEAN NOT NULL DEFAULT 0",
            "password_changed_at": "DATETIME",
            "token_version": "INTEGER NOT NULL DEFAULT 0",
        },
        "registration_requests": {
            "provisioning_email": "VARCHAR(255)",
            "pending_password_hash": "VARCHAR(255)",
            "provisioning_expires_at": "DATETIME",
            "provisioned_at": "DATETIME",
            "credential_status": "VARCHAR(32) NOT NULL DEFAULT 'NOT_PROVISIONED'",
            "credential_error": "VARCHAR(160)",
            "credential_attempts": "INTEGER NOT NULL DEFAULT 0",
            "credential_sent_at": "DATETIME",
            "password_setup_completed_at": "DATETIME",
        },
        "documents": {
            "title": "VARCHAR(180) NOT NULL DEFAULT ''",
            "description": "TEXT NOT NULL DEFAULT ''",
            "chapter_id": "VARCHAR(36) REFERENCES chapters(id) ON DELETE SET NULL",
        },
        "attendance_records": {
            "recorded_by": "VARCHAR(36) REFERENCES users(id)",
        },
        "notifications": {
            "email_outbox_id": "VARCHAR(36) REFERENCES email_outbox(id)",
        },
    }
    for table, columns in additions.items():
        if table not in tables:
            continue
        current = {column["name"] for column in inspect(engine).get_columns(table)}
        missing = {name: ddl for name, ddl in columns.items() if name not in current}
        if missing:
            course_semester_added = course_semester_added or (table == "courses" and "semester" in missing)
            profile_semester_added = profile_semester_added or (table == "student_profiles" and "semester" in missing)
            with engine.begin() as connection:
                for name, ddl in missing.items():
                    connection.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {portable_ddl(engine, ddl)}"))
    if course_semester_added:
        with engine.begin() as connection:
            rows = connection.execute(text("SELECT id, term FROM courses")).all()
            for course_id, term in rows:
                match = re.search(r"(?:semester|sem)\s*(\d+)", str(term or ""), re.IGNORECASE)
                if match:
                    semester = max(1, int(match.group(1)))
                    connection.execute(text("UPDATE courses SET semester=:semester, academic_year=:year WHERE id=:id"), {"semester": semester, "year": (semester + 1) // 2, "id": course_id})
    if profile_semester_added:
        with engine.begin() as connection:
            rows = connection.execute(text("SELECT user_id, term, program FROM student_profiles")).all()
            for user_id, term, program in rows:
                match = re.search(r"(?:semester|sem)\s*(\d+)", str(term or ""), re.IGNORECASE)
                if match:
                    semester = max(1, int(match.group(1)))
                    connection.execute(text("UPDATE student_profiles SET semester=:semester, academic_year=:year, department=:department WHERE user_id=:id"), {"semester": semester, "year": (semester + 1) // 2, "department": str(program or ""), "id": user_id})
    if "documents" in tables:
        with engine.begin() as connection:
            connection.execute(text("UPDATE documents SET title=filename WHERE title='' OR title IS NULL"))
    if "student_profiles" in tables:
        with engine.begin() as connection:
            connection.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ix_student_profiles_institutional_id ON student_profiles (institutional_id)"))
            connection.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ix_student_profiles_institutional_email ON student_profiles (institutional_email)"))
        from app.services.student_ids import assign_student_institutional_id

        factory = sessionmaker(bind=engine, expire_on_commit=False)
        with factory() as db:
            from app.models import StudentProfile

            missing = db.query(StudentProfile).filter((StudentProfile.institutional_id.is_(None)) | (StudentProfile.institutional_id == "")).all()
            for profile in missing:
                assign_student_institutional_id(db, profile.user_id)
            db.commit()

    if "registration_requests" in tables and "student_profiles" in tables:
        from app.models import Notification, RegistrationRequest, StudentProfile, User

        factory = sessionmaker(bind=engine, expire_on_commit=False)
        with factory() as db:
            pending_users = db.query(User).filter(User.role == "student", User.approval_status.in_(("pending", "rejected"))).all()
            for user in pending_users:
                profile = db.get(StudentProfile, user.id)
                if profile is None:
                    continue
                request_status = "PENDING_APPROVAL" if user.approval_status == "pending" else "REJECTED"
                request = db.query(RegistrationRequest).filter_by(user_id=user.id).first()
                if request is None:
                    request = RegistrationRequest(
                        user_id=user.id,
                        status=request_status,
                        submitted_name=user.display_name,
                        submitted_email=user.email,
                        submitted_program=profile.program,
                        submitted_term=profile.term,
                        submitted_roll_number=profile.roll_number,
                    )
                    db.add(request)
                    db.flush()
                profile.academic_status = request_status
                user.is_active = False
                if request_status == "PENDING_APPROVAL":
                    event_key = f"registration:{user.id}:pending"
                    if db.query(Notification.id).filter_by(event_key=event_key).first() is None:
                        db.add(Notification(event_key=event_key, audience_role="administrator", kind="registration_pending", title="Student registration awaiting review", body=f"{user.display_name} submitted a student registration request.", related_user_id=user.id))
            db.commit()

    # Convert the legacy one-faculty-per-course assignment into the allocation table
    # additively; keep the legacy field intact for backwards compatibility.
    if "faculty_allocations" in tables or "chapters" in tables:
        from app.models import Chapter, Document, FacultyAllocation

        factory = sessionmaker(bind=engine, expire_on_commit=False)
        with factory() as db:
            if "faculty_allocations" in tables:
                rows = db.execute(text("SELECT id, faculty_id, academic_year, semester, section, term FROM courses")).all()
                for course_id, faculty_id, academic_year, semester, section, term in rows:
                    exists = db.query(FacultyAllocation.id).filter_by(faculty_id=faculty_id, academic_year=academic_year, course_id=course_id, section=section).first()
                    if not exists:
                        db.add(FacultyAllocation(faculty_id=faculty_id, academic_year=academic_year, course_id=course_id, semester=semester, section=section, academic_term=term or ""))
            if "chapters" in tables:
                course_ids = db.scalars(text("SELECT id FROM courses")).all()
                for course_id in course_ids:
                    general = db.query(Chapter).filter_by(course_id=course_id, title="General materials").first()
                    if general is None and db.query(Document.id).filter_by(course_id=course_id, chapter_id=None).first() is not None:
                        general = Chapter(course_id=course_id, title="General materials", ordinal=0)
                        db.add(general)
                        db.flush()
                    if general is None and db.query(Chapter.id).filter_by(course_id=course_id).first() is None:
                        db.add(Chapter(course_id=course_id, title="General materials", ordinal=0))
                        db.flush()
                        general = db.query(Chapter).filter_by(course_id=course_id, title="General materials").first()
                    if general is not None:
                        db.execute(text("UPDATE documents SET chapter_id=:chapter WHERE course_id=:course AND chapter_id IS NULL"), {"chapter": general.id, "course": course_id})
            db.commit()


def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    db = factory()
    try:
        yield db
    finally:
        db.close()
