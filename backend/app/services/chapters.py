from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Chunk, Course, Document

_MARKDOWN = re.compile(r"^#{1,3}\s+(.+?)\s*$")
_CHAPTER = re.compile(r"^chapter\s+\d+\b.*$", re.IGNORECASE)


def catalog_for_courses(db: Session, courses: list[Course]) -> list[dict]:
    rows = []
    for course in sorted(courses, key=lambda item: item.code):
        chapters = []
        documents = db.scalars(select(Document).where(Document.course_id == course.id, Document.status == "ready").order_by(Document.filename)).all()
        for document in documents:
            chunks = db.scalars(select(Chunk).where(Chunk.document_id == document.id).order_by(Chunk.ordinal)).all()
            chapters.extend(_chapters_from_document(document, list(chunks)))
        rows.append({"id": course.id, "code": course.code, "name": course.name, "chapters": [_public(chapter) for chapter in chapters]})
    return rows


def resolve_chapter(db: Session, course_id: str, chapter_id: str) -> dict | None:
    documents = db.scalars(select(Document).where(Document.course_id == course_id, Document.status == "ready")).all()
    for document in documents:
        chunks = db.scalars(select(Chunk).where(Chunk.document_id == document.id).order_by(Chunk.ordinal)).all()
        for chapter in _chapters_from_document(document, list(chunks)):
            if chapter["id"] == chapter_id:
                return chapter
    return None


def _public(chapter: dict) -> dict:
    return {key: chapter[key] for key in ("id", "title", "source", "page", "basis")}


def _chapters_from_document(document: Document, chunks: list[Chunk]) -> list[dict]:
    sections: list[dict] = []
    current: dict | None = None
    for chunk in chunks:
        title = _heading(chunk.text)
        if title and (current is None or title != current["title"]):
            current = {"title": title, "chunks": [], "page": chunk.page, "basis": "heading"}
            sections.append(current)
        if current is None:
            current = {"title": _filename_title(document.filename), "chunks": [], "page": chunk.page, "basis": "document"}
            sections.append(current)
        current["chunks"].append(chunk)
    if not sections:
        return []
    chapters = []
    for index, section in enumerate(sections):
        chapters.append({
            "id": f"{document.id}:{index}",
            "title": section["title"],
            "source": document.filename,
            "page": section["page"],
            "basis": section["basis"],
            "documentId": document.id,
            "chunkIds": {chunk.id for chunk in section["chunks"]},
        })
    return chapters


def _heading(text: str) -> str | None:
    lines = text.splitlines()
    for index, raw in enumerate(lines):
        line = raw.strip()
        if not line:
            continue
        markdown = _MARKDOWN.match(line)
        if markdown:
            return markdown.group(1).strip()[:160]
        if _CHAPTER.match(line):
            return line[:160]
        following_blank = index + 1 < len(lines) and not lines[index + 1].strip()
        if following_blank and _looks_like_title(line):
            return line[:160]
        return None
    return None


def _looks_like_title(line: str) -> bool:
    words = line.split()
    return 3 <= len(line) <= 80 and 1 <= len(words) <= 12 and not line.endswith((".", "?", "!"))


def _filename_title(filename: str) -> str:
    stem = filename.rsplit(".", 1)[0]
    return stem.replace("-", " ").replace("_", " ").strip()[:160] or filename
