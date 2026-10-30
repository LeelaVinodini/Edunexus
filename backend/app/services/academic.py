from __future__ import annotations

import json
import re
from collections import Counter
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    Announcement,
    AnnouncementRead,
    Assignment,
    AttendanceRecord,
    Chunk,
    ContentDraft,
    Conversation,
    Course,
    Document,
    Enrollment,
    FacultyAllocation,
    Mark,
    Message,
    Quiz,
    QuizAttempt,
    StudentProfile,
    StudyTask,
    Submission,
    TimetableSlot,
    User,
    utcnow,
)
from app.services.embeddings import content_tokens, cosine, embed_text
from app.services.language import LanguageModel

WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
RETRIEVAL_LIMIT = 4
RETRIEVAL_FLOOR = 0.08


def audit(db: Session, actor_id: str | None, action: str, entity: str, entity_id: str, detail: str = "") -> None:
    from app.models import AuditLog

    db.add(AuditLog(actor_id=actor_id, action=action, entity=entity, entity_id=entity_id, detail=detail[:500]))


def user_public(user: User) -> dict:
    return {
        "id": user.id,
        "email": user.email,
        "displayName": user.display_name,
        "role": user.role,
        "mustChangePassword": bool(user.must_change_password),
        "passwordChangeRequired": bool(user.must_change_password),
    }


def enrolled_course_ids(db: Session, student_id: str) -> list[str]:
    return list(db.scalars(select(Enrollment.course_id).where(Enrollment.student_id == student_id)))


def faculty_course_ids(db: Session, faculty_id: str) -> list[str]:
    return sorted(set(db.scalars(select(FacultyAllocation.course_id).where(FacultyAllocation.faculty_id == faculty_id)).all()))


def course_or_none(db: Session, course_id: str) -> Course | None:
    return db.get(Course, course_id)


def student_matches_course_scope(profile: StudentProfile | None, course: Course | None) -> bool:
    if profile is None or course is None:
        return False
    if profile.section and course.section and profile.section.casefold() != course.section.casefold():
        return False
    student_department = (profile.department or "").casefold().strip()
    course_department = (course.department or "").casefold().strip()
    if student_department and course_department and student_department not in course_department and course_department not in student_department:
        return False
    return True


def student_current_semester(db: Session, student_id: str) -> int:
    profile = db.get(StudentProfile, student_id)
    if profile is None:
        return 1
    try:
        value = int(profile.semester or 1)
    except (TypeError, ValueError):
        value = 1
    return min(max(value, 1), 14)


def student_can_access_course(db: Session, student_id: str, course_id: str) -> bool:
    course = db.get(Course, course_id)
    profile = db.get(StudentProfile, student_id)
    if course is None or profile is None:
        return False
    current = student_current_semester(db, student_id)
    course_semester = course.semester if course.semester is not None else 0
    if course_semester < 1 or course_semester > current:
        return False
    enrolled = db.scalar(select(Enrollment.id).where(Enrollment.student_id == student_id, Enrollment.course_id == course_id)) is not None
    if enrolled:
        return True
    if course_semester >= current:
        return False
    return student_matches_course_scope(profile, course)


def student_accessible_course_ids(db: Session, student_id: str, *, semester_limit: int | None = None) -> list[str]:
    profile = db.get(StudentProfile, student_id)
    if profile is None:
        return []
    current = student_current_semester(db, student_id)
    limit = current if semester_limit is None else min(max(int(semester_limit), 1), current)
    result: set[str] = set()
    courses_by_id = {course.id: course for course in db.scalars(select(Course)).all()}
    for course_id in enrolled_course_ids(db, student_id):
        course = courses_by_id.get(course_id)
        if course is None:
            continue
        course_semester = course.semester if course.semester is not None else 0
        if 1 <= course_semester <= limit and student_can_access_course(db, student_id, course_id):
            result.add(course_id)
    for course in courses_by_id.values():
        course_semester = course.semester if course.semester is not None else 0
        if course.id in result:
            continue
        if not (1 <= course_semester <= limit):
            continue
        if course_semester >= current:
            continue
        if not student_matches_course_scope(profile, course):
            continue
        result.add(course.id)
    return sorted(result)


def auto_enroll_student_in_matching_courses(db: Session, student_id: str) -> list[str]:
    profile = db.get(StudentProfile, student_id)
    if profile is None:
        return []
    current = student_current_semester(db, student_id)
    added: list[str] = []
    courses = db.scalars(select(Course).where(Course.semester == current)).all()
    for course in courses:
        if not student_matches_course_scope(profile, course):
            continue
        existing = db.scalar(select(Enrollment.id).where(Enrollment.course_id == course.id, Enrollment.student_id == student_id))
        if existing is not None:
            continue
        db.add(Enrollment(course_id=course.id, student_id=student_id))
        added.append(course.id)
    db.flush()
    return added


def backfill_student_course_assignments(db: Session) -> dict:
    total = 0
    repaired = 0
    unresolved = []
    students = db.scalars(select(User).where(User.role == "student")).all()
    for user in students:
        total += 1
        profile = db.get(StudentProfile, user.id)
        if profile is None:
            unresolved.append({"studentId": user.id, "email": user.email, "reason": "missing profile"})
            continue
        if not profile.section:
            profile.section = "A"
        try:
            semester = int(profile.semester or 1)
        except (TypeError, ValueError):
            semester = 1
        if semester < 1 or semester > 14:
            semester = 1
        profile.semester = semester
        profile.academic_year = (semester + 1) // 2
        if not profile.term:
            profile.term = f"Semester {semester}"
        enrollments = db.scalars(select(Enrollment).where(Enrollment.student_id == user.id)).all()
        had_enrollment = bool(enrollments)
        before_semester = profile.semester
        before_section = profile.section
        # Repair inconsistent academic assignment from existing enrolments.
        if enrollments:
            courses = [db.get(Course, enrollment.course_id) for enrollment in enrollments]
            courses = [course for course in courses if course is not None]
            if courses:
                semesters = [course.semester for course in courses if course.semester is not None]
                if semesters:
                    profile.semester = max(semesters)
                sections = [course.section for course in courses if course.section]
                if sections:
                    profile.section = sections[0]
                departments = [course.department for course in courses if course.department]
                if departments:
                    profile.department = departments[0]
                profile.academic_year = ((profile.semester or 1) + 1) // 2
                profile.term = f"Semester {profile.semester}"
        added = []
        if not had_enrollment:
            added = auto_enroll_student_in_matching_courses(db, user.id)
        if added or enrollments:
            # count repaired if we changed anything? keep simple
            pass
        if added or (enrollments and (profile.semester != before_semester or profile.section != before_section)):
            repaired += 1
        now_has = db.scalar(select(Enrollment.id).where(Enrollment.student_id == user.id)) is not None
        if not now_has:
            unresolved.append({"studentId": user.id, "email": user.email, "reason": "no matching course for semester/section"})
    db.commit()
    assigned_count = sum(1 for user in students if db.scalar(select(Enrollment.id).where(Enrollment.student_id == user.id)) is not None)
    valid_semester_section = 0
    for user in students:
        profile = db.get(StudentProfile, user.id)
        if profile is not None and 1 <= (profile.semester or 0) <= 14 and bool(profile.section):
            valid_semester_section += 1
    return {
        "totalStudents": total,
        "studentsAssignedToCourse": assigned_count,
        "studentsWithValidSemesterAndSection": valid_semester_section,
        "repairedStudents": repaired,
        "unresolved": unresolved,
    }


def faculty_can_access_course(db: Session, faculty_id: str, course_id: str) -> bool:
    return db.scalar(select(FacultyAllocation.id).where(FacultyAllocation.faculty_id == faculty_id, FacultyAllocation.course_id == course_id)) is not None


def _course_map(db: Session, course_ids: list[str]) -> dict[str, Course]:
    if not course_ids:
        return {}
    courses = db.scalars(select(Course).where(Course.id.in_(course_ids))).all()
    return {course.id: course for course in courses}


def _instructor_name(db: Session, faculty_id: str) -> str:
    faculty = db.get(User, faculty_id)
    return faculty.display_name if faculty else "Faculty"


def course_payload(db: Session, course: Course, *, resource_count: int | None = None) -> dict:
    payload = {
        "id": course.id,
        "code": course.code,
        "name": course.name,
        "term": course.term,
        "room": course.room,
        "semester": course.semester,
        "academicYear": course.academic_year,
        "section": course.section,
        "department": course.department,
        "instructor": _instructor_name(db, course.faculty_id),
    }
    if resource_count is not None:
        payload["resourceCount"] = resource_count
    return payload


def student_subjects(db: Session, student_id: str) -> list[dict]:
    ids = enrolled_course_ids(db, student_id)
    courses = _course_map(db, ids)
    results = []
    for course_id in ids:
        course = courses[course_id]
        count = db.query(Document).filter(Document.course_id == course_id, Document.status == "ready").count()
        results.append(course_payload(db, course, resource_count=count))
    return sorted(results, key=lambda item: item["code"])


LOW_ATTENDANCE_THRESHOLD = 75
ATTENDANCE_HISTORY_LIMIT = 200


def attendance_summary(db: Session, student_id: str, semester: int | None = None) -> dict:
    current = student_current_semester(db, student_id)
    records = db.scalars(select(AttendanceRecord).where(AttendanceRecord.student_id == student_id)).all()
    courses = _course_map(db, list({record.course_id for record in records}))
    filtered_records = []
    for record in records:
        course = courses.get(record.course_id)
        if course is None:
            continue
        course_semester = course.semester if course.semester is not None else 0
        if course_semester < 1 or course_semester > current:
            continue
        if not student_can_access_course(db, student_id, record.course_id):
            continue
        if semester is not None and course_semester != int(semester):
            continue
        filtered_records.append(record)
    records = filtered_records
    courses = _course_map(db, list({record.course_id for record in records}))
    grouped: dict[str, dict[str, int]] = {}
    for record in records:
        bucket = grouped.setdefault(record.course_id, {"attended": 0, "held": 0})
        bucket["held"] += 1
        bucket["attended"] += int(attended_record(record))
    subjects = []
    attended = held = 0
    for course_id, bucket in grouped.items():
        attended += bucket["attended"]
        held += bucket["held"]
        rate = round(bucket["attended"] / bucket["held"] * 100) if bucket["held"] else 0
        course = courses[course_id]
        subjects.append({
            "courseId": course_id,
            "subject": course.name,
            "code": course.code,
            "semester": course.semester,
            "section": course.section,
            "attended": bucket["attended"],
            "held": bucket["held"],
            "percentage": rate,
            "low": bool(bucket["held"]) and rate < LOW_ATTENDANCE_THRESHOLD,
        })
    percentage = round(attended / held * 100) if held else 0
    ordered = sorted(records, key=lambda record: (record.session_date, courses[record.course_id].code), reverse=True)
    history = [
        {
            "date": record.session_date.isoformat(),
            "courseId": record.course_id,
            "subject": courses[record.course_id].name,
            "code": courses[record.course_id].code,
            "status": record.status,
            "attended": attended_record(record),
        }
        for record in ordered[:ATTENDANCE_HISTORY_LIMIT]
    ]
    low_subjects = [subject for subject in subjects if subject["low"]]
    return {
        "attended": attended,
        "held": held,
        "percentage": percentage,
        "subjects": sorted(subjects, key=lambda item: item["code"]),
        "history": history,
        "lowAttendance": {
            "threshold": LOW_ATTENDANCE_THRESHOLD,
            "overall": bool(held) and percentage < LOW_ATTENDANCE_THRESHOLD,
            "subjects": [{"courseId": item["courseId"], "subject": item["subject"], "code": item["code"], "percentage": item["percentage"]} for item in low_subjects],
        },
        "methodology": "Percentage is attended sessions divided by held sessions, rounded to the nearest whole percent; present and late both count as attended. "
        f"Subjects below {LOW_ATTENDANCE_THRESHOLD}% carry a descriptive low-attendance warning. No eligibility threshold or prediction is applied.",
    }


def marks_summary(db: Session, student_id: str, semester: int | None = None) -> dict:
    current = student_current_semester(db, student_id)
    marks = db.scalars(select(Mark).where(Mark.student_id == student_id)).all()
    courses = _course_map(db, list({mark.course_id for mark in marks}))
    grouped: dict[str, dict] = {}
    for mark in marks:
        course = courses.get(mark.course_id)
        if course is None:
            continue
        course_semester = course.semester if course.semester is not None else 0
        if course_semester < 1 or course_semester > current:
            continue
        if not student_can_access_course(db, student_id, mark.course_id):
            continue
        if semester is not None and course_semester != int(semester):
            continue
        row = grouped.setdefault(mark.course_id, {"courseId": mark.course_id, "subject": course.name, "code": course.code, "internal": None, "semester": None, "semesterNumber": course_semester})
        row["semesterNumber"] = course_semester
        row[mark.kind] = None if mark.score is None else {"score": mark.score, "maxScore": mark.max_score}
    return {
        "subjects": sorted(grouped.values(), key=lambda item: item["code"]),
        "methodology": "Each subject stores at most one internal assessment and one semester result. Missing results stay unavailable. No grade conversion or prediction is applied.",
    }


def _mean(values: list[int]) -> int | None:
    if not values:
        return None
    return round(sum(values) / len(values))


def student_dashboard(db: Session, student_id: str) -> dict:
    attendance = attendance_summary(db, student_id)
    marks = marks_summary(db, student_id)
    internal = [row["internal"]["score"] for row in marks["subjects"] if row["internal"]]
    semester = [row["semester"]["score"] for row in marks["subjects"] if row["semester"]]
    today = WEEKDAYS[datetime.now().weekday()]
    slots = timetable_for(db, student_id)
    today_slots = [slot for slot in slots if slot["day"] == today]
    assignments = [item for item in student_assignments(db, student_id) if item["submissionStatus"] != "Submitted"][:4]
    announcements = student_announcements(db, student_id)[:3]
    return {
        "attendance": {"attendedSessions": attendance["attended"], "heldSessions": attendance["held"], "percentage": attendance["percentage"]},
        "marks": {"internalMarks": _mean(internal), "semesterMarks": _mean(semester)},
        "subjectCount": len(student_subjects(db, student_id)),
        "timetable": [{"id": slot["id"], "time": slot["time"], "subject": slot["subject"], "room": slot["room"]} for slot in today_slots],
        "assignments": [{"id": item["id"], "title": item["title"], "subject": item["subject"], "dueLabel": item["dueLabel"]} for item in assignments],
        "announcements": [{"id": item["id"], "title": item["title"], "message": item["body"], "audience": item["audience"]} for item in announcements],
        "methodology": {
            "attendance": attendance["methodology"],
            "marks": "Dashboard internal and semester figures are the mean of recorded scores for that assessment type, rounded to the nearest whole number. They are not predicted grades.",
            "timetable": "Today’s timetable uses the server’s local weekday.",
        },
    }


def timetable_for(db: Session, student_id: str) -> list[dict]:
    ids = enrolled_course_ids(db, student_id)
    slots = db.scalars(select(TimetableSlot).where(TimetableSlot.course_id.in_(ids))).all() if ids else []
    courses = _course_map(db, ids)
    order = {name: index for index, name in enumerate(WEEKDAYS)}
    rows = []
    for slot in slots:
        course = courses[slot.course_id]
        rows.append({
            "id": slot.id,
            "day": slot.day,
            "time": f"{slot.start_time}–{slot.end_time}",
            "subject": course.name,
            "room": slot.room or course.room,
            "courseId": course.id,
        })
    return sorted(rows, key=lambda item: (order.get(item["day"], 9), item["time"]))


def _due_label(assignment: Assignment, submission: Submission | None) -> str:
    if submission and submission.feedback:
        return "Feedback available"
    if submission:
        return "Submitted"
    due = assignment.due_at
    if due.tzinfo is None:
        due = due.replace(tzinfo=timezone.utc)
    if due < utcnow():
        return "Past due"
    return f"Due {due.date().isoformat()}"


def student_assignments(db: Session, student_id: str) -> list[dict]:
    ids = student_accessible_course_ids(db, student_id)
    if not ids:
        return []
    assignments = db.scalars(select(Assignment).where(Assignment.course_id.in_(ids), Assignment.status.in_(["published", "closed"]))).all()
    courses = _course_map(db, ids)
    rows = []
    for assignment in assignments:
        submission = db.scalar(select(Submission).where(Submission.assignment_id == assignment.id, Submission.student_id == student_id))
        rows.append(_assignment_payload(assignment, courses[assignment.course_id], submission))
    return sorted(rows, key=lambda item: item["dueAt"])


def _assignment_payload(assignment: Assignment, course: Course, submission: Submission | None) -> dict:
    return {
        "id": assignment.id,
        "courseId": course.id,
        "title": assignment.title,
        "subject": course.name,
        "instructions": assignment.instructions,
        "dueAt": assignment.due_at.isoformat(),
        "dueLabel": _due_label(assignment, submission),
        "status": assignment.status,
        "submissionStatus": "Feedback available" if submission and submission.feedback else "Submitted" if submission else "Not submitted",
        "submissionBody": submission.body if submission else None,
        "submissionAttachmentName": submission.file_name if submission else None,
        "submissionAttachmentSize": submission.file_size_bytes if submission else None,
        "feedback": submission.feedback if submission else None,
    }


def student_announcements(db: Session, student_id: str) -> list[dict]:
    ids = student_accessible_course_ids(db, student_id)
    if not ids:
        return []
    announcements = db.scalars(select(Announcement).where(Announcement.course_id.in_(ids), Announcement.status == "published")).all()
    courses = _course_map(db, ids)
    reads = set(db.scalars(select(AnnouncementRead.announcement_id).where(AnnouncementRead.student_id == student_id)))
    rows = []
    for item in announcements:
        course = courses[item.course_id]
        rows.append({
            "id": item.id,
            "courseId": course.id,
            "title": item.title,
            "body": item.body,
            "category": item.category,
            "publishedAt": item.published_at.isoformat() if item.published_at else None,
            "read": item.id in reads,
            "audience": f"{course.code} · {'Read' if item.id in reads else 'Unread'}",
        })
    return sorted(rows, key=lambda item: item["publishedAt"] or "", reverse=True)


def mark_announcement_read(db: Session, student_id: str, announcement_id: str) -> bool:
    announcement = db.get(Announcement, announcement_id)
    if announcement is None or announcement.status != "published" or not student_can_access_course(db, student_id, announcement.course_id):
        return False
    existing = db.scalar(select(AnnouncementRead).where(AnnouncementRead.announcement_id == announcement_id, AnnouncementRead.student_id == student_id))
    if existing is None:
        def _record() -> None:
            db.add(AnnouncementRead(announcement_id=announcement_id, student_id=student_id))
            db.commit()

        try:
            retry_once_on_conflict(db, _record)
        except IntegrityError:
            db.rollback()
    return True


def submit_assignment(
    db: Session,
    student: User,
    assignment_id: str,
    body: str,
    attachment: dict | None = None,
) -> tuple[str | None, str, str | None]:
    """Save a text and/or file submission.

    `attachment` carries file metadata already stored by the caller:
    {"filename", "contentType", "sizeBytes", "storagePath"}. The third return
    value is the previous stored path when a new file replaced it.
    """
    assignment = db.get(Assignment, assignment_id)
    if assignment is None or assignment.status != "published" or not student_can_access_course(db, student.id, assignment.course_id):
        return None, "This assignment is not available.", None
    due = assignment.due_at if assignment.due_at.tzinfo else assignment.due_at.replace(tzinfo=timezone.utc)
    if due < utcnow():
        return None, "The deadline for this assignment has passed.", None
    text = body.strip()
    if len(text) > 8000:
        return None, "Enter a submission up to 8000 characters.", None
    submission = db.scalar(select(Submission).where(Submission.assignment_id == assignment.id, Submission.student_id == student.id))
    if submission and submission.feedback:
        return None, "This submission already has feedback and can no longer be replaced.", None
    if not text and not attachment and not (submission and submission.file_storage_path):
        return None, "Add a written response or attach a PDF, DOCX, or TXT file.", None

    def _persist() -> tuple[str, str | None]:
        current = db.scalar(select(Submission).where(Submission.assignment_id == assignment.id, Submission.student_id == student.id))
        replaced_path: str | None = None
        if current is None:
            current = Submission(assignment_id=assignment.id, student_id=student.id, body=text)
            db.add(current)
        else:
            current.body = text
            current.submitted_at = utcnow()
        if attachment:
            replaced_path = current.file_storage_path
            current.file_name = str(attachment["filename"])
            current.file_content_type = str(attachment["contentType"])
            current.file_size_bytes = int(attachment["sizeBytes"])
            current.file_storage_path = str(attachment["storagePath"])
        db.commit()
        db.refresh(current)
        return current.id, replaced_path

    submission_id, replaced = retry_once_on_conflict(db, _persist)
    return submission_id, "Submission saved.", replaced


def student_profile(db: Session, user: User) -> dict:
    from app.models import StudentProfile

    profile = db.get(StudentProfile, user.id)
    subjects = student_subjects(db, user.id)
    return {
        "displayName": user.display_name,
        "email": user.email,
        "program": profile.program if profile else None,
        "term": profile.term if profile else None,
        "rollNumber": profile.roll_number if profile else None,
        "institutionalId": profile.institutional_id if profile else None,
        "institutionalEmail": profile.institutional_email if profile else None,
        "department": profile.department if profile else None,
        "academicYear": profile.academic_year if profile else None,
        "semester": profile.semester if profile else None,
        "section": profile.section if profile else None,
        "subjects": [{"code": item["code"], "name": item["name"]} for item in subjects],
        "note": "Profile fields are provisioned by the institution and are read-only in this version.",
    }


def study_tasks(db: Session, student_id: str) -> list[dict]:
    tasks = db.scalars(select(StudyTask).where(StudyTask.student_id == student_id)).all()
    courses = _course_map(db, [task.course_id for task in tasks if task.course_id])
    rows = []
    for task in tasks:
        course = courses.get(task.course_id) if task.course_id else None
        rows.append({"id": task.id, "title": task.title, "courseId": task.course_id, "subject": course.name if course else "General", "studyDate": task.study_date.isoformat(), "done": task.done})
    return sorted(rows, key=lambda item: (item["done"], item["studyDate"]))


def planner_suggestions(db: Session, student_id: str) -> dict:
    """Suggestions come only from due published assignments and the student’s lowest attendance courses."""
    suggestions = []
    upcoming = utcnow() + timedelta(days=14)
    for assignment in student_assignments(db, student_id):
        if assignment["submissionStatus"] != "Not submitted" or assignment["status"] != "published":
            continue
        due = datetime.fromisoformat(assignment["dueAt"])
        if due.tzinfo is None:
            due = due.replace(tzinfo=timezone.utc)
        if due <= upcoming:
            suggestions.append({
                "title": f"Work on {assignment['title']}",
                "courseId": assignment["courseId"],
                "subject": assignment["subject"],
                "reason": "Published assignment due within 14 days and not yet submitted.",
            })
    attendance = attendance_summary(db, student_id)["subjects"]
    if attendance:
        lowest = min(attendance, key=lambda item: item["percentage"])
        if lowest["held"] > 0:
            suggestions.append({
                "title": f"Review missed sessions in {lowest['subject']}",
                "courseId": lowest["courseId"],
                "subject": lowest["subject"],
                "reason": f"Lowest recorded attendance among your courses ({lowest['percentage']}% of held sessions). This is a descriptive sort, not a risk prediction.",
            })
    return {
        "suggestions": suggestions,
        "methodology": "Suggestions are a descriptive list of published assignments due within 14 days that are not yet submitted and, when attendance exists, the course with the lowest attendance percentage. They are drafts until you add them. This is not a risk prediction.",
    }


def quiz_performance(db: Session, student_id: str) -> dict:
    """Quiz marks and attempt statistics restated from stored QuizAttempt records.

    Unattempted quizzes count toward the completion rate only; they are never
    scored as zero. Averages, highest, and latest figures use submitted attempts
    with recorded scores only.
    """
    course_ids = [cid for cid in enrolled_course_ids(db, student_id) if (lambda c: c is not None and (c.semester or 0) <= student_current_semester(db, student_id))(db.get(Course, cid))]
    empty = {
        "available": 0,
        "submitted": 0,
        "completionRate": None,
        "averagePercent": None,
        "highestPercent": None,
        "latest": None,
        "results": [],
        "methodology": "Quiz figures restate stored attempts for quizzes in your enrolled subjects. Unattempted quizzes count toward the completion rate only and are never scored as zero.",
    }
    if not course_ids:
        return empty
    quizzes = db.scalars(select(Quiz).where(Quiz.course_id.in_(course_ids))).all()
    if not quizzes:
        return empty
    courses = _course_map(db, course_ids)
    attempts = {
        attempt.quiz_id: attempt
        for attempt in db.scalars(select(QuizAttempt).where(QuizAttempt.student_id == student_id, QuizAttempt.quiz_id.in_([quiz.id for quiz in quizzes]))).all()
    }
    now = utcnow()
    results = []
    for quiz in quizzes:
        course = courses.get(quiz.course_id)
        attempt = attempts.get(quiz.id)
        due = quiz.due_at if quiz.due_at.tzinfo else quiz.due_at.replace(tzinfo=timezone.utc)
        submitted = attempt is not None and attempt.submitted_at is not None and attempt.score is not None
        if submitted:
            status = "submitted"
        elif attempt is not None:
            status = "in progress"
        elif now > due:
            status = "closed"
        else:
            status = "not started"
        max_score = attempt.max_score if submitted else None
        percent = round(attempt.score / max_score * 100) if submitted and max_score else None
        submitted_at = attempt.submitted_at if submitted else None
        if submitted_at is not None and submitted_at.tzinfo is None:
            submitted_at = submitted_at.replace(tzinfo=timezone.utc)
        results.append({
            "quizId": quiz.id,
            "title": quiz.title,
            "subject": course.name if course else "",
            "code": course.code if course else "",
            "semester": course.semester if course else None,
            "status": status,
            "score": attempt.score if submitted else None,
            "maxScore": max_score,
            "percent": percent,
            "attemptDate": submitted_at.isoformat() if submitted_at else None,
            "dueAt": due.isoformat(),
        })
    submitted_rows = [row for row in results if row["status"] == "submitted"]
    percents = [row["percent"] for row in submitted_rows if row["percent"] is not None]
    latest = max(submitted_rows, key=lambda row: row["attemptDate"]) if submitted_rows else None
    return {
        "available": len(results),
        "submitted": len(submitted_rows),
        "completionRate": round(len(submitted_rows) / len(results) * 100) if results else None,
        "averagePercent": round(sum(percents) / len(percents)) if percents else None,
        "highestPercent": max(percents) if percents else None,
        "latest": latest,
        "results": sorted(results, key=lambda row: row["dueAt"], reverse=True),
        "methodology": empty["methodology"],
    }


def student_analytics(db: Session, student_id: str) -> dict:
    attendance = attendance_summary(db, student_id)
    marks = marks_summary(db, student_id)
    tasks = study_tasks(db, student_id)
    quizzes = quiz_performance(db, student_id)
    return {
        "attendance": attendance,
        "marks": marks,
        "quizzes": quizzes,
        "planner": {"total": len(tasks), "complete": sum(1 for task in tasks if task["done"])},
        "methodology": "Figures restate stored attendance, assessment records, quiz attempts, and planner tasks. No trend, rank, or predicted outcome is calculated.",
    }


def faculty_courses(db: Session, faculty_id: str) -> list[dict]:
    course_ids = faculty_course_ids(db, faculty_id)
    courses = db.scalars(select(Course).where(Course.id.in_(course_ids))).all() if course_ids else []
    rows = []
    for course in courses:
        students = db.query(Enrollment).filter(Enrollment.course_id == course.id).count()
        resources = db.query(Document).filter(Document.course_id == course.id).count()
        payload = course_payload(db, course, resource_count=resources)
        payload["studentCount"] = students
        rows.append(payload)
    return sorted(rows, key=lambda item: item["code"])


def course_students(db: Session, course_id: str) -> list[dict]:
    enrollments = db.scalars(select(Enrollment).where(Enrollment.course_id == course_id)).all()
    rows = []
    for enrollment in enrollments:
        student = db.get(User, enrollment.student_id)
        if student and student.is_active and student.approval_status == "approved":
            profile = db.get(StudentProfile, student.id)
            rows.append({"id": student.id, "displayName": student.display_name, "email": student.email, "institutionalId": profile.institutional_id if profile else None, "rollNumber": profile.roll_number if profile else None, "section": profile.section if profile else None, "semester": profile.semester if profile else None})
    return sorted(rows, key=lambda item: item["displayName"])


def faculty_dashboard(db: Session, faculty_id: str) -> dict:
    courses = faculty_courses(db, faculty_id)
    ids = [course["id"] for course in courses]
    documents = db.scalars(select(Document).where(Document.course_id.in_(ids))).all() if ids else []
    assignments = db.scalars(select(Assignment).where(Assignment.course_id.in_(ids))).all() if ids else []
    ordered_courses = sorted(courses, key=lambda item: (item["academicYear"], item["semester"], item["code"]))
    grouped: dict[int, list[dict]] = {}
    for course in ordered_courses:
        grouped.setdefault(course["academicYear"], []).append(course)
    recent_documents = sorted(documents, key=lambda item: item.created_at, reverse=True)[:5]
    recent_attendance = db.scalars(select(AttendanceRecord).where(AttendanceRecord.course_id.in_(ids)).order_by(AttendanceRecord.session_date.desc()).limit(100)).all() if ids else []
    sessions = {}
    for record in recent_attendance:
        sessions.setdefault((record.course_id, record.session_date.isoformat()), record)
    recent_sessions = []
    for course_id, session_date in list(sessions.keys())[:5]:
        course = db.get(Course, course_id)
        recent_sessions.append({"courseId": course_id, "course": course.name if course else "", "code": course.code if course else "", "date": session_date, "section": course.section if course else ""})
    return {
        "courseCount": len(courses),
        "studentCount": sum(course["studentCount"] for course in courses),
        "documents": {"total": len(documents), "ready": sum(1 for item in documents if item.status == "ready"), "failed": sum(1 for item in documents if item.status == "failed")},
        "assignments": {"draft": sum(1 for item in assignments if item.status == "draft"), "published": sum(1 for item in assignments if item.status == "published")},
        "assignedByAcademicYear": [{"academicYear": year, "courses": items} for year, items in sorted(grouped.items())],
        "recentMaterials": [{"id": item.id, "courseId": item.course_id, "title": item.title or item.filename, "status": item.status, "createdAt": item.created_at.isoformat()} for item in recent_documents],
        "recentAttendanceSessions": recent_sessions,
    }


def faculty_analytics(db: Session, course: Course) -> dict:
    students = course_students(db, course.id)
    attendance_rows = []
    for student in students:
        records = db.scalars(select(AttendanceRecord).where(AttendanceRecord.course_id == course.id, AttendanceRecord.student_id == student["id"])).all()
        held = len(records)
        attended = sum(1 for record in records if attended_record(record))
        attendance_rows.append({**student, "attended": attended, "held": held, "percentage": round(attended / held * 100) if held else None})
    assignments = db.scalars(select(Assignment).where(Assignment.course_id == course.id, Assignment.status.in_(["published", "closed"]))).all()
    assignment_rows = []
    for assignment in assignments:
        count = db.query(Submission).filter(Submission.assignment_id == assignment.id).count()
        assignment_rows.append({"id": assignment.id, "title": assignment.title, "submissions": count, "enrolled": len(students)})
    quizzes = db.scalars(select(Quiz).where(Quiz.course_id == course.id)).all()
    quiz_rows = []
    for quiz in quizzes:
        attempts = db.scalars(select(QuizAttempt).where(QuizAttempt.quiz_id == quiz.id, QuizAttempt.submitted_at.is_not(None))).all()
        percents = [round(attempt.score / attempt.max_score * 100) for attempt in attempts if attempt.score is not None and attempt.max_score]
        quiz_rows.append({
            "id": quiz.id,
            "title": quiz.title,
            "submitted": len(attempts),
            "enrolled": len(students),
            "meanPercent": round(sum(percents) / len(percents)) if percents else None,
            "highestPercent": max(percents) if percents else None,
        })
    recorded = [row["percentage"] for row in attendance_rows if row["percentage"] is not None]
    return {
        "course": course_payload(db, course),
        "attendance": attendance_rows,
        "assignments": assignment_rows,
        "quizzes": quiz_rows,
        "meanAttendance": round(sum(recorded) / len(recorded)) if recorded else None,
        "methodology": "Mean attendance is the unweighted average of each enrolled student’s attended/held percentage. Students with no held sessions are omitted from the mean. Assignment counts are submitted work divided by current enrolment. Quiz figures restate stored submitted attempts; unsubmitted attempts are not scored as zero. No support or risk label is inferred.",
    }


_PAGE_LINE = re.compile(r"^(?:page[ \t]*)?\d{1,4}$", re.IGNORECASE)
_SECTION_LINE = re.compile(r"^(?:section|chapter)\s+\d+\b.*$", re.IGNORECASE)
_CAPS_HEADING_LINE = re.compile(r"^[A-Z][A-Z0-9 &/:,'’\-–—.]{5,79}$")


def _content_lines(text: str) -> list[str]:
    lines = []
    for raw in text.splitlines():
        line = re.sub(r"\s+", " ", raw).strip()
        if not line or _PAGE_LINE.match(line) or _SECTION_LINE.match(line) or _CAPS_HEADING_LINE.match(line):
            continue
        lines.append(line)
    return lines


def clean_retrieved_chunks(chunks: list[dict]) -> list[dict]:
    """Remove extraction furniture, chunk-overlap duplication, and near-duplicate excerpts before context assembly.

    Slides and PDF pages repeat running headers, page numbers, and section furniture, and overlapping chunks repeat the
    same sentences. Cleaning at retrieval time also improves already-indexed documents without re-uploading them.
    """
    staged: list[tuple[dict, list[str]]] = [(chunk, _content_lines(str(chunk.get("text", "")))) for chunk in chunks]
    counts: Counter[str] = Counter()
    for _, lines in staged:
        counts.update({line.casefold() for line in lines})
    cleaned: list[dict] = []
    texts: list[str] = []
    fingerprints: list[str] = []
    for chunk, lines in staged:
        kept = [line for line in lines if counts[line.casefold()] == 1 or len(line) > 80 or line[-1:] in ".!?…"]
        text = " ".join(kept).strip()
        for prior in texts:
            limit = min(len(text), len(prior), 400)
            cut = 0
            for size in range(limit, 59, -1):
                if text[:size].casefold() == prior[-size:].casefold():
                    cut = size
                    break
            if cut:
                text = text[cut:].strip()
                break
        fingerprint = re.sub(r"[^a-z0-9]+", "", text.casefold())
        if len(fingerprint) < 24 or any(fingerprint in prior for prior in fingerprints):
            continue
        cleaned.append({**chunk, "text": text, "excerpt": text[:500]})
        texts.append(text)
        fingerprints.append(fingerprint)
    return cleaned


def retrieve_chunks(db: Session, course_id: str, query: str, *, document_id: str | None = None, chunk_ids: set[str] | None = None) -> list[dict]:
    documents = db.scalars(select(Document).where(Document.course_id == course_id, Document.status == "ready")).all()
    if document_id:
        documents = [document for document in documents if document.id == document_id]
    if not documents:
        return []
    by_id = {document.id: document for document in documents}
    chunks = db.scalars(select(Chunk).where(Chunk.document_id.in_(list(by_id)))).all()
    if chunk_ids is not None:
        chunks = [chunk for chunk in chunks if chunk.id in chunk_ids]
    query_vector = embed_text(query)
    scored: list[tuple[float, Chunk]] = []
    for chunk in chunks:
        score = cosine(query_vector, json.loads(chunk.embedding))
        scored.append((score, chunk))
    scored.sort(key=lambda item: item[0], reverse=True)
    selected = [(score, chunk) for score, chunk in scored if score >= RETRIEVAL_FLOOR][:RETRIEVAL_LIMIT]
    results = []
    for score, chunk in selected:
        document = by_id[chunk.document_id]
        results.append({
            "documentId": document.id,
            "title": document.filename,
            "page": chunk.page,
            "excerpt": chunk.text[:500],
            "score": round(score, 4),
            "text": chunk.text,
        })
    return clean_retrieved_chunks(results)


def citation_payload(chunk: dict) -> dict:
    page = f" · page {chunk['page']}" if chunk["page"] else ""
    return {"documentId": chunk["documentId"], "label": f"{chunk['title']}{page}", "page": chunk["page"], "excerpt": chunk["excerpt"]}


def attach_chapter_to_citations(citations: list[dict], chapter: dict) -> list[dict]:
    title = chapter.get("title") or ""
    rows = []
    for citation in citations:
        label = citation["label"]
        if title and title not in label:
            label = f"{label} · {title}"
        rows.append({**citation, "label": label, "section": title or None})
    return rows


def conversation_history_text(db: Session, conversation_id: str, limit: int = 6) -> str:
    messages = db.scalars(select(Message).where(Message.conversation_id == conversation_id).order_by(Message.created_at)).all()
    lines = []
    for item in list(messages)[-limit:]:
        speaker = "Student" if item.role == "user" else "Tutor"
        lines.append(f"{speaker}: {item.content.strip()[:800]}")
    return "\n".join(lines)


SUPPLEMENTARY_LABEL = "Additional explanation — not covered in your uploaded notes."
TUTOR_POLICY = (
    "Answer the student's actual question directly, in the length the question calls for: a short question gets a short answer, "
    "and only a request for detail gets a structured, comprehensive answer. The numbered course excerpts are context, not a limit. "
    "Explain the answer in your own words: do not copy sentences from the excerpts, do not repeat the documents' headings, "
    "and never return the retrieved text itself as the answer. "
    "If an excerpt genuinely supports a claim, cite it immediately after that claim using only its supplied [n] marker; "
    "never cite an excerpt that does not support the claim, and do not cite excerpts for general knowledge. "
    "If the notes do not cover the question, answer from general knowledge and begin that part with exactly: "
    f"{SUPPLEMENTARY_LABEL} Use this heading once, and only when you add general knowledge. "
    "Never claim to have read material that was not supplied, and do not speculate about what a chapter or section contains from its number or title. "
    "If the question is genuinely ambiguous, ask one concise clarifying question instead of guessing. "
    "For history, specialised facts, and any named person, date, machine, or attribution, state the detail only if you are confident it is accurate. "
    "If you are not confident, explain the idea in general terms, say that uncertain specifics such as names, dates, or attributions should be checked against a reliable source, and do not guess. "
    "Do not invent dates, names, figures, sources, quotations, or page numbers."
)
NO_EXCERPTS_NOTE = "No excerpts from the uploaded notes matched this question. Answer from general knowledge, using the supplementary heading once."
NO_MATCH_REFUSAL = "No approved material in this course matched the question. Nothing was invented to fill the gap."
_SUPPLEMENTARY_HEADING = re.compile(
    r"^[ \t*#_>-]*Additional explanation(?:[ \t]*\([^)\n]*\)|[ \t]*[—–-][ \t]*not covered in your uploaded notes\.?)?[ \t*_:.]*",
    re.IGNORECASE | re.MULTILINE,
)
_CITATION_MARKER = re.compile(r"\[(\d+(?:\s*,\s*\d+)*)\]")


def _expand_grouped_markers(match: re.Match[str]) -> str:
    """Turn compact markers such as [2, 3] into [2][3] so every valid marker can be linked."""
    return "".join(f"[{token.strip()}]" for token in match.group(1).split(","))


def numbered_context(chunks: list[dict]) -> str:
    blocks = []
    for index, chunk in enumerate(chunks, start=1):
        location = f", page {chunk['page']}" if chunk["page"] else ""
        blocks.append(f"[{index}] From {chunk['title']}{location}:\n{chunk['text']}")
    return "\n\n".join(blocks)


def _tidy(text: str) -> str:
    text = re.sub(r"[ \t]{2,}", " ", text)
    return re.sub(r"[ \t]+([.,;:!?])", r"\1", text)


def finalize_answer(raw: str, chunks: list[dict]) -> tuple[str, list[dict]]:
    """Keep citations that point to real excerpts in the grounded part, and strip any citation from the supplementary part.

    Grouped markers like [2, 3] are split first, and each kept citation carries the original marker number so the UI can
    link [n] to the source it actually came from.
    """
    text = _CITATION_MARKER.sub(_expand_grouped_markers, raw.strip())
    heading = _SUPPLEMENTARY_HEADING.search(text)
    grounded_raw = text[: heading.start()] if heading else text
    grounded = _tidy(_CITATION_MARKER.sub(lambda match: match.group(0) if 1 <= int(match.group(1)) <= len(chunks) else "", grounded_raw)).strip()
    used: list[int] = []
    for number in _CITATION_MARKER.findall(grounded):
        index = int(number)
        if index not in used:
            used.append(index)
    if heading:
        body = _tidy(_CITATION_MARKER.sub("", text[heading.end():])).strip()
        message = f"{grounded}\n\n{SUPPLEMENTARY_LABEL}" + (f"\n{body}" if body else "")
        message = message.strip()
    else:
        message = grounded
    return message, [{**citation_payload(chunks[index - 1]), "number": index} for index in used]


RAW_CONTEXT_REFUSAL = (
    "The tutor could not turn the retrieved course text into an answer for this question. "
    "Nothing was invented; try rephrasing the question or ask about one specific point."
)


def _normalized_words(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.casefold()).strip()


def looks_like_raw_context(message: str, chunks: list[dict]) -> bool:
    """True when the model echoed retrieved material instead of answering, so it is never shown as the generated answer."""
    body = _CITATION_MARKER.sub(" ", message)
    body = re.sub(r"\bfrom\b[^:]{0,120}:", " ", body, flags=re.IGNORECASE)
    normalized = _normalized_words(body)
    if len(normalized) < 200:
        return False
    haystacks = [_normalized_words(str(chunk.get("text", ""))) for chunk in chunks]
    haystacks = [hay for hay in haystacks if hay]
    if any(len(hay) >= 200 and hay in normalized for hay in haystacks):
        return True
    return normalized in " ".join(haystacks)


def answer_with_sources(model: LanguageModel, instruction: str, chunks: list[dict]) -> dict:
    if not model.configured:
        if not chunks:
            return {"message": NO_MATCH_REFUSAL, "citations": [], "provider": model.name, "draft": True, "grounded": False}
        context = "\n".join(chunk["text"] for chunk in chunks)
        message = model.complete(instruction, context)
        citations = [{**citation_payload(chunk), "number": position} for position, chunk in enumerate(chunks, start=1)]
        return {"message": message, "citations": citations, "provider": model.name, "draft": True, "grounded": True}
    if not chunks:
        message, citations = finalize_answer(model.complete(f"{instruction}\n{NO_EXCERPTS_NOTE}", "No excerpts matched."), [])
        return {"message": message, "citations": citations, "provider": model.name, "draft": True, "grounded": False}
    raw = model.complete(instruction, numbered_context(chunks))
    if looks_like_raw_context(raw, chunks):
        return {"message": RAW_CONTEXT_REFUSAL, "citations": [], "provider": model.name, "draft": True, "grounded": False}
    message, citations = finalize_answer(raw, chunks)
    return {"message": message, "citations": citations, "provider": model.name, "draft": True, "grounded": bool(citations)}


def save_exchange(db: Session, user_id: str, course_id: str | None, mode: str, prompt: str, result: dict, conversation_id: str | None = None) -> str:
    conversation = db.get(Conversation, conversation_id) if conversation_id else None
    if conversation is None or conversation.user_id != user_id:
        conversation = Conversation(user_id=user_id, course_id=course_id, mode=mode, title=prompt[:80])
        db.add(conversation)
        db.flush()
    db.add(Message(conversation_id=conversation.id, role="user", content=prompt, citations="[]"))
    db.add(Message(conversation_id=conversation.id, role="assistant", content=result["message"], citations=json.dumps(result["citations"])))
    db.commit()
    return conversation.id


def exam_insights(db: Session, course_id: str) -> dict:
    papers = db.scalars(select(Document).where(Document.course_id == course_id, Document.kind == "previous_paper", Document.status == "ready")).all()
    if not papers:
        return {
            "status": "pending_sources",
            "message": "No processed previous papers are available for this course. Topic counts are not estimated without them.",
            "papers": [],
            "topics": [],
            "methodology": "When previous-paper documents exist, the service counts tokens after removing common stopwords. Counts describe those files only and are not appearance probabilities.",
        }
    counter: Counter[str] = Counter()
    paper_rows = []
    for paper in papers:
        chunks = db.scalars(select(Chunk).where(Chunk.document_id == paper.id)).all()
        text = "\n".join(chunk.text for chunk in chunks)
        counter.update(content_tokens(text))
        paper_rows.append({"id": paper.id, "title": paper.filename})
    topics = [{"term": term, "count": count} for term, count in counter.most_common(12)]
    return {
        "status": "ready",
        "message": "Descriptive token counts from the processed previous-paper files in this course.",
        "papers": paper_rows,
        "topics": topics,
        "methodology": "Tokens are lowercase words longer than two characters, with a fixed stopword list removed. The count is how often the token appears across the listed papers. This is not a prediction of future exams.",
    }


def record_content_draft(db: Session, author_id: str, course_id: str, topic: str, kind: str, body: str, provider: str) -> ContentDraft:
    draft = ContentDraft(course_id=course_id, author_id=author_id, topic=topic, kind=kind, body=body, provider=provider)
    db.add(draft)
    db.commit()
    db.refresh(draft)
    return draft


def retry_once_on_conflict(db: Session, action):
    """Run a write once; if a concurrent request inserted the same unique row first, roll back and re-run it.

    The second run re-reads the committed row and updates it, so two faculty members saving the same
    record at the same moment both succeed instead of one receiving a server error.
    """
    try:
        return action()
    except IntegrityError:
        db.rollback()
        return action()


def upsert_mark(db: Session, actor_id: str, course_id: str, student_id: str, kind: str, score: int | None, max_score: int) -> Mark:
    return retry_once_on_conflict(db, lambda: _upsert_mark(db, actor_id, course_id, student_id, kind, score, max_score))


def _upsert_mark(db: Session, actor_id: str, course_id: str, student_id: str, kind: str, score: int | None, max_score: int) -> Mark:
    mark = db.scalar(select(Mark).where(Mark.course_id == course_id, Mark.student_id == student_id, Mark.kind == kind))
    if mark is None:
        mark = Mark(course_id=course_id, student_id=student_id, kind=kind, score=score, max_score=max_score)
        db.add(mark)
    else:
        mark.score = score
        mark.max_score = max_score
    db.flush()
    audit(db, actor_id, "record_mark", "mark", mark.id, f"{kind} {score}/{max_score}")
    db.commit()
    db.refresh(mark)
    return mark


ATTENDANCE_STATUSES = ("present", "absent", "late")


def attended_record(record: AttendanceRecord) -> bool:
    """Present and late both count as attended. Absent does not."""
    return record.status in ("present", "late")


def save_attendance_session(db: Session, actor_id: str, course_id: str, session_date: date, entries: dict[str, str]) -> int:
    """Create or edit one session's records in a single transaction. entries maps student id to a status."""
    return retry_once_on_conflict(db, lambda: _save_attendance_session(db, actor_id, course_id, session_date, entries))


def _save_attendance_session(db: Session, actor_id: str, course_id: str, session_date: date, entries: dict[str, str]) -> int:
    existing = {row.student_id: row for row in db.scalars(select(AttendanceRecord).where(AttendanceRecord.course_id == course_id, AttendanceRecord.session_date == session_date)).all()}
    for student_id, status in entries.items():
        record = existing.get(student_id)
        if record is None:
            record = AttendanceRecord(course_id=course_id, student_id=student_id, session_date=session_date, present=False, status=status)
            db.add(record)
        record.status = status
        record.present = status != "absent"
        record.recorded_by = actor_id
    audit(db, actor_id, "save_attendance", "attendance", course_id, f"{session_date.isoformat()} {len(entries)} records")
    db.commit()
    return len(entries)


def upsert_attendance(db: Session, actor_id: str, course_id: str, student_id: str, session_date: date, present: bool) -> AttendanceRecord:
    return retry_once_on_conflict(db, lambda: _upsert_attendance(db, actor_id, course_id, student_id, session_date, present))


def _upsert_attendance(db: Session, actor_id: str, course_id: str, student_id: str, session_date: date, present: bool) -> AttendanceRecord:
    record = db.scalar(select(AttendanceRecord).where(AttendanceRecord.course_id == course_id, AttendanceRecord.student_id == student_id, AttendanceRecord.session_date == session_date))
    if record is None:
        record = AttendanceRecord(course_id=course_id, student_id=student_id, session_date=session_date, present=present)
        record.recorded_by = actor_id
        db.add(record)
    else:
        record.present = present
        record.status = "present" if present else "absent"
        record.recorded_by = actor_id
    db.flush()
    audit(db, actor_id, "record_attendance", "attendance", record.id, session_date.isoformat())
    db.commit()
    db.refresh(record)
    return record
