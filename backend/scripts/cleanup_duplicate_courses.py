#!/usr/bin/env python
"""
Cleanup script to remove duplicate non-sectioned CSE courses (CSE101-CSE106)
that were created in the first seed run before the section suffix fix.
The sectioned courses (CSE101-A, CSE101-B, etc.) are the correct ones.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.database import build_session_factory
from app.models import (
    AttendanceRecord,
    Chapter,
    Course,
    Document,
    Enrollment,
    FacultyAllocation,
    Mark,
)
from app.services.storage import resolve_storage_path


def cleanup_duplicate_courses(db: Session, upload_dir: Path) -> dict:
    """Remove non-sectioned CSE courses that have sectioned counterparts.
    
    The first seed run created base codes (CSE101, CSE201, etc.) without section suffixes.
    The second run created proper sectioned codes (CSE101-A, CSE101-B, etc.).
    This removes the base codes and their data.
    """
    
    # Non-sectioned course codes to remove (all semesters)
    duplicate_codes = [
        'CSE101', 'CSE102', 'CSE103', 'CSE104', 'CSE105', 'CSE106',
        'CSE201', 'CSE202', 'CSE203', 'CSE204', 'CSE205', 'CSE206',
        'CSE301', 'CSE302', 'CSE303', 'CSE304', 'CSE305', 'CSE306',
        'CSE401', 'CSE402', 'CSE403', 'CSE404', 'CSE405', 'CSE406',
    ]
    
    stats = {
        'coursesRemoved': 0,
        'enrollmentsRemoved': 0,
        'allocationsRemoved': 0,
        'attendanceRemoved': 0,
        'marksRemoved': 0,
        'chaptersRemoved': 0,
        'documentsRemoved': 0,
    }
    
    for code in duplicate_codes:
        course = db.scalar(select(Course).where(Course.code == code))
        if not course:
            print(f'{code}: not found, skipping')
            continue
        
        print(f'Cleaning up {code} (id: {course.id[:8]})...')
        
        # Delete enrollments
        enrollments = db.scalars(select(Enrollment).where(Enrollment.course_id == course.id)).all()
        for e in enrollments:
            db.delete(e)
        stats['enrollmentsRemoved'] += len(enrollments)
        
        # Delete faculty allocations
        allocations = db.scalars(select(FacultyAllocation).where(FacultyAllocation.course_id == course.id)).all()
        for a in allocations:
            db.delete(a)
        stats['allocationsRemoved'] += len(allocations)
        
        # Delete attendance records
        attendance = db.scalars(select(AttendanceRecord).where(AttendanceRecord.course_id == course.id)).all()
        for a in attendance:
            db.delete(a)
        stats['attendanceRemoved'] += len(attendance)
        
        # Delete marks
        marks = db.scalars(select(Mark).where(Mark.course_id == course.id)).all()
        for m in marks:
            db.delete(m)
        stats['marksRemoved'] += len(marks)
        
        # Delete documents (and their files)
        documents = db.scalars(select(Document).where(Document.course_id == course.id)).all()
        for doc in documents:
            # Delete file if exists
            file_path = resolve_storage_path(upload_dir, doc.storage_path)
            if file_path is not None and file_path.is_file():
                try:
                    file_path.unlink()
                except Exception:
                    pass
            db.delete(doc)
        stats['documentsRemoved'] += len(documents)
        
        # Delete chapters
        chapters = db.scalars(select(Chapter).where(Chapter.course_id == course.id)).all()
        for c in chapters:
            db.delete(c)
        stats['chaptersRemoved'] += len(chapters)
        
        # Delete the course itself
        db.delete(course)
        stats['coursesRemoved'] += 1
        
        print(f'  Removed: {len(enrollments)} enrollments, {len(allocations)} allocations, {len(attendance)} attendance, {len(marks)} marks, {len(documents)} docs, {len(chapters)} chapters')
    
    db.commit()
    return stats


if __name__ == "__main__":
    from app.config import load_settings
    
    settings = load_settings()
    factory = build_session_factory(settings)
    
    with factory() as session:
        result = cleanup_duplicate_courses(session, settings.upload_dir)
        print("\nCleanup completed:")
        for key, value in result.items():
            print(f"  {key}: {value}")