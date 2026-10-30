from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import (
    Announcement,
    Assignment,
    AttendanceRecord,
    Course,
    Chapter,
    Document,
    Enrollment,
    FacultyAllocation,
    Mark,
    StudentProfile,
    StudyTask,
    TimetableSlot,
    User,
)
from app.security import hash_password, validate_password
from app.services.documents import file_sha256, process_document
from app.services.student_ids import assign_student_institutional_id

logger = logging.getLogger(__name__)

STUDENT_EMAIL = "student@edunexus.test"
STUDENT_TWO_EMAIL = "student.two@edunexus.test"
FACULTY_EMAIL = "faculty@edunexus.test"
ADMIN_EMAIL = "admin@edunexus.test"
STUDENT_PASSWORD = "StudentPass123!"
FACULTY_PASSWORD = "FacultyPass123!"
ADMIN_PASSWORD = "AdminPass123!"

STACK_NOTES = """Stacks and queues

A stack removes the most recent item first, which is called last-in first-out.
The peek operation inspects the top item without removing it.
Function calls and undo history are practical uses of a stack.
A queue removes the earliest item first.
"""

PAPER_NOTES = """Previous paper CS201 sample

Question 1. Explain stack removal order and the peek operation.
Question 2. Compare stack and queue removal.
Question 3. Describe one use of a stack in program execution.
"""


def seed_bootstrap_admin(db: Session, settings: Settings) -> bool:
    """Create one administrator on an empty database from explicit environment values.

    The password is never logged. Remove BOOTSTRAP_ADMIN_PASSWORD from the environment after
    the first successful start and sign-in.
    """
    if db.scalar(select(User.id)) is not None:
        return False
    email = settings.bootstrap_admin_email.strip().lower()
    password = settings.bootstrap_admin_password
    if not email and not password:
        return False
    if not email or not password:
        raise RuntimeError("Set both BOOTSTRAP_ADMIN_EMAIL and BOOTSTRAP_ADMIN_PASSWORD, or neither.")
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email) or len(email) > 255:
        raise RuntimeError("BOOTSTRAP_ADMIN_EMAIL must be a valid email address.")
    problem = validate_password(password)
    if problem:
        raise RuntimeError(f"BOOTSTRAP_ADMIN_PASSWORD is not acceptable: {problem}")
    if password in {STUDENT_PASSWORD, FACULTY_PASSWORD, ADMIN_PASSWORD}:
        raise RuntimeError("BOOTSTRAP_ADMIN_PASSWORD must not use a documented demo password.")
    user = User(
        email=email,
        password_hash=hash_password(password),
        role="administrator",
        display_name="Institution Administrator",
        approval_status="approved",
        is_active=True,
    )
    db.add(user)
    db.flush()
    from app.services.academic import audit

    audit(db, None, "bootstrap_administrator", "user", user.id)
    db.commit()
    logger.warning(
        "Bootstrap administrator created. Change its password, then remove "
        "BOOTSTRAP_ADMIN_PASSWORD from backend/.env."
    )
    return True


def seed_database(db: Session, settings: Settings) -> None:
    if db.scalar(select(User.id)) is not None:
        return
    faculty = User(email=FACULTY_EMAIL, password_hash=hash_password(FACULTY_PASSWORD), role="faculty", display_name="Asha Menon", department="Computer Science")
    student = User(email=STUDENT_EMAIL, password_hash=hash_password(STUDENT_PASSWORD), role="student", display_name="Rohan Iyer")
    student_two = User(email=STUDENT_TWO_EMAIL, password_hash=hash_password(STUDENT_PASSWORD), role="student", display_name="Meera Shah")
    admin = User(email=ADMIN_EMAIL, password_hash=hash_password(ADMIN_PASSWORD), role="administrator", display_name="Institution Admin")
    db.add_all([faculty, student, student_two, admin])
    db.flush()
    db.add(StudentProfile(user_id=student.id, program="B.Tech Computer Science", term="Semester 3", roll_number="CS24001", semester=3, academic_year=2, department="Computer Science", section="A"))
    db.add(StudentProfile(user_id=student_two.id, program="B.Tech Computer Science", term="Semester 3", roll_number="CS24002", semester=3, academic_year=2, department="Computer Science", section="A"))
    db.flush()
    assign_student_institutional_id(db, student.id)
    assign_student_institutional_id(db, student_two.id)
    structures = Course(code="CS201", name="Data Structures", term="Semester 3", room="C-204", faculty_id=faculty.id, semester=3, academic_year=2, section="A", department="Computer Science")
    databases = Course(code="CS203", name="Database Systems", term="Semester 3", room="B-112", faculty_id=faculty.id, semester=3, academic_year=2, section="A", department="Computer Science")
    db.add_all([structures, databases])
    db.flush()
    structure_chapters = [
        Chapter(course_id=structures.id, title=title, ordinal=index)
        for index, title in enumerate(("Foundations and Abstract Data Types", "Stacks and Queues", "Linked Lists", "Trees and Graphs", "Sorting and Searching"), start=1)
    ]
    database_chapters = [
        Chapter(course_id=databases.id, title=title, ordinal=index)
        for index, title in enumerate(("Database Foundations", "Relational Model", "SQL and Queries", "Normalization", "Transactions and Security"), start=1)
    ]
    db.add_all([*structure_chapters, *database_chapters])
    db.flush()
    db.add_all([
        FacultyAllocation(faculty_id=faculty.id, academic_year=2, course_id=structures.id, semester=3, section="A", academic_term="Semester 3"),
        FacultyAllocation(faculty_id=faculty.id, academic_year=2, course_id=databases.id, semester=3, section="A", academic_term="Semester 3"),
    ])
    db.add_all([
        Enrollment(course_id=structures.id, student_id=student.id),
        Enrollment(course_id=databases.id, student_id=student.id),
        Enrollment(course_id=databases.id, student_id=student_two.id),
    ])
    today = date.today()
    for offset, present in enumerate([True, True, False, True]):
        db.add(AttendanceRecord(course_id=structures.id, student_id=student.id, session_date=today - timedelta(days=offset + 1), present=present))
    db.add(AttendanceRecord(course_id=databases.id, student_id=student.id, session_date=today - timedelta(days=1), present=True))
    db.add(AttendanceRecord(course_id=databases.id, student_id=student_two.id, session_date=today - timedelta(days=1), present=False))
    db.add(Mark(course_id=structures.id, student_id=student.id, kind="internal", score=18, max_score=20))
    db.add(Mark(course_id=structures.id, student_id=student.id, kind="semester", score=72, max_score=100))
    db.add(Mark(course_id=databases.id, student_id=student.id, kind="internal", score=16, max_score=20))
    db.add(Mark(course_id=databases.id, student_id=student_two.id, kind="internal", score=11, max_score=20))
    db.add(Mark(course_id=databases.id, student_id=student_two.id, kind="semester", score=61, max_score=100))
    db.add_all([
        TimetableSlot(course_id=structures.id, day="Monday", start_time="09:00", end_time="10:00", room="C-204"),
        TimetableSlot(course_id=databases.id, day="Monday", start_time="10:15", end_time="11:15", room="B-112"),
        TimetableSlot(course_id=structures.id, day="Wednesday", start_time="11:30", end_time="12:30", room="C-204"),
        TimetableSlot(course_id=databases.id, day="Thursday", start_time="13:30", end_time="14:30", room="B-112"),
    ])
    due = datetime.now(timezone.utc) + timedelta(days=7)
    db.add(Assignment(
        course_id=structures.id,
        title="Linked list implementation",
        instructions="Explain insertion and deletion for a singly linked list, using only your own words.",
        due_at=due,
        status="published",
        created_by=faculty.id,
    ))
    db.add(Announcement(
        course_id=structures.id,
        title="Lab room change",
        body="Wednesday's Data Structures lab meets in C-210.",
        category="Course",
        status="published",
        created_by=faculty.id,
        published_at=datetime.now(timezone.utc),
    ))
    db.add(StudyTask(student_id=student.id, course_id=structures.id, title="Review linked lists", study_date=today, done=False))
    db.commit()
    _seed_document(db, settings, structures.id, faculty.id, "stacks-and-queues.txt", "notes", STACK_NOTES, structure_chapters[1].id)
    _seed_document(db, settings, structures.id, faculty.id, "cs201-previous-paper.txt", "previous_paper", PAPER_NOTES, structure_chapters[1].id)


def _seed_document(db: Session, settings: Settings, course_id: str, faculty_id: str, filename: str, kind: str, text: str, chapter_id: str | None = None) -> None:
    settings.upload_dir.mkdir(parents=True, exist_ok=True)
    data = text.encode("utf-8")
    path = settings.upload_dir / f"seed-{filename}"
    path.write_bytes(data)
    document = Document(
        course_id=course_id,
        uploaded_by=faculty_id,
        filename=filename,
        title=Path(filename).stem.replace("-", " ").title(),
        kind=kind,
        chapter_id=chapter_id,
        content_type="text/plain",
        size_bytes=len(data),
        storage_path=path.name,
        sha256=file_sha256(data),
        status="pending",
    )
    db.add(document)
    db.commit()
    db.refresh(document)
    process_document(db, document, settings.upload_dir)


DEMO_DOMAIN = "example.test"
DEMO_SECTION_CODES = ("CS201", "CS203")
DEMO_STUDENTS_PER_SECTION = 20
_DEMO_FIRST_NAMES = [
    "Aarav", "Ananya", "Diya", "Kabir", "Ishita", "Vihaan", "Saanvi", "Reyansh", "Myra", "Advait",
    "Kiara", "Arjun", "Tara", "Ishaan", "Riya", "Dev", "Nisha", "Rahul", "Pooja", "Samir",
]
_DEMO_LAST_NAMES = [
    "Verma", "Nair", "Gupta", "Pillai", "Reddy", "Joshi", "Kulkarni", "Das", "Mehta", "Chatterjee",
    "Bhat", "Sharma", "Iyer", "Rao", "Kapoor", "Desai", "Menon", "Singh", "Patel", "Krishnan",
]


def _semester_number(term: str | None) -> int:
    import re
    match = re.search(r"(?:semester|sem)\s*(\d+)", term or "", re.IGNORECASE)
    return min(max(int(match.group(1)), 1), 14) if match else 1


def seed_demo_students(db: Session) -> dict[str, int]:
    """Add demo students to each demo section. Idempotent: existing demo accounts and enrolments are kept, never reset.

    Demo accounts are approved students with the student role only. Passwords come from DEMO_STUDENT_PASSWORD when set;
    otherwise an unguessable random secret is hashed and discarded, so no password is printed or stored in source.
    """
    import os
    import secrets

    courses = {course.code: course for course in db.scalars(select(Course).where(Course.code.in_(DEMO_SECTION_CODES))).all()}
    password = os.getenv("DEMO_STUDENT_PASSWORD") or secrets.token_urlsafe(32)
    shared_hash: str | None = None
    added: dict[str, int] = {}
    for section_index, code in enumerate(DEMO_SECTION_CODES):
        course = courses.get(code)
        added[code] = 0
        if course is None:
            continue
        for number in range(1, DEMO_STUDENTS_PER_SECTION + 1):
            email = f"demo.{code.lower()}.{number:02d}@{DEMO_DOMAIN}"
            user = db.scalar(select(User).where(User.email == email))
            if user is None:
                if shared_hash is None:
                    shared_hash = hash_password(password)
                first = _DEMO_FIRST_NAMES[(number - 1 + section_index * 7) % len(_DEMO_FIRST_NAMES)]
                last = _DEMO_LAST_NAMES[(number - 1) % len(_DEMO_LAST_NAMES)]
                user = User(email=email, password_hash=shared_hash, role="student", display_name=f"{first} {last}", approval_status="approved", is_active=True)
                db.add(user)
                db.flush()
                db.add(StudentProfile(user_id=user.id, program="B.Tech Computer Science", term=course.term, roll_number=f"{code}-DEMO-{number:02d}", semester=_semester_number(course.term), academic_year=(_semester_number(course.term) + 1) // 2, department="Computer Science", section="A"))
                db.flush()
                assign_student_institutional_id(db, user.id)
                added[code] += 1
            if db.scalar(select(Enrollment.id).where(Enrollment.course_id == course.id, Enrollment.student_id == user.id)) is None:
                db.add(Enrollment(course_id=course.id, student_id=user.id))
        db.flush()
    db.commit()
    return added


if __name__ == "__main__":
    from app.config import load_settings
    from app.database import add_missing_columns, build_session_factory
    from app.models import Base

    settings = load_settings()
    factory = build_session_factory(settings)
    engine = factory.kw["bind"]
    Base.metadata.create_all(engine)
    add_missing_columns(engine)
    with factory() as session:
        print({code: count for code, count in seed_demo_students(session).items()})
