#!/usr/bin/env python
"""Idempotent backfill of student course assignments."""

from __future__ import annotations

import json
from pathlib import Path

from app.config import load_settings
from app.database import add_missing_columns, build_session_factory
from app.models import Base
from app.services.academic import backfill_student_course_assignments


def main() -> None:
    settings = load_settings()
    factory = build_session_factory(settings)
    engine = factory.kw["bind"]
    Base.metadata.create_all(engine)
    add_missing_columns(engine)
    with factory() as session:
        report = backfill_student_course_assignments(session)
        print(json.dumps(report, indent=2, default=str))


if __name__ == "__main__":
    main()