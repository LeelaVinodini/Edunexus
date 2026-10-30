"""Previous-year question paper storage and listing.

Deliberately isolated from the AI Tutor document pipeline: question papers are
stored as ``PreviousPaper`` rows with their own files under
``UPLOAD_DIR/question-papers`` and never enter retrieval or chunking.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import Course, PreviousPaper, User, new_id
from app.services.documents import extension_of, safe_filename

PAPER_TYPES: dict[str, str] = {
    "end_semester": "End-semester exam",
    "mid_term": "Mid-term exam",
    "supplementary": "Supplementary exam",
    "model": "Model question paper",
    "university": "University exam",
    "other": "Other paper",
}
MIN_EXAM_YEAR = 1990


def current_exam_year_limit() -> int:
    return datetime.now(timezone.utc).year


def paper_type_label(paper_type: str) -> str:
    return PAPER_TYPES.get(paper_type, paper_type)


def file_format(filename: str) -> str:
    return extension_of(filename).upper() or "FILE"


def validate_paper_metadata(exam_year: int, paper_type: str) -> str | None:
    """Return a human-readable problem, or None when the metadata is valid."""
    if paper_type not in PAPER_TYPES:
        return "Choose one of the supported examination types."
    limit = current_exam_year_limit()
    if not MIN_EXAM_YEAR <= exam_year <= limit:
        return f"Enter an examination year between {MIN_EXAM_YEAR} and {limit}."
    return None


def store_paper_file(settings: Settings, data: bytes, extension: str) -> str:
    """Write paper bytes under UPLOAD_DIR/question-papers and return the portable storage key."""
    folder = Path(settings.upload_dir) / "question-papers"
    folder.mkdir(parents=True, exist_ok=True)
    name = f"{new_id()}.{extension}"
    (folder / name).write_bytes(data)
    return f"question-papers/{name}"


def find_duplicate(db: Session, course_id: str, exam_year: int, paper_type: str, sha256: str) -> PreviousPaper | None:
    return db.scalar(
        select(PreviousPaper).where(
            PreviousPaper.course_id == course_id,
            PreviousPaper.exam_year == exam_year,
            PreviousPaper.paper_type == paper_type,
            PreviousPaper.sha256 == sha256,
        )
    )


def paper_payload(db: Session, paper: PreviousPaper, course: Course | None) -> dict:
    uploader = db.get(User, paper.uploaded_by)
    return {
        "id": paper.id,
        "courseId": paper.course_id,
        "subject": course.name if course else "",
        "code": course.code if course else "",
        "semester": course.semester if course else None,
        "academicYear": course.academic_year if course else None,
        "section": course.section if course else "",
        "examYear": paper.exam_year,
        "paperType": paper.paper_type,
        "paperTypeLabel": paper_type_label(paper.paper_type),
        "title": paper.title,
        "description": paper.description,
        "filename": paper.filename,
        "fileFormat": file_format(paper.filename),
        "contentType": paper.content_type,
        "sizeBytes": paper.size_bytes,
        "uploadedBy": uploader.display_name if uploader else "",
        "createdAt": paper.created_at.isoformat(),
    }


def list_papers(db: Session, course_ids: list[str], *, semester: int | None = None, course_id: str | None = None, exam_year: int | None = None, paper_type: str | None = None) -> list[dict]:
    """List stored papers for the given (already authorized) courses, newest first."""
    if not course_ids:
        return []
    query = select(PreviousPaper).where(PreviousPaper.course_id.in_(course_ids))
    if course_id:
        query = query.where(PreviousPaper.course_id == course_id)
    if exam_year:
        query = query.where(PreviousPaper.exam_year == exam_year)
    if paper_type:
        query = query.where(PreviousPaper.paper_type == paper_type)
    papers = db.scalars(query).all()
    courses = {course.id: course for course in db.scalars(select(Course).where(Course.id.in_(course_ids))).all()}
    rows = []
    for paper in papers:
        course = courses.get(paper.course_id)
        if course is None:
            continue
        if semester is not None and course.semester != semester:
            continue
        rows.append(paper_payload(db, paper, course))
    return sorted(rows, key=lambda row: (row["examYear"], row["code"], row["createdAt"]), reverse=True)


def safe_display_filename(filename: str) -> str:
    return safe_filename(filename)
