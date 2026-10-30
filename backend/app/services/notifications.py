from __future__ import annotations

import logging
import html
import smtplib
import ssl
import time
from email.message import EmailMessage

from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.models import EmailOutbox, Notification, utcnow

logger = logging.getLogger(__name__)


def queue_email(db: Session, settings: Settings, event_key: str, recipient: str, subject: str, body: str) -> EmailOutbox | None:
    """Persist an email before attempting delivery. In-app notifications do not depend on SMTP."""
    if not settings.email_notifications_configured:
        return None
    existing = db.scalar(select(EmailOutbox).where(EmailOutbox.event_key == event_key))
    if existing is not None:
        return existing
    row = EmailOutbox(event_key=event_key, recipient=recipient, subject=subject, body=body)
    db.add(row)
    db.flush()
    return row


def _smtp_send(settings: Settings, message: EmailMessage) -> None:
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=10) as server:
        if settings.smtp_use_tls:
            server.starttls(context=ssl.create_default_context())
        if settings.smtp_username:
            server.login(settings.smtp_username, settings.smtp_password)
        server.send_message(message)


def send_temporary_credentials(
    settings: Settings,
    recipient: str,
    display_name: str,
    institutional_email: str,
    temporary_password: str,
    attempts: int = 3,
) -> tuple[bool, str | None]:
    """Deliver first-login credentials without persisting the plaintext password.

    The message body is built in memory for this delivery only. Nothing is written
    to the persistent email outbox, so a retry can never replay a stored password.
    Returns (delivered, error_code) where error_code is a non-secret classification.
    """
    if not settings.smtp_configured:
        return False, "SMTP_NOT_CONFIGURED"
    if not settings.app_url:
        return False, "APP_URL_NOT_CONFIGURED"
    login_url = f"{settings.app_url.rstrip('/')}/login"
    display = " ".join((display_name or "").split()) or "student"
    safe_name = html.escape(display)
    safe_email = html.escape(institutional_email)
    safe_password = html.escape(temporary_password)
    text_body = (
        f"Welcome to EduNexus, {display}!\n\n"
        "Your institution has approved your student registration and created an EduNexus account for you.\n\n"
        f"Institutional email: {institutional_email}\n"
        f"Temporary password: {temporary_password}\n\n"
        f"Sign in at: {login_url}\n\n"
        "This password is temporary. When you sign in, EduNexus will require you to set a new password before opening the portal.\n"
        "Do not share this password with anyone. Institution staff will never ask you for it.\n"
    )
    html_body = (
        "<p>Welcome to EduNexus, "
        f"<strong>{safe_name}</strong>!</p>"
        "<p>Your institution has approved your student registration and created an EduNexus account for you.</p>"
        f"<p><strong>Institutional email:</strong> {safe_email}<br />"
        f"<strong>Temporary password:</strong> <code>{safe_password}</code></p>"
        f'<p><a href="{html.escape(login_url)}">Sign in to EduNexus</a></p>'
        "<p>This password is temporary. When you sign in, EduNexus will require you to set a new password before opening the portal. "
        "Do not share this password with anyone. Institution staff will never ask you for it.</p>"
    )
    last_error: str | None = None
    for attempt in range(1, max(attempts, 1) + 1):
        message = EmailMessage()
        message["From"] = settings.smtp_from
        message["To"] = recipient
        message["Subject"] = "Welcome to EduNexus - your institutional account"
        message.set_content(text_body)
        message.add_alternative(html_body, subtype="html")
        try:
            _smtp_send(settings, message)
        except Exception as error:  # noqa: BLE001 - classification only, never the message
            last_error = type(error).__name__
            logger.warning("Credential email delivery failed on attempt %s (%s)", attempt, last_error)
            if attempt < attempts:
                time.sleep(0.25 * attempt)
            continue
        return True, None
    return False, last_error or "DELIVERY_FAILED"


def deliver_email_outbox(factory: sessionmaker[Session], settings: Settings, outbox_id: str) -> None:
    """Deliver one persisted email with bounded retries; log only exception classes."""
    if not settings.email_notifications_configured:
        return
    db = factory()
    try:
        row = db.get(EmailOutbox, outbox_id)
        if row is None or row.status == "sent":
            return
        claimed = db.execute(
            update(EmailOutbox)
            .where(EmailOutbox.id == outbox_id, EmailOutbox.status.in_(("pending", "failed")))
            .values(status="sending")
        )
        db.commit()
        if claimed.rowcount != 1:
            return
        email = EmailMessage()
        email["From"] = settings.smtp_from
        email["To"] = row.recipient
        email["Subject"] = row.subject
        email.set_content(row.body)
        for attempt in range(1, 4):
            try:
                with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=10) as server:
                    if settings.smtp_use_tls:
                        server.starttls(context=ssl.create_default_context())
                    if settings.smtp_username:
                        server.login(settings.smtp_username, settings.smtp_password)
                    server.send_message(email)
            except Exception as error:
                logger.warning("Registration notification email failed on attempt %s (%s)", attempt, type(error).__name__)
                row = db.get(EmailOutbox, outbox_id)
                row.attempts += 1
                row.last_error = type(error).__name__
                row.status = "failed" if attempt == 3 else "sending"
                if attempt == 3:
                    admin_notification_exists = db.scalar(select(Notification.id).where(Notification.email_outbox_id == row.id, Notification.audience_role == "administrator"))
                    failure_key = f"email-delivery-failed:{row.id}"
                    if admin_notification_exists is None and db.scalar(select(Notification.id).where(Notification.event_key == failure_key)) is None:
                        db.add(Notification(
                            event_key=failure_key,
                            audience_role="administrator",
                            kind="email_delivery_failed",
                            title="Email notification needs attention",
                            body="An email notification could not be delivered after automatic retries. The registration request and in-app notification remain available.",
                            email_outbox_id=row.id,
                        ))
                db.commit()
                if attempt < 3:
                    time.sleep(0.25 * attempt)
                continue
            row = db.get(EmailOutbox, outbox_id)
            row.attempts += 1
            row.status = "sent"
            row.sent_at = utcnow()
            row.last_error = None
            db.commit()
            return
    finally:
        db.close()
