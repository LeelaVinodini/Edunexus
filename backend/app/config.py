from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

BACKEND_ROOT = Path(__file__).resolve().parents[1]

INSECURE_JWT_SECRETS = {
    "",
    "dev-only-change-me",
    "replace-with-a-long-random-string",
    "change-me",
    "secret",
}


def _as_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    database_url: str
    jwt_secret: str
    seed_on_start: bool
    upload_dir: Path
    cors_origins: list[str]
    seed_demo_accounts: bool = False
    bootstrap_admin_email: str = ""
    bootstrap_admin_password: str = ""
    max_upload_bytes: int = 10 * 1024 * 1024
    token_hours: int = 12
    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.8-flash"
    gemini_base_url: str = "https://generativelanguage.googleapis.com/v1beta"
    institution_email_domain: str = "students.example.test"
    app_url: str = ""
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    admin_notification_email: str = ""
    smtp_use_tls: bool = True
    app_env: str = "development"
    log_level: str = "INFO"
    db_pool_size: int = 10
    db_max_overflow: int = 20
    frontend_dist: Path | None = None

    @property
    def is_production(self) -> bool:
        return self.app_env.strip().lower() == "production"

    @property
    def language_model_configured(self) -> bool:
        return bool(self.gemini_api_key.strip())

    @property
    def smtp_configured(self) -> bool:
        return bool(self.smtp_host.strip() and self.smtp_from.strip())

    @property
    def email_notifications_configured(self) -> bool:
        return bool(self.smtp_configured and self.admin_notification_email)

    @property
    def institution_email_domain_configured(self) -> bool:
        domain = self.institution_email_domain.strip().lower()
        return bool(domain) and domain not in {"students.example.test", "example.test", "example.com"}


def validate_settings(settings: Settings) -> None:
    """Reject configuration that would make a deployment unsafe by default."""
    secret = settings.jwt_secret.strip()
    if len(secret) < 32 or secret in INSECURE_JWT_SECRETS:
        raise RuntimeError(
            "JWT_SECRET is missing, short, or a known placeholder. Generate a unique value "
            "with: python -c \"import secrets; print(secrets.token_urlsafe(48))\" "
            "and set it in backend/.env before starting the API."
        )
    if any(origin.strip() == "*" for origin in settings.cors_origins):
        raise RuntimeError("CORS_ORIGINS must list explicit frontend origins; '*' is not allowed.")
    if settings.is_production:
        _validate_production(settings)


def _validate_production(settings: Settings) -> None:
    problems: list[str] = []
    if not settings.database_url.startswith("postgresql"):
        problems.append("DATABASE_URL must be a PostgreSQL URL (postgresql+psycopg://...) in production.")
    if settings.seed_demo_accounts:
        problems.append("SEED_DEMO_ACCOUNTS must be false in production.")
    if not settings.app_url.startswith("https://"):
        problems.append("APP_URL must be the public https:// frontend address in production.")
    local_origins = [origin for origin in settings.cors_origins if "localhost" in origin or "127.0.0.1" in origin]
    if local_origins:
        problems.append("CORS_ORIGINS must not contain localhost or 127.0.0.1 in production.")
    if any(not origin.startswith("https://") for origin in settings.cors_origins):
        problems.append("Every CORS_ORIGINS entry must use https:// in production.")
    if settings.token_hours < 1 or settings.token_hours > 24:
        problems.append("TOKEN_HOURS must be between 1 and 24 in production.")
    if problems:
        raise RuntimeError("Production configuration is unsafe: " + " ".join(problems))


def load_settings() -> Settings:
    load_dotenv(BACKEND_ROOT / ".env", override=False)
    upload = Path(os.getenv("UPLOAD_DIR", str(BACKEND_ROOT / "data" / "uploads")))
    if not upload.is_absolute():
        upload = (BACKEND_ROOT / upload).resolve()
    database_url = os.getenv("DATABASE_URL", f"sqlite:///{(BACKEND_ROOT / 'data' / 'edunexus.db').as_posix()}")
    if database_url.startswith("sqlite:///") and not database_url.startswith("sqlite:////"):
        raw = database_url.removeprefix("sqlite:///")
        path = Path(raw)
        if not path.is_absolute():
            database_url = f"sqlite:///{(BACKEND_ROOT / path).resolve().as_posix()}"
    origins = [item.strip() for item in os.getenv("CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173").split(",") if item.strip()]
    return Settings(
        database_url=database_url,
        jwt_secret=(os.getenv("JWT_SECRET") or "").strip(),
        seed_on_start=_as_bool(os.getenv("SEED_ON_START"), False),
        upload_dir=upload,
        cors_origins=origins,
        seed_demo_accounts=_as_bool(os.getenv("SEED_DEMO_ACCOUNTS"), False),
        bootstrap_admin_email=(os.getenv("BOOTSTRAP_ADMIN_EMAIL") or "").strip(),
        bootstrap_admin_password=os.getenv("BOOTSTRAP_ADMIN_PASSWORD") or "",
        gemini_api_key=os.getenv("GEMINI_API_KEY", "").strip(),
        gemini_model=(os.getenv("GEMINI_MODEL") or "").strip() or "gemini-3.8-flash",
        gemini_base_url=((os.getenv("GEMINI_BASE_URL") or "").strip() or "https://generativelanguage.googleapis.com/v1beta").rstrip("/"),
        institution_email_domain=(os.getenv("INSTITUTION_EMAIL_DOMAIN") or "students.example.test").strip().lower().lstrip("@"),
        app_url=((os.getenv("APP_URL") or os.getenv("PUBLIC_APP_URL") or "").strip().rstrip("/")),
        smtp_host=(os.getenv("SMTP_HOST") or "").strip(),
        smtp_port=int(os.getenv("SMTP_PORT", "587")),
        smtp_username=(os.getenv("SMTP_USERNAME") or "").strip(),
        smtp_password=os.getenv("SMTP_PASSWORD") or "",
        smtp_from=(os.getenv("SMTP_FROM") or "").strip(),
        admin_notification_email=(os.getenv("ADMIN_NOTIFICATION_EMAIL") or "").strip(),
        smtp_use_tls=_as_bool(os.getenv("SMTP_USE_TLS"), True),
        app_env=(os.getenv("APP_ENV") or "development").strip().lower(),
        log_level=(os.getenv("LOG_LEVEL") or "INFO").strip().upper(),
        db_pool_size=int(os.getenv("DB_POOL_SIZE", "10")),
        db_max_overflow=int(os.getenv("DB_MAX_OVERFLOW", "20")),
        token_hours=int(os.getenv("TOKEN_HOURS", "12")),
        frontend_dist=_frontend_dist(),
    )


def _frontend_dist() -> Path | None:
    """Built React app served by the API when present (student laptop package). FRONTEND_DIST overrides."""
    configured = (os.getenv("FRONTEND_DIST") or "").strip()
    candidate = Path(configured) if configured else BACKEND_ROOT.parent / "dist"
    if not candidate.is_absolute():
        candidate = (BACKEND_ROOT / candidate).resolve()
    return candidate if (candidate / "index.html").is_file() else None
