from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import load_settings, validate_settings
from app.database import add_missing_columns, build_session_factory
from app.models import Base
from app.services.academic_seed import seed_academic_pilot


def main() -> None:
    settings = load_settings()
    validate_settings(settings)
    settings.upload_dir.mkdir(parents=True, exist_ok=True)
    factory = build_session_factory(settings)
    engine = factory.kw["bind"]
    Base.metadata.create_all(engine)
    add_missing_columns(engine)
    with factory() as db:
        counts = seed_academic_pilot(db, settings)
    print("Academic pilot seed complete:", counts)


if __name__ == "__main__":
    main()
