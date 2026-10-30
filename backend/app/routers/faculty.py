from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import Settings
from app.deps import get_db, get_settings, require_roles
from app.models import Announcement, Assignment, AttendanceRecord, Chapter, Course, Document, PreviousPaper, Submission, User, utcnow
from app.services.papers import (
    find_duplicate,
    paper_payload,
    list_papers,
    store_paper_file,
    validate_paper_metadata,
)
from app.services.academic import (
    ATTENDANCE_STATUSES,
    audit,
    course_students,
    faculty_analytics,
    faculty_can_access_course,
    faculty_courses,
    faculty_dashboard,
    save_attendance_session,
    student_can_access_course,
    upsert_attendance,
    upsert_mark,
)
from app.services.documents import ALLOWED_EXTENSIONS, extension_of, file_sha256, process_document, read_upload_limited, safe_filename
from app.services.storage import resolve_storage_path

router = APIRouter(prefix="/api/faculty", tags=["faculty"])


class AssignmentBody(BaseModel):
    courseId: str
    title: str = Field(min_length=1, max_length=160)
    instructions: str = Field(default="", max_length=8000)
    dueAt: datetime


class AssignmentPatch(BaseModel):
    title: str | None = Field(default=None, max_length=160)
    instructions: str | None = Field(default=None, max_length=8000)
    dueAt: datetime | None = None


class AnnouncementBody(BaseModel):
    courseId: str
    title: str = Field(min_length=1, max_length=160)
    body: str = Field(min_length=1, max_length=4000)
    category: str = Field(default="Course", max_length=40)


class AnnouncementPatch(BaseModel):
    title: str | None = Field(default=None, max_length=160)
    body: str | None = Field(default=None, max_length=4000)
    category: str | None = Field(default=None, max_length=40)


class FeedbackBody(BaseModel):
    feedback: str = Field(min_length=1, max_length=4000)


class MarkBody(BaseModel):
    studentId: str
    kind: str
    score: int | None = Field(default=None, ge=0, le=1000)
    maxScore: int = Field(ge=1, le=1000)


class AttendanceBody(BaseModel):
    studentId: str
    sessionDate: date
    present: bool


def _faculty(user: User = Depends(require_roles("faculty"))) -> User:
    return user


def _course_for_faculty(db: Session, user: User, course_id: str) -> Course:
    course = db.get(Course, course_id)
    if course is None or not faculty_can_access_course(db, user.id, course.id):
        raise HTTPException(status_code=404, detail="Course not found.")
    return course


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


@router.get("/dashboard")
def dashboard(user: User = Depends(_faculty), db: Session = Depends(get_db)):
    return faculty_dashboard(db, user.id)


@router.get("/courses")
def courses(user: User = Depends(_faculty), db: Session = Depends(get_db)):
    return faculty_courses(db, user.id)


@router.get("/courses/{course_id}/students")
def students(course_id: str, user: User = Depends(_faculty), db: Session = Depends(get_db)):
    _course_for_faculty(db, user, course_id)
    return course_students(db, course_id)


@router.put("/courses/{course_id}/marks")
def record_mark(course_id: str, body: MarkBody, user: User = Depends(_faculty), db: Session = Depends(get_db)):
    _course_for_faculty(db, user, course_id)
    if body.kind not in {"internal", "semester"}:
        raise HTTPException(status_code=422, detail="Assessment type must be internal or semester.")
    if body.score is not None and body.score > body.maxScore:
        raise HTTPException(status_code=422, detail="Score cannot exceed the maximum.")
    if not student_can_access_course(db, body.studentId, course_id):
        raise HTTPException(status_code=404, detail="That student is not enrolled in this course.")
    mark = upsert_mark(db, user.id, course_id, body.studentId, body.kind, body.score, body.maxScore)
    return {"id": mark.id, "status": "saved"}


@router.post("/courses/{course_id}/attendance")
def record_attendance(course_id: str, body: AttendanceBody, user: User = Depends(_faculty), db: Session = Depends(get_db)):
    _course_for_faculty(db, user, course_id)
    if not student_can_access_course(db, body.studentId, course_id):
        raise HTTPException(status_code=404, detail="That student is not enrolled in this course.")
    record = upsert_attendance(db, user.id, course_id, body.studentId, body.sessionDate, body.present)
    return {"id": record.id, "status": "saved"}


class SessionAttendanceEntry(BaseModel):
    studentId: str = Field(min_length=1, max_length=36)
    status: str


class SessionAttendanceBody(BaseModel):
    sessionDate: date
    records: list[SessionAttendanceEntry] = Field(min_length=1, max_length=500)


@router.get("/courses/{course_id}/attendance")
def session_attendance(course_id: str, sessionDate: date, user: User = Depends(_faculty), db: Session = Depends(get_db)):
    _course_for_faculty(db, user, course_id)
    records = {row.student_id: row.status for row in db.scalars(select(AttendanceRecord).where(AttendanceRecord.course_id == course_id, AttendanceRecord.session_date == sessionDate)).all()}
    return {
        "sessionDate": sessionDate.isoformat(),
        "records": [{"studentId": student["id"], "displayName": student["displayName"], "institutionalId": student.get("institutionalId"), "rollNumber": student.get("rollNumber"), "section": student.get("section"), "status": records.get(student["id"])} for student in course_students(db, course_id)],
    }


@router.put("/courses/{course_id}/attendance")
def save_session_attendance(course_id: str, body: SessionAttendanceBody, user: User = Depends(_faculty), db: Session = Depends(get_db)):
    _course_for_faculty(db, user, course_id)
    entries: dict[str, str] = {}
    for entry in body.records:
        if entry.status not in ATTENDANCE_STATUSES:
            raise HTTPException(status_code=422, detail="Attendance must be present, absent, or late.")
        if not student_can_access_course(db, entry.studentId, course_id):
            raise HTTPException(status_code=404, detail="That student is not enrolled in this course.")
        entries[entry.studentId] = entry.status
    saved = save_attendance_session(db, user.id, course_id, body.sessionDate, entries)
    return {"status": "saved", "saved": saved, "sessionDate": body.sessionDate.isoformat()}


@router.get("/analytics")
def analytics(courseId: str, user: User = Depends(_faculty), db: Session = Depends(get_db)):
    course = _course_for_faculty(db, user, courseId)
    return faculty_analytics(db, course)


def _assignment_row(db: Session, assignment: Assignment) -> dict:
    course = db.get(Course, assignment.course_id)
    count = db.query(Submission).filter(Submission.assignment_id == assignment.id).count()
    return {
        "id": assignment.id,
        "courseId": assignment.course_id,
        "subject": course.name if course else "",
        "title": assignment.title,
        "instructions": assignment.instructions,
        "dueAt": assignment.due_at.isoformat(),
        "status": assignment.status,
        "submissionCount": count,
    }


@router.get("/assignments")
def assignments(user: User = Depends(_faculty), db: Session = Depends(get_db)):
    ids = [course["id"] for course in faculty_courses(db, user.id)]
    rows = db.scalars(select(Assignment).where(Assignment.course_id.in_(ids))).all() if ids else []
    return [_assignment_row(db, row) for row in rows]


@router.post("/assignments", status_code=201)
def create_assignment(body: AssignmentBody, user: User = Depends(_faculty), db: Session = Depends(get_db)):
    _course_for_faculty(db, user, body.courseId)
    assignment = Assignment(course_id=body.courseId, title=body.title.strip(), instructions=body.instructions.strip(), due_at=_aware(body.dueAt), status="draft", created_by=user.id)
    db.add(assignment)
    db.flush()
    audit(db, user.id, "create_assignment_draft", "assignment", assignment.id)
    db.commit()
    db.refresh(assignment)
    return _assignment_row(db, assignment)


@router.patch("/assignments/{assignment_id}")
def edit_assignment(assignment_id: str, body: AssignmentPatch, user: User = Depends(_faculty), db: Session = Depends(get_db)):
    assignment = db.get(Assignment, assignment_id)
    if assignment is None or not faculty_can_access_course(db, user.id, assignment.course_id):
        raise HTTPException(status_code=404, detail="Assignment not found.")
    if assignment.status != "draft":
        raise HTTPException(status_code=409, detail="Only drafts can be edited.")
    if body.title is not None:
        assignment.title = body.title.strip()
    if body.instructions is not None:
        assignment.instructions = body.instructions.strip()
    if body.dueAt is not None:
        assignment.due_at = _aware(body.dueAt)
    db.commit()
    db.refresh(assignment)
    return _assignment_row(db, assignment)


@router.post("/assignments/{assignment_id}/publish")
def publish_assignment(assignment_id: str, user: User = Depends(_faculty), db: Session = Depends(get_db)):
    assignment = db.get(Assignment, assignment_id)
    if assignment is None or not faculty_can_access_course(db, user.id, assignment.course_id):
        raise HTTPException(status_code=404, detail="Assignment not found.")
    if assignment.status != "draft":
        raise HTTPException(status_code=409, detail="Only a draft can be published.")
    assignment.status = "published"
    audit(db, user.id, "publish_assignment", "assignment", assignment.id, assignment.title)
    db.commit()
    return {"status": "published"}


@router.post("/assignments/{assignment_id}/close")
def close_assignment(assignment_id: str, user: User = Depends(_faculty), db: Session = Depends(get_db)):
    assignment = db.get(Assignment, assignment_id)
    if assignment is None or not faculty_can_access_course(db, user.id, assignment.course_id):
        raise HTTPException(status_code=404, detail="Assignment not found.")
    if assignment.status != "published":
        raise HTTPException(status_code=409, detail="Only a published assignment can be closed.")
    assignment.status = "closed"
    audit(db, user.id, "close_assignment", "assignment", assignment.id)
    db.commit()
    return {"status": "closed"}


@router.get("/assignments/{assignment_id}/submissions")
def list_submissions(assignment_id: str, user: User = Depends(_faculty), db: Session = Depends(get_db)):
    assignment = db.get(Assignment, assignment_id)
    if assignment is None or not faculty_can_access_course(db, user.id, assignment.course_id):
        raise HTTPException(status_code=404, detail="Assignment not found.")
    rows = db.scalars(select(Submission).where(Submission.assignment_id == assignment.id)).all()
    result = []
    for row in rows:
        student = db.get(User, row.student_id)
        result.append({
            "id": row.id,
            "studentId": row.student_id,
            "studentName": student.display_name if student else "Student",
            "body": row.body,
            "submittedAt": row.submitted_at.isoformat(),
            "feedback": row.feedback,
            "attachmentName": row.file_name,
            "attachmentSizeBytes": row.file_size_bytes,
        })
    return result


@router.get("/assignments/{assignment_id}/submissions/{submission_id}/attachment")
def download_submission_attachment(assignment_id: str, submission_id: str, user: User = Depends(_faculty), db: Session = Depends(get_db), settings: Settings = Depends(get_settings)):
    assignment = db.get(Assignment, assignment_id)
    submission = db.get(Submission, submission_id)
    if assignment is None or submission is None or submission.assignment_id != assignment.id or not faculty_can_access_course(db, user.id, assignment.course_id):
        raise HTTPException(status_code=404, detail="Submission not found.")
    path = resolve_storage_path(settings.upload_dir, submission.file_storage_path)
    if path is None or not path.is_file():
        raise HTTPException(status_code=404, detail="This submission has no stored file.")
    return FileResponse(path, filename=submission.file_name or path.name, media_type=submission.file_content_type or "application/octet-stream")


@router.post("/assignments/{assignment_id}/submissions/{submission_id}/feedback")
def review_submission(assignment_id: str, submission_id: str, body: FeedbackBody, user: User = Depends(_faculty), db: Session = Depends(get_db)):
    assignment = db.get(Assignment, assignment_id)
    submission = db.get(Submission, submission_id)
    if assignment is None or submission is None or submission.assignment_id != assignment.id or not faculty_can_access_course(db, user.id, assignment.course_id):
        raise HTTPException(status_code=404, detail="Submission not found.")
    submission.feedback = body.feedback.strip()
    submission.reviewed_at = utcnow()
    audit(db, user.id, "review_submission", "submission", submission.id)
    db.commit()
    return {"status": "saved"}


def _announcement_row(db: Session, item: Announcement) -> dict:
    course = db.get(Course, item.course_id)
    return {
        "id": item.id,
        "courseId": item.course_id,
        "subject": course.name if course else "",
        "title": item.title,
        "body": item.body,
        "category": item.category,
        "status": item.status,
        "publishedAt": item.published_at.isoformat() if item.published_at else None,
    }


@router.get("/announcements")
def announcements(user: User = Depends(_faculty), db: Session = Depends(get_db)):
    ids = [course["id"] for course in faculty_courses(db, user.id)]
    rows = db.scalars(select(Announcement).where(Announcement.course_id.in_(ids))).all() if ids else []
    return [_announcement_row(db, row) for row in rows]


@router.post("/announcements", status_code=201)
def create_announcement(body: AnnouncementBody, user: User = Depends(_faculty), db: Session = Depends(get_db)):
    _course_for_faculty(db, user, body.courseId)
    item = Announcement(course_id=body.courseId, title=body.title.strip(), body=body.body.strip(), category=body.category.strip() or "Course", status="draft", created_by=user.id)
    db.add(item)
    db.flush()
    audit(db, user.id, "create_announcement_draft", "announcement", item.id)
    db.commit()
    db.refresh(item)
    return _announcement_row(db, item)


@router.patch("/announcements/{announcement_id}")
def edit_announcement(announcement_id: str, body: AnnouncementPatch, user: User = Depends(_faculty), db: Session = Depends(get_db)):
    item = db.get(Announcement, announcement_id)
    if item is None or not faculty_can_access_course(db, user.id, item.course_id):
        raise HTTPException(status_code=404, detail="Announcement not found.")
    if item.status != "draft":
        raise HTTPException(status_code=409, detail="Only drafts can be edited.")
    if body.title is not None:
        item.title = body.title.strip()
    if body.body is not None:
        item.body = body.body.strip()
    if body.category is not None:
        item.category = body.category.strip() or item.category
    db.commit()
    return _announcement_row(db, item)


@router.post("/announcements/{announcement_id}/publish")
def publish_announcement(announcement_id: str, user: User = Depends(_faculty), db: Session = Depends(get_db)):
    item = db.get(Announcement, announcement_id)
    if item is None or not faculty_can_access_course(db, user.id, item.course_id):
        raise HTTPException(status_code=404, detail="Announcement not found.")
    if item.status != "draft":
        raise HTTPException(status_code=409, detail="Only a draft can be published.")
    item.status = "published"
    item.published_at = utcnow()
    audit(db, user.id, "publish_announcement", "announcement", item.id, item.title)
    db.commit()
    return {"status": "published"}


def _document_row(document: Document) -> dict:
    return {
        "id": document.id,
        "courseId": document.course_id,
        "filename": document.filename,
        "title": document.title or document.filename,
        "description": document.description,
        "chapterId": document.chapter_id,
        "kind": document.kind,
        "status": document.status,
        "error": document.error,
        "sizeBytes": document.size_bytes,
        "createdAt": document.created_at.isoformat(),
    }


@router.get("/documents")
def documents(courseId: str | None = None, user: User = Depends(_faculty), db: Session = Depends(get_db)):
    ids = [course["id"] for course in faculty_courses(db, user.id)]
    if courseId:
        if courseId not in ids:
            raise HTTPException(status_code=404, detail="Course not found.")
        ids = [courseId]
    rows = db.scalars(select(Document).where(Document.course_id.in_(ids))).all() if ids else []
    return [_document_row(row) for row in rows]


@router.get("/courses/{course_id}/chapters")
def faculty_chapters(course_id: str, user: User = Depends(_faculty), db: Session = Depends(get_db)):
    _course_for_faculty(db, user, course_id)
    rows = db.scalars(select(Chapter).where(Chapter.course_id == course_id).order_by(Chapter.ordinal)).all()
    return [{"id": row.id, "title": row.title, "ordinal": row.ordinal, "syllabusCode": row.syllabus_code} for row in rows]


@router.post("/documents", status_code=201)
async def upload_document(
    courseId: str = Form(),
    kind: str = Form(default="notes"),
    chapterId: str = Form(default=""),
    title: str = Form(default=""),
    description: str = Form(default=""),
    upload: UploadFile = File(),
    user: User = Depends(_faculty),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    _course_for_faculty(db, user, courseId)
    if kind not in {"lecture_notes", "study_notes", "notes", "slides", "question_bank", "previous_paper", "important_questions", "practice", "other"}:
        raise HTTPException(status_code=422, detail="Document kind is not recognized.")
    filename = safe_filename(upload.filename or "upload")
    extension = extension_of(filename)
    if extension not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=400, detail="Upload a PDF, PPTX, DOCX, TXT, or Markdown file.")
    selected_chapter = db.get(Chapter, chapterId) if chapterId else None
    if selected_chapter and selected_chapter.course_id != courseId:
        raise HTTPException(status_code=422, detail="Choose a chapter belonging to the selected subject.")
    if chapterId and selected_chapter is None:
        raise HTTPException(status_code=422, detail="Choose a valid chapter.")
    if selected_chapter is None:
        selected_chapter = db.scalar(select(Chapter).where(Chapter.course_id == courseId, Chapter.title == "General materials"))
        if selected_chapter is None:
            selected_chapter = Chapter(course_id=courseId, title="General materials", ordinal=0)
            db.add(selected_chapter)
            db.flush()
    data = await read_upload_limited(upload, settings.max_upload_bytes, "Choose a non-empty file up to 10 MB.")
    if not data:
        raise HTTPException(status_code=400, detail="Choose a non-empty file up to 10 MB.")
    digest = file_sha256(data)
    existing = db.scalar(select(Document).where(Document.course_id == courseId, Document.sha256 == digest, Document.chapter_id == selected_chapter.id, Document.uploaded_by == user.id))
    if existing and existing.status == "ready":
        return _document_row(existing)
    settings.upload_dir.mkdir(parents=True, exist_ok=True)
    from app.models import new_id

    stored_name = f"{new_id()}.{extension}"
    path = Path(settings.upload_dir) / stored_name
    path.write_bytes(data)
    document = existing or Document(course_id=courseId, uploaded_by=user.id, filename=filename, kind=kind, content_type=upload.content_type or "application/octet-stream", size_bytes=len(data), storage_path=stored_name, sha256=digest, status="pending")
    document.filename = filename
    document.title = title.strip()[:180] or Path(filename).stem[:180]
    document.description = description.strip()[:4000]
    document.kind = kind
    document.chapter_id = selected_chapter.id
    document.storage_path = stored_name
    document.size_bytes = len(data)
    document.status = "processing"
    document.error = None
    db.add(document)
    db.commit()
    db.refresh(document)
    audit(db, user.id, "upload_document", "document", document.id, filename)
    db.commit()
    process_document(db, document, settings.upload_dir)
    return _document_row(document)


@router.get("/documents/{document_id}/download")
def download(document_id: str, user: User = Depends(_faculty), db: Session = Depends(get_db), settings: Settings = Depends(get_settings)):
    document = db.get(Document, document_id)
    if document is None or not faculty_can_access_course(db, user.id, document.course_id):
        raise HTTPException(status_code=404, detail="Document not found.")
    path = resolve_storage_path(settings.upload_dir, document.storage_path)
    if path is None or not path.is_file():
        raise HTTPException(status_code=404, detail="Document not found.")
    return FileResponse(path, filename=document.filename)


@router.delete("/documents/{document_id}")
def delete_document(document_id: str, user: User = Depends(_faculty), db: Session = Depends(get_db), settings: Settings = Depends(get_settings)):
    document = db.get(Document, document_id)
    if document is None or document.uploaded_by != user.id or not faculty_can_access_course(db, user.id, document.course_id):
        raise HTTPException(status_code=404, detail="Document not found.")
    path = resolve_storage_path(settings.upload_dir, document.storage_path)
    audit(db, user.id, "delete_document", "document", document.id, document.filename)
    db.delete(document)
    db.commit()
    if path is not None and path.exists() and "seed-" not in path.name:
        path.unlink()
    return {"status": "deleted"}


@router.post("/documents/{document_id}/reprocess")
def reprocess(document_id: str, user: User = Depends(_faculty), db: Session = Depends(get_db), settings: Settings = Depends(get_settings)):
    document = db.get(Document, document_id)
    if document is None or document.uploaded_by != user.id or not faculty_can_access_course(db, user.id, document.course_id):
        raise HTTPException(status_code=404, detail="Document not found.")
    document.status = "processing"
    db.commit()
    process_document(db, document, settings.upload_dir)
    return _document_row(document)


@router.get("/question-papers")
def faculty_question_papers(courseId: str | None = None, user: User = Depends(_faculty), db: Session = Depends(get_db)):
    ids = [course["id"] for course in faculty_courses(db, user.id)]
    if courseId:
        if courseId not in ids:
            raise HTTPException(status_code=404, detail="Course not found.")
        ids = [courseId]
    return {"items": list_papers(db, ids)}


@router.post("/question-papers", status_code=201)
async def upload_question_paper(
    courseId: str = Form(),
    examYear: str = Form(),
    paperType: str = Form(),
    title: str = Form(default=""),
    description: str = Form(default=""),
    upload: UploadFile = File(),
    user: User = Depends(_faculty),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    course = _course_for_faculty(db, user, courseId)
    try:
        exam_year = int(str(examYear).strip())
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="Enter the examination year as a four-digit number.")
    problem = validate_paper_metadata(exam_year, paperType)
    if problem:
        raise HTTPException(status_code=422, detail=problem)
    filename = safe_filename(upload.filename or "question-paper")
    extension = extension_of(filename)
    if extension not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=400, detail="Upload a PDF, PPTX, DOCX, TXT, or Markdown file.")
    data = await read_upload_limited(upload, settings.max_upload_bytes, "Choose a non-empty file up to 10 MB.")
    if not data:
        raise HTTPException(status_code=400, detail="Choose a non-empty file up to 10 MB.")
    digest = file_sha256(data)
    if find_duplicate(db, courseId, exam_year, paperType, digest):
        raise HTTPException(status_code=409, detail="This paper is already stored for that subject, examination year, and paper type.")
    key = store_paper_file(settings, data, extension)
    paper = PreviousPaper(
        course_id=courseId,
        uploaded_by=user.id,
        title=title.strip()[:180] or Path(filename).stem[:180],
        description=description.strip()[:4000],
        exam_year=exam_year,
        paper_type=paperType,
        filename=filename,
        content_type=upload.content_type or "application/octet-stream",
        size_bytes=len(data),
        storage_path=key,
        sha256=digest,
    )
    db.add(paper)
    db.flush()
    audit(db, user.id, "upload_question_paper", "previous_paper", paper.id, f"{course.code} {exam_year} {paperType}")
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        resolve_storage_path(settings.upload_dir, key).unlink(missing_ok=True)
        raise HTTPException(status_code=409, detail="This paper is already stored for that subject, examination year, and paper type.")
    db.refresh(paper)
    return paper_payload(db, paper, course)


@router.get("/question-papers/{paper_id}/download")
def download_question_paper(paper_id: str, user: User = Depends(_faculty), db: Session = Depends(get_db), settings: Settings = Depends(get_settings)):
    paper = db.get(PreviousPaper, paper_id)
    path = resolve_storage_path(settings.upload_dir, paper.storage_path) if paper is not None else None
    if paper is None or not faculty_can_access_course(db, user.id, paper.course_id) or path is None or not path.is_file():
        raise HTTPException(status_code=404, detail="Question paper not found.")
    return FileResponse(path, filename=paper.filename, media_type=paper.content_type)


@router.delete("/question-papers/{paper_id}")
def delete_question_paper(paper_id: str, user: User = Depends(_faculty), db: Session = Depends(get_db), settings: Settings = Depends(get_settings)):
    paper = db.get(PreviousPaper, paper_id)
    if paper is None or paper.uploaded_by != user.id or not faculty_can_access_course(db, user.id, paper.course_id):
        raise HTTPException(status_code=404, detail="Question paper not found.")
    path = resolve_storage_path(settings.upload_dir, paper.storage_path)
    audit(db, user.id, "delete_question_paper", "previous_paper", paper.id, paper.filename)
    db.delete(paper)
    db.commit()
    try:
        if path is not None and path.is_file():
            path.unlink()
    except OSError:
        pass
    return {"status": "deleted"}
