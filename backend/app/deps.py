from __future__ import annotations

import time
from collections import defaultdict, deque
from collections.abc import Callable

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import User
from app.security import decode_token

bearer = HTTPBearer(auto_error=False)
_attempts: dict[str, deque[float]] = defaultdict(deque)


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_db(request: Request):
    factory = request.app.state.session_factory
    db = factory()
    try:
        yield db
    finally:
        db.close()


def enforce_login_rate(request: Request, email: str) -> None:
    key = f"{request.client.host if request.client else 'local'}:{email.lower()}"
    now = time.monotonic()
    bucket = _attempts[key]
    while bucket and now - bucket[0] > 60:
        bucket.popleft()
    if len(bucket) >= 8:
        raise HTTPException(status_code=429, detail="Too many sign-in attempts. Wait a minute and try again.")
    bucket.append(now)


def student_access_problem(user: User) -> str | None:
    if user.role != "student" or user.approval_status == "approved":
        return None
    if user.approval_status == "rejected":
        return "Your student registration was not approved. Contact the institution for help."
    return "Your student registration is awaiting administrator approval."


def _resolve_authenticated_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None,
    db: Session,
    settings: Settings,
) -> User:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(status_code=401, detail="Authentication required.")
    try:
        payload = decode_token(settings, credentials.credentials)
        user = db.get(User, str(payload.get("sub")))
        token_version = int(payload.get("ver", 0) or 0)
    except Exception as error:
        raise HTTPException(status_code=401, detail="Authentication required.") from error
    if user is None or token_version != int(user.token_version or 0):
        raise HTTPException(status_code=401, detail="Authentication required.")
    problem = student_access_problem(user)
    if problem:
        raise HTTPException(status_code=403, detail=problem)
    if not user.is_active:
        raise HTTPException(status_code=401, detail="Authentication required.")
    request.state.user = user
    return user


def current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> User:
    user = _resolve_authenticated_user(request, credentials, db, settings)
    if user.must_change_password:
        raise HTTPException(
            status_code=403,
            detail="Set a new password before opening the student portal.",
        )
    return user


def current_user_password_setup(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> User:
    """Authenticated user who may still be required to replace a temporary password.

    Only identity lookup and the password-change endpoint use this dependency.
    """
    return _resolve_authenticated_user(request, credentials, db, settings)


def require_roles(*roles: str) -> Callable:
    def dependency(user: User = Depends(current_user)) -> User:
        if user.role not in roles:
            raise HTTPException(status_code=403, detail="You do not have access to this resource.")
        return user

    return dependency
