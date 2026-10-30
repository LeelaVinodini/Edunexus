from __future__ import annotations

from datetime import date
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import Settings
from app.deps import get_db, get_settings, require_roles
from app.models import Chapter, Course, Document, Enrollment, Notification, NotificationRead, PreviousPaper, StudentProfile, Submission, StudyTask, User, new_id
from app.services.documents import extension_of, read_upload_limited, safe_filename
from app.services.papers import list_papers
from app.services.storage import resolve_storage_path
from app.services.academic import (
    attendance_summary,
    enrolled_course_ids,
    mark_announcement_read,
    marks_summary,
    planner_suggestions,
    student_accessible_course_ids,
    student_analytics,
    student_announcements,
    student_assignments,
    student_can_access_course,
    student_current_semester,
    student_dashboard,
    student_profile,
    student_subjects,
    study_tasks,
    submit_assignment,
    timetable_for,
)

router = APIRouter(prefix="/api/student", tags=["student"])


class SubmissionBody(BaseModel):
    body: str = Field(min_length=1, max_length=8000)


SUBMISSION_EXTENSIONS = {"pdf", "docx", "txt"}
SUBMISSION_MEDIA_TYPES = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "txt": "text/plain",
}


def _submission_media_type(filename: str, stored: str | None) -> str:
    return SUBMISSION_MEDIA_TYPES.get(extension_of(filename), stored or "application/octet-stream")


def _discard_replaced(stored: str | None, upload_dir: Path) -> None:
    path = resolve_storage_path(upload_dir, stored)
    if path is not None and path.is_file():
        path.unlink()


class TaskBody(BaseModel):
    title: str = Field(min_length=1, max_length=160)
    courseId: str | None = None
    studyDate: date


class TaskUpdate(BaseModel):
    done: bool


def _student(user: User = Depends(require_roles("student"))) -> User:
    return user


@router.get("/dashboard")
def dashboard(user: User = Depends(_student), db: Session = Depends(get_db)):
    return student_dashboard(db, user.id)


@router.get("/notifications")
def student_notifications(user: User = Depends(_student), db: Session = Depends(get_db)):
    rows = db.scalars(select(Notification).where(Notification.recipient_user_id == user.id).order_by(Notification.created_at.desc()).limit(100)).all()
    read_ids = set(db.scalars(select(NotificationRead.notification_id).where(NotificationRead.user_id == user.id)).all())
    return {"items": [{"id": row.id, "kind": row.kind, "title": row.title, "body": row.body, "createdAt": row.created_at.isoformat(), "read": row.id in read_ids} for row in rows], "unreadCount": sum(1 for row in rows if row.id not in read_ids)}


@router.post("/notifications/{notification_id}/read")
def mark_student_notification_read(notification_id: str, user: User = Depends(_student), db: Session = Depends(get_db)):
    row = db.get(Notification, notification_id)
    if row is None or row.recipient_user_id != user.id:
        raise HTTPException(status_code=404, detail="Notification not found.")
    if db.scalar(select(NotificationRead.id).where(NotificationRead.notification_id == row.id, NotificationRead.user_id == user.id)) is None:
        db.add(NotificationRead(notification_id=row.id, user_id=user.id))
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
    return {"status": "read"}


@router.get("/subjects")
def subjects(user: User = Depends(_student), db: Session = Depends(get_db)):
    rows = []
    for subject in student_subjects(db, user.id):
        documents = db.query(Document).filter(Document.course_id == subject["id"], Document.status == "ready").all()
        subject = {**subject, "resources": [{"id": item.id, "title": item.filename, "kind": item.kind, "status": item.status} for item in documents]}
        rows.append(subject)
    return rows


def _current_semester(db: Session, student_id: str) -> int:
    profile = db.get(StudentProfile, student_id)
    return min(max(profile.semester if profile else 1, 1), 14)


@router.get("/materials/semesters")
def material_semesters(user: User = Depends(_student), db: Session = Depends(get_db)):
    current = _current_semester(db, user.id)
    profile = db.get(StudentProfile, user.id)
    return {"currentSemester": current, "currentSection": profile.section if profile else None, "semesters": [{"number": number, "label": f"Semester {number}", "current": number == current} for number in range(1, current + 1)]}


@router.get("/materials/semesters/{semester}/subjects")
def material_subjects(semester: int, user: User = Depends(_student), db: Session = Depends(get_db)):
    current = _current_semester(db, user.id)
    if semester < 1 or semester > current:
        raise HTTPException(status_code=404, detail="Semester not available.")
    accessible = set(student_accessible_course_ids(db, user.id, semester_limit=semester))
    courses = db.scalars(select(Course).where(Course.id.in_(accessible), Course.semester == semester).order_by(Course.code)).all() if accessible else []
    return [{"id": course.id, "code": course.code, "name": course.name, "semester": course.semester, "academicYear": course.academic_year, "section": course.section, "chapterCount": db.query(Chapter).filter(Chapter.course_id == course.id).count()} for course in courses]


@router.get("/materials/subjects/{course_id}/chapters")
def material_chapters(course_id: str, user: User = Depends(_student), db: Session = Depends(get_db)):
    if not student_can_access_course(db, user.id, course_id):
        raise HTTPException(status_code=404, detail="Subject not found.")
    chapters = db.scalars(select(Chapter).where(Chapter.course_id == course_id).order_by(Chapter.ordinal)).all()
    visible_statuses = ("ready", "failed")
    rows = [{"id": item.id, "title": item.title, "ordinal": item.ordinal, "syllabusCode": item.syllabus_code, "resourceCount": db.query(Document).filter(Document.chapter_id == item.id, Document.status.in_(visible_statuses)).count()} for item in chapters]
    unassigned = db.query(Document).filter(Document.course_id == course_id, Document.chapter_id.is_(None), Document.status.in_(visible_statuses)).count()
    if unassigned:
        rows.append({"id": "unassigned", "title": "General materials", "ordinal": 999, "syllabusCode": None, "resourceCount": unassigned})
    return rows


@router.get("/materials/subjects/{course_id}/chapters/{chapter_id}")
def chapter_materials(course_id: str, chapter_id: str, user: User = Depends(_student), db: Session = Depends(get_db)):
    if not student_can_access_course(db, user.id, course_id):
        raise HTTPException(status_code=404, detail="Study materials not found.")
    course = db.get(Course, course_id)
    if chapter_id == "unassigned":
        chapter = {"id": "unassigned", "title": "General materials", "ordinal": 999}
        query = select(Document).where(Document.course_id == course_id, Document.chapter_id.is_(None), Document.status.in_(("ready", "failed")))
    else:
        row = db.get(Chapter, chapter_id)
        if row is None or row.course_id != course_id:
            raise HTTPException(status_code=404, detail="Study materials not found.")
        chapter = {"id": row.id, "title": row.title, "ordinal": row.ordinal}
        query = select(Document).where(Document.course_id == course_id, Document.chapter_id == row.id, Document.status.in_(("ready", "failed")))
    resources = db.scalars(query.order_by(Document.created_at.desc())).all()
    return {
        "subject": {"id": course.id, "code": course.code, "name": course.name, "semester": course.semester, "academicYear": course.academic_year, "section": course.section},
        "chapter": chapter,
        "resources": [{"id": item.id, "title": item.title or item.filename, "filename": item.filename, "description": item.description, "type": item.kind, "contentType": item.content_type, "sizeBytes": item.size_bytes, "status": item.status, "processingError": item.error, "academicYear": course.academic_year, "section": course.section, "semester": course.semester, "academicTerm": course.term, "createdAt": item.created_at.isoformat()} for item in resources],
    }


@router.get("/attendance")
def attendance(semester: int | None = None, user: User = Depends(_student), db: Session = Depends(get_db)):
    current = _current_semester(db, user.id)
    if semester is not None and (semester < 1 or semester > current):
        raise HTTPException(status_code=404, detail="Semester not available.")
    return attendance_summary(db, user.id, semester=semester)


@router.get("/marks")
def marks(semester: int | None = None, user: User = Depends(_student), db: Session = Depends(get_db)):
    current = _current_semester(db, user.id)
    if semester is not None and (semester < 1 or semester > current):
        raise HTTPException(status_code=404, detail="Semester not available.")
    return marks_summary(db, user.id, semester=semester)


@router.get("/timetable")
def timetable(user: User = Depends(_student), db: Session = Depends(get_db)):
    return timetable_for(db, user.id)


@router.get("/assignments")
def assignments(user: User = Depends(_student), db: Session = Depends(get_db)):
    return student_assignments(db, user.id)


@router.post("/assignments/{assignment_id}/submissions")
def submit(assignment_id: str, body: SubmissionBody, user: User = Depends(_student), db: Session = Depends(get_db)):
    submission_id, message, _ = submit_assignment(db, user, assignment_id, body.body)
    if submission_id is None:
        raise HTTPException(status_code=400, detail=message)
    return {"id": submission_id, "message": message}


@router.post("/assignments/{assignment_id}/submissions/upload")
async def submit_with_attachment(
    assignment_id: str,
    body: str = Form(default=""),
    upload: UploadFile = File(),
    user: User = Depends(_student),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    filename = safe_filename(upload.filename or "submission")
    extension = extension_of(filename)
    if extension not in SUBMISSION_EXTENSIONS:
        raise HTTPException(status_code=400, detail="Attach a PDF, DOCX, or TXT file.")
    data = await read_upload_limited(upload, settings.max_upload_bytes, "Choose a file up to 10 MB.")
    if not data:
        raise HTTPException(status_code=400, detail="The attached file is empty.")
    folder = Path(settings.upload_dir) / "submissions"
    folder.mkdir(parents=True, exist_ok=True)
    stored_name = f"submissions/{new_id()}.{extension}"
    stored = Path(settings.upload_dir) / stored_name
    stored.write_bytes(data)
    try:
        submission_id, message, replaced = submit_assignment(db, user, assignment_id, body, {
            "filename": filename,
            "contentType": SUBMISSION_MEDIA_TYPES[extension],
            "sizeBytes": len(data),
            "storagePath": stored_name,
        })
    except Exception:
        stored.unlink(missing_ok=True)
        raise
    if submission_id is None:
        stored.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail=message)
    _discard_replaced(replaced, settings.upload_dir)
    return {"id": submission_id, "message": message, "attachmentName": filename}


@router.get("/assignments/{assignment_id}/submissions/attachment")
def download_submission_attachment(assignment_id: str, user: User = Depends(_student), db: Session = Depends(get_db), settings: Settings = Depends(get_settings)):
    submission = db.scalar(select(Submission).where(Submission.assignment_id == assignment_id, Submission.student_id == user.id))
    path = resolve_storage_path(settings.upload_dir, submission.file_storage_path) if submission else None
    if submission is None or path is None or not path.is_file():
        raise HTTPException(status_code=404, detail="No stored file is available for this submission.")
    return FileResponse(path, filename=submission.file_name or path.name, media_type=_submission_media_type(submission.file_name or "", submission.file_content_type))


@router.get("/announcements")
def announcements(user: User = Depends(_student), db: Session = Depends(get_db)):
    return student_announcements(db, user.id)


@router.post("/announcements/{announcement_id}/read")
def read_announcement(announcement_id: str, user: User = Depends(_student), db: Session = Depends(get_db)):
    if not mark_announcement_read(db, user.id, announcement_id):
        raise HTTPException(status_code=404, detail="Announcement not found.")
    return {"status": "read"}


@router.get("/profile")
def profile(user: User = Depends(_student), db: Session = Depends(get_db)):
    return student_profile(db, user)


@router.get("/planner")
def planner(user: User = Depends(_student), db: Session = Depends(get_db)):
    return study_tasks(db, user.id)


@router.post("/planner", status_code=201)
def add_task(body: TaskBody, user: User = Depends(_student), db: Session = Depends(get_db)):
    title = body.title.strip()
    if not title:
        raise HTTPException(status_code=422, detail="Enter a study task.")
    course_id = body.courseId or None
    if course_id and not student_can_access_course(db, user.id, course_id):
        raise HTTPException(status_code=403, detail="You are not enrolled in that course.")
    task = StudyTask(student_id=user.id, course_id=course_id, title=title, study_date=body.studyDate, done=False)
    db.add(task)
    db.commit()
    db.refresh(task)
    return {"id": task.id}


@router.patch("/planner/{task_id}")
def update_task(task_id: str, body: TaskUpdate, user: User = Depends(_student), db: Session = Depends(get_db)):
    task = db.get(StudyTask, task_id)
    if task is None or task.student_id != user.id:
        raise HTTPException(status_code=404, detail="Task not found.")
    task.done = body.done
    db.commit()
    return {"id": task.id, "done": task.done}


@router.get("/planner/suggestions")
def suggestions(user: User = Depends(_student), db: Session = Depends(get_db)):
    return planner_suggestions(db, user.id)


@router.get("/analytics")
def analytics(user: User = Depends(_student), db: Session = Depends(get_db)):
    return student_analytics(db, user.id)


@router.get("/documents/{document_id}/download")
def download(document_id: str, user: User = Depends(_student), db: Session = Depends(get_db), settings: Settings = Depends(get_settings)):
    document = db.get(Document, document_id)
    path = resolve_storage_path(settings.upload_dir, document.storage_path) if document is not None else None
    if document is None or document.status not in {"ready", "failed"} or not student_can_access_course(db, user.id, document.course_id) or path is None or not path.is_file():
        raise HTTPException(status_code=404, detail="Document not found.")
    return FileResponse(path, filename=document.filename, media_type=document.content_type)


@router.get("/question-papers")
def question_papers(
    semester: int | None = None,
    courseId: str | None = None,
    examYear: int | None = None,
    paperType: str | None = None,
    user: User = Depends(_student),
    db: Session = Depends(get_db),
):
    """Previous-year papers for subjects the student is enrolled in, with optional filters."""
    ids = student_accessible_course_ids(db, user.id)
    if courseId and courseId not in ids:
        return {"items": []}
    items = list_papers(db, ids, semester=semester, course_id=courseId, exam_year=examYear, paper_type=paperType)
    return {"items": items}


@router.get("/question-papers/{paper_id}/download")
def download_question_paper(paper_id: str, user: User = Depends(_student), db: Session = Depends(get_db), settings: Settings = Depends(get_settings)):
    paper = db.get(PreviousPaper, paper_id)
    path = resolve_storage_path(settings.upload_dir, paper.storage_path) if paper is not None else None
    if paper is None or not student_can_access_course(db, user.id, paper.course_id) or path is None or not path.is_file():
        raise HTTPException(status_code=404, detail="Question paper not found.")
    return FileResponse(path, filename=paper.filename, media_type=paper.content_type)
