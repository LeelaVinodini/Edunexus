from __future__ import annotations

import os
from datetime import date, timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import AttendanceRecord, Chapter, Course, Document, Enrollment, FacultyAllocation, Mark, Notification, RegistrationRequest, StudentProfile, User
from app.security import hash_password, validate_password
from app.services.documents import file_sha256, process_document
from app.services.storage import storage_key
from app.services.student_ids import assign_student_institutional_id

PILOT_SEMESTERS = 7
SUBJECTS_PER_SEMESTER = 3
STUDENTS_PER_SEMESTER = 30
# The three demonstration semesters that carry a fourth, distinct subject.
# Every other semester keeps its original three subjects so existing records and
# allocations are preserved. Semesters 4-6 were chosen because the two-semester
# pair (5, 6) shares academic year 3 and the spare fourth faculty member can cover
# both additions (two distinct subjects in that year), while semester 4 adds only
# one subject in academic year 2. That keeps every faculty member at or below the
# two-distinct-subjects-per-academic-year limit.
FOUR_SUBJECT_SEMESTERS = (4, 5, 6)
FOURTH_SUBJECT_NAME = "Emerging Topics and Practice"
CHAPTER_TITLES = (
    "Foundations and Key Concepts",
    "Models and Core Operations",
    "Design and Analysis",
    "Applications and Case Studies",
    "Review and Practice",
)
SUBJECT_NAMES = ("Computing Foundations", "Applied Methods", "Systems and Practice")
FACULTY_NAMES = ("Fictional Faculty One", "Fictional Faculty Two", "Fictional Faculty Three", "Fictional Faculty Four")


def seed_academic_pilot(db: Session, settings: Settings, password: str | None = None) -> dict[str, int | list[int]]:
    """Add a repeatable, fictional seven-semester dataset without resetting existing rows."""
    password = password if password is not None else os.getenv("ACADEMIC_DEMO_PASSWORD", "")
    problem = validate_password(password)
    if problem:
        raise RuntimeError("Set ACADEMIC_DEMO_PASSWORD to a valid development-only password before seeding.")

    faculty_users: list[User] = []
    faculty_count = 0
    for index, name in enumerate(FACULTY_NAMES, start=1):
        email = f"pilot.faculty{index}@faculty.example.test"
        user = db.scalar(select(User).where(User.email == email))
        if user is None:
            user = User(email=email, password_hash=hash_password(password), role="faculty", display_name=name, department="Synthetic Computing", approval_status="approved")
            db.add(user)
            db.flush()
            faculty_count += 1
        if user.role != "faculty":
            raise RuntimeError(f"The synthetic faculty identifier {email} is already used by another role.")
        faculty_users.append(user)

    course_count = student_count = chapter_count = resource_count = attendance_count = mark_count = 0
    courses_by_semester: dict[int, list[Course]] = {}
    for semester in range(1, PILOT_SEMESTERS + 1):
        academic_year = (semester + 1) // 2
        courses: list[Course] = []
        subject_names = SUBJECT_NAMES + ((FOURTH_SUBJECT_NAME,) if semester in FOUR_SUBJECT_SEMESTERS else ())
        for subject_index, subject_name in enumerate(subject_names):
            if subject_index < SUBJECTS_PER_SEMESTER:
                subject_number_in_year = ((semester - 1) % 2) * SUBJECTS_PER_SEMESTER + subject_index
                faculty = faculty_users[subject_number_in_year // 2]
            else:
                # The fourth faculty member has no original allocation, so the added
                # subject for each demonstration semester stays within the yearly limit.
                faculty = faculty_users[-1]
            code = f"PILOT-S{semester:02d}-{subject_index + 1:02d}"
            course = db.scalar(select(Course).where(Course.code == code))
            if course is None:
                course = Course(
                    code=code,
                    name=f"Semester {semester} {subject_name}",
                    term=f"Semester {semester}",
                    room=f"Pilot-{semester:02d}",
                    faculty_id=faculty.id,
                    semester=semester,
                    academic_year=academic_year,
                    section="A",
                    department="Synthetic Computing",
                )
                db.add(course)
                db.flush()
                course_count += 1
            courses.append(course)
            if db.scalar(select(FacultyAllocation.id).where(FacultyAllocation.faculty_id == faculty.id, FacultyAllocation.course_id == course.id, FacultyAllocation.academic_year == academic_year, FacultyAllocation.section == "A")) is None:
                db.add(FacultyAllocation(faculty_id=faculty.id, course_id=course.id, academic_year=academic_year, semester=semester, section="A", academic_term=f"Semester {semester}"))
            chapters: list[Chapter] = []
            for ordinal, chapter_title in enumerate(CHAPTER_TITLES, start=1):
                title = f"{chapter_title} — {subject_name}"
                chapter = db.scalar(select(Chapter).where(Chapter.course_id == course.id, Chapter.ordinal == ordinal))
                if chapter is None:
                    chapter = Chapter(course_id=course.id, title=title, ordinal=ordinal, syllabus_code=f"S{semester}-U{ordinal}")
                    db.add(chapter)
                    db.flush()
                    chapter_count += 1
                chapters.append(chapter)
            for ordinal, chapter in enumerate(chapters, start=1):
                content = (
                    f"{chapter.title}\n\n"
                    f"Synthetic development notes for {course.name}, Semester {semester}, Chapter {ordinal}. "
                    "This fictional material is provided only to exercise EduNexus chapter retrieval and resource navigation. "
                    f"Core topic: {subject_name.lower()} concepts, terminology, examples, and practice for unit {ordinal}.\n"
                )
                resource_count += _seed_resource(db, settings, course, faculty, chapter, content, "study_notes", f"unit-{ordinal}-study-notes.txt")
                if ordinal == 3:
                    bank = f"{chapter.title} — Question bank\n\nSynthetic question: Explain one core concept from this unit.\nSynthetic exercise: Apply the concept to a small example.\n"
                    resource_count += _seed_resource(db, settings, course, faculty, chapter, bank, "question_bank", f"unit-{ordinal}-question-bank.txt")
                if ordinal == 5:
                    paper = f"{chapter.title} — Previous examination practice\n\nSynthetic practice question: Compare two ideas from this chapter and support your answer with an example.\n"
                    resource_count += _seed_resource(db, settings, course, faculty, chapter, paper, "previous_paper", f"unit-{ordinal}-practice-paper.txt")
        courses_by_semester[semester] = courses

    db.commit()

    for semester in courses_by_semester:
        authorized_courses = [course for prior_semester in range(1, semester + 1) for course in courses_by_semester[prior_semester]]
        for number in range(1, STUDENTS_PER_SEMESTER + 1):
            email = f"pilot.s{semester:02d}.{number:02d}@students.example.test"
            student = db.scalar(select(User).where(User.email == email))
            if student is None:
                student = User(email=email, password_hash=hash_password(password), role="student", display_name=f"Fictional Student S{semester:02d}-{number:02d}", approval_status="approved")
                db.add(student)
                db.flush()
                profile = StudentProfile(
                    user_id=student.id,
                    program="Synthetic B.Tech Computing",
                    term=f"Semester {semester}",
                    roll_number=f"PILOT-S{semester:02d}-{number:03d}",
                    semester=semester,
                    academic_year=(semester + 1) // 2,
                    department="Synthetic Computing",
                    section="A",
                    institutional_email=f"fictional.student.s{semester:02d}.{number:02d}@students.example.test",
                )
                db.add(profile)
                db.flush()
                assign_student_institutional_id(db, student.id)
                student_count += 1
            for course in authorized_courses:
                if db.scalar(select(Enrollment.id).where(Enrollment.student_id == student.id, Enrollment.course_id == course.id)) is None:
                    db.add(Enrollment(student_id=student.id, course_id=course.id))
                for session_offset, present in ((1, True), (2, number % 4 != 0)):
                    session_date = date.today() - timedelta(days=session_offset)
                    record = db.scalar(select(AttendanceRecord.id).where(AttendanceRecord.student_id == student.id, AttendanceRecord.course_id == course.id, AttendanceRecord.session_date == session_date))
                    if record is None:
                        db.add(AttendanceRecord(course_id=course.id, student_id=student.id, session_date=session_date, present=present, status="present" if present else "absent", recorded_by=course.faculty_id))
                        attendance_count += 1
                if db.scalar(select(Mark.id).where(Mark.student_id == student.id, Mark.course_id == course.id, Mark.kind == "internal")) is None:
                    db.add(Mark(course_id=course.id, student_id=student.id, kind="internal", score=12 + number % 9, max_score=20))
                    mark_count += 1
        db.commit()

    registration_count, notification_count = _seed_sample_registration_requests(db, password)

    return {
        "facultyCreated": faculty_count,
        "coursesCreated": course_count,
        "studentsCreated": student_count,
        "chaptersCreated": chapter_count,
        "resourcesCreated": resource_count,
        "attendanceCreated": attendance_count,
        "marksCreated": mark_count,
        "registrationRequestsCreated": registration_count,
        "notificationsCreated": notification_count,
        "studentsPerSemester": STUDENTS_PER_SEMESTER,
        "semesters": PILOT_SEMESTERS,
        "fourSubjectSemesters": list(FOUR_SUBJECT_SEMESTERS),
    }


def _seed_sample_registration_requests(db: Session, password: str) -> tuple[int, int]:
    """Add fictional pending applications so the admin review queue can be exercised."""
    samples = (
        ("pilot.pending1@students.example.test", "Fictional Applicant One", "PILOT-PENDING-001"),
        ("pilot.pending2@students.example.test", "Fictional Applicant Two", "PILOT-PENDING-002"),
    )
    registration_count = notification_count = 0
    for email, name, roll_number in samples:
        if db.scalar(select(User).where(User.email == email)) is not None:
            continue
        user = User(
            email=email,
            password_hash=hash_password(password),
            role="student",
            display_name=name,
            department="Synthetic Computing",
            approval_status="pending",
            is_active=False,
        )
        db.add(user)
        db.flush()
        db.add(StudentProfile(
            user_id=user.id,
            program="Synthetic B.Tech Computing",
            term="Semester 1",
            roll_number=roll_number,
            semester=1,
            academic_year=1,
            department="Synthetic Computing",
            section="A",
            academic_status="PENDING_APPROVAL",
        ))
        db.flush()
        assign_student_institutional_id(db, user.id)
        db.add(RegistrationRequest(
            user_id=user.id,
            status="PENDING_APPROVAL",
            submitted_name=name,
            submitted_email=email,
            submitted_program="Synthetic B.Tech Computing",
            submitted_term="Semester 1",
            submitted_roll_number=roll_number,
        ))
        registration_count += 1
        event_key = f"registration:{user.id}:pending"
        if db.scalar(select(Notification.id).where(Notification.event_key == event_key)) is None:
            db.add(Notification(
                event_key=event_key,
                audience_role="administrator",
                kind="registration_pending",
                title="Student registration awaiting review",
                body=f"{name} submitted a fictional development registration request.",
                related_user_id=user.id,
            ))
            notification_count += 1
        db.commit()
    return registration_count, notification_count


def _seed_resource(db: Session, settings: Settings, course: Course, faculty: User, chapter: Chapter, content: str, kind: str, filename: str) -> int:
    data = content.encode("utf-8")
    digest = file_sha256(data)
    existing = db.scalar(select(Document).where(Document.course_id == course.id, Document.sha256 == digest))
    if existing is not None:
        if existing.chapter_id is None:
            existing.chapter_id = chapter.id
            db.commit()
        return 0
    folder = Path(settings.upload_dir) / "academic-pilot"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{digest}.txt"
    if not path.exists():
        path.write_bytes(data)
    document = Document(
        course_id=course.id,
        uploaded_by=faculty.id,
        filename=filename,
        title=filename.removesuffix(".txt").replace("-", " ").title(),
        description="Synthetic development material; not an official institutional document.",
        chapter_id=chapter.id,
        kind=kind,
        content_type="text/plain",
        size_bytes=len(data),
        storage_path=storage_key(settings.upload_dir, path),
        sha256=digest,
        status="processing",
    )
    db.add(document)
    db.commit()
    db.refresh(document)
    process_document(db, document, settings.upload_dir)
    return 1
