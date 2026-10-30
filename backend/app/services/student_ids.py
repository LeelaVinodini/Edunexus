from __future__ import annotations

import secrets
import threading
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import StudentProfile

_ID_LOCK = threading.Lock()


def generate_unique_student_institutional_id(db: Session) -> str:
    """Generate a unique institutional student ID using the established convention."""
    year = datetime.now(timezone.utc).year
    while True:
        candidate = f"ENX{year}-{secrets.token_hex(3).upper()}"
        existing = db.scalar(select(StudentProfile.user_id).where(StudentProfile.institutional_id == candidate))
        if existing is None:
            return candidate


def assign_student_institutional_id(db: Session, user_id: str) -> str | None:
    """Assign a permanent institutional ID to a student profile if it does not already have one."""
    profile = db.get(StudentProfile, user_id)
    if profile is None:
        return None
    if profile.institutional_id:
        return profile.institutional_id

    with _ID_LOCK:
        profile = db.get(StudentProfile, user_id)
        if profile is None or profile.institutional_id:
            return profile.institutional_id if profile else None
        for _ in range(10):
            candidate = generate_unique_student_institutional_id(db)
            profile.institutional_id = candidate
            try:
                db.flush()
                return candidate
            except Exception:  # noqa: BLE001 - retry on unique constraint races
                db.rollback()
                profile = db.get(StudentProfile, user_id)
                if profile is None or profile.institutional_id:
                    return profile.institutional_id if profile else None
    raise RuntimeError("Unable to allocate a unique institutional student ID.")
