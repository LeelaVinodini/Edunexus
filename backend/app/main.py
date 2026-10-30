from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.config import Settings, load_settings, validate_settings
from app.database import add_missing_columns, build_session_factory
from app.models import Base
from app.routers.ai import router as ai_router
from app.routers.auth import admin_router, router as auth_router
from app.routers.faculty import router as faculty_router
from app.routers.quizzes import faculty_router as faculty_quiz_router, student_router as student_quiz_router
from app.routers.student import router as student_router
from app.services.language import build_language_model
from app.services.seed import seed_bootstrap_admin, seed_database, seed_demo_students

logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    validate_settings(settings)
    logging.basicConfig(level=getattr(logging, settings.log_level, logging.INFO), format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings.upload_dir.mkdir(parents=True, exist_ok=True)
    if settings.database_url.startswith("sqlite:///"):
        from pathlib import Path

        db_path = Path(settings.database_url.removeprefix("sqlite:///"))
        db_path.parent.mkdir(parents=True, exist_ok=True)
    factory = build_session_factory(settings)
    engine = factory.kw["bind"]
    Base.metadata.create_all(engine)
    add_missing_columns(engine)
    if settings.seed_on_start and settings.seed_demo_accounts:
        db = factory()
        try:
            seed_database(db, settings)
            seed_demo_students(db)
        finally:
            db.close()
        logger.warning(
            "Documented demo accounts are enabled. Use this mode for local development only, "
            "never for a shared or public deployment."
        )
    elif settings.bootstrap_admin_email or settings.bootstrap_admin_password:
        db = factory()
        try:
            seed_bootstrap_admin(db, settings)
        finally:
            db.close()
    elif settings.seed_on_start:
        logger.warning(
            "SEED_ON_START is enabled but SEED_DEMO_ACCOUNTS is false and no bootstrap "
            "administrator is configured, so no accounts were created."
        )

    app = FastAPI(title="EduNexus API", version="0.2.0")
    app.state.settings = settings
    app.state.session_factory = factory
    app.state.language_model = build_language_model(settings)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
    )
    app.include_router(auth_router)
    app.include_router(admin_router)
    app.include_router(student_router)
    app.include_router(faculty_router)
    app.include_router(faculty_quiz_router)
    app.include_router(student_quiz_router)
    app.include_router(ai_router)

    @app.middleware("http")
    async def prevent_api_caching(request: Request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/api"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/api/health")
    def health():
        model = app.state.language_model
        try:
            with factory.kw["bind"].connect() as connection:
                connection.execute(text("SELECT 1"))
        except Exception:
            logger.error("Health check could not reach the database.")
            return JSONResponse(status_code=503, content={"status": "unavailable", "database": "unreachable"})
        return {"status": "ok", "database": "ok", "languageModel": model.name, "languageModelConfigured": model.configured}

    dist = settings.frontend_dist
    if dist is not None:
        from fastapi.responses import FileResponse

        dist_root = Path(dist).resolve()

        @app.get("/{full_path:path}", include_in_schema=False)
        def serve_frontend(full_path: str):
            if full_path == "api" or full_path.startswith("api/"):
                raise HTTPException(status_code=404, detail="Not found.")
            candidate = (dist_root / full_path).resolve() if full_path else dist_root / "index.html"
            if candidate.is_file() and dist_root in candidate.parents:
                return FileResponse(candidate)
            return FileResponse(dist_root / "index.html", headers={"Cache-Control": "no-cache"})

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_: Request, __: RequestValidationError):
        return JSONResponse(status_code=422, content={"detail": "Check the submitted fields and try again."})

    @app.exception_handler(Exception)
    async def unexpected(request: Request, error: Exception):
        if isinstance(error, HTTPException):
            return JSONResponse(status_code=error.status_code, content={"detail": error.detail})
        logger.error(
            "Unhandled %s on %s %s",
            type(error).__name__,
            request.method,
            request.url.path,
        )
        return JSONResponse(status_code=500, content={"detail": "The request could not be completed."})

    return app


_app: FastAPI | None = None


def __getattr__(name: str):
    global _app
    if name != "app":
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    if _app is None:
        _app = create_app()
    return _app
