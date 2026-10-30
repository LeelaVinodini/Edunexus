from __future__ import annotations

import logging
import time
from typing import Protocol

import httpx

from app.config import Settings

logger = logging.getLogger(__name__)


class LanguageModel(Protocol):
    name: str
    configured: bool

    def complete(self, instruction: str, context: str) -> str: ...


class ExtractiveModel:
    """Quotes retrieved passages. This is not a generative language model."""

    name = "extractive"
    configured = False

    def complete(self, instruction: str, context: str) -> str:
        if not context.strip():
            return "No approved course material matched this request."
        excerpts = [line.strip() for line in context.split("\n") if line.strip()][:4]
        quoted = "\n".join(f"• {line[:500]}" for line in excerpts)
        return (
            "Draft grounded in retrieved course materials. A language model is not configured, "
            "so this response quotes the closest passages instead of paraphrasing them.\n\n"
            f"{quoted}"
        )


class LanguageModelError(RuntimeError):
    """A language model call failed. The message is safe to show and never contains the key."""


_GEMINI_STATUS_MESSAGES = {
    400: "The Gemini API rejected the request. Check GEMINI_API_KEY and GEMINI_MODEL on the API server, then restart the API.",
    401: "The Gemini API rejected GEMINI_API_KEY on the API server. Check that the key is valid and restart the API.",
    403: "The Gemini API rejected GEMINI_API_KEY on the API server. Check that the key is valid and that the Gemini API is enabled for it.",
    404: "The Gemini model named by GEMINI_MODEL is not available to this key. Check GEMINI_MODEL.",
    408: "The Gemini request timed out. Retry shortly.",
    429: "The Gemini rate limit or free-tier quota was reached. Wait about a minute before trying again, or check usage in Google AI Studio.",
    500: "The Gemini service is temporarily unavailable. It is having a server problem; wait a few minutes and try again. Your saved work is unchanged.",
    502: "The Gemini service is temporarily unavailable. It is having a server problem; wait a few minutes and try again. Your saved work is unchanged.",
    503: "The Gemini service is temporarily unavailable. It is currently experiencing high demand; wait a few minutes and try again. Your saved work is unchanged.",
    504: "The Gemini service is temporarily unavailable. It is taking too long to respond; wait a few minutes and try again. Your saved work is unchanged.",
    529: "The Gemini service is temporarily unavailable. It is currently experiencing high demand; wait a few minutes and try again. Your saved work is unchanged.",
}

_HTTP_AUTH_STATUSES = {401, 403}
_GOOGLE_AUTH_STATUSES = {"UNAUTHENTICATED", "PERMISSION_DENIED"}
_GOOGLE_SERVER_STATUSES = {"UNAVAILABLE", "INTERNAL", "DEADLINE_EXCEEDED", "UNKNOWN"}


def _gemini_error_status(payload: object) -> str:
    """Reads Google's error status string (for example UNAVAILABLE, RESOURCE_EXHAUSTED) when present."""
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            return str(error.get("status") or "")
    return ""


def _gemini_status_message(status_code: int, payload: object) -> str:
    """Maps a Gemini HTTP error to a safe, user-facing message. Google's own text is never shown to students."""
    api_status = _gemini_error_status(payload)
    if status_code in _HTTP_AUTH_STATUSES or api_status in _GOOGLE_AUTH_STATUSES:
        return _GEMINI_STATUS_MESSAGES[status_code] if status_code in _HTTP_AUTH_STATUSES else _GEMINI_STATUS_MESSAGES[401]
    if status_code == 404 or api_status == "NOT_FOUND":
        return _GEMINI_STATUS_MESSAGES[404]
    if status_code == 429 or api_status == "RESOURCE_EXHAUSTED":
        return _GEMINI_STATUS_MESSAGES[429]
    if status_code in _GEMINI_STATUS_MESSAGES:
        return _GEMINI_STATUS_MESSAGES[status_code]
    if api_status in _GOOGLE_SERVER_STATUSES:
        return _GEMINI_STATUS_MESSAGES[503]
    return f"Gemini returned an error (HTTP {status_code})."

_GEMINI_BLOCKED_FINISH_REASONS = {"SAFETY", "RECITATION", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII"}

_GEMINI_TIMEOUT_SECONDS = 30.0
_GEMINI_RETRY_DELAY_SECONDS = 0.25

_GEMINI_SYSTEM = (
    "You are an EduNexus academic tutor for university students. Answer like a knowledgeable, natural tutor: direct, accurate, "
    "and no longer than the question needs. Use numbered course excerpts only as supporting context; place [n] citations only next "
    "to claims those exact excerpts support, and never attribute general knowledge to the notes. Write the answer in your own words: "
    "do not copy sentences from the excerpts, do not repeat the documents' headings, and never output the retrieved passages themselves "
    "as the answer. Use the instruction's exact heading "
    "once when you add general knowledge. For historical or specialised detail, give names, dates, and attributions only when confident; "
    "otherwise explain generally and say they need checking against a reliable source. Do not invent dates, sources, "
    "quotations, statistics, or page numbers, and do not add generic introductions or repeated disclaimers."
)


class GeminiModel:
    """Google Gemini Developer API (generateContent). The key is sent in a header, never in the URL."""

    name = "gemini"
    configured = True

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def complete(self, instruction: str, context: str) -> str:
        url = f"{self.settings.gemini_base_url}/models/{self.settings.gemini_model}:generateContent"
        request_kwargs = {
            "headers": {"x-goog-api-key": self.settings.gemini_api_key},
            "json": {
                "systemInstruction": {"parts": [{"text": _GEMINI_SYSTEM}]},
                "contents": [{"role": "user", "parts": [{"text": f"{instruction}\n\nCourse excerpts:\n{context}"}]}],
                "generationConfig": {"temperature": 0.2},
            },
            "timeout": _GEMINI_TIMEOUT_SECONDS,
        }
        response = None
        for attempt in range(2):
            try:
                response = httpx.post(url, **request_kwargs)
            except httpx.TimeoutException as error:
                if attempt == 0:
                    logger.warning("Gemini request timed out; retrying once")
                    time.sleep(_GEMINI_RETRY_DELAY_SECONDS)
                    continue
                logger.warning("Gemini request timed out after %.0fs", _GEMINI_TIMEOUT_SECONDS)
                raise LanguageModelError("The Gemini request timed out. Retry shortly.") from error
            except httpx.TransportError as error:
                if attempt == 0:
                    logger.warning("Gemini network request failed; retrying once")
                    time.sleep(_GEMINI_RETRY_DELAY_SECONDS)
                    continue
                logger.warning("Gemini could not be reached: %s", type(error).__name__)
                raise LanguageModelError("Gemini could not be reached. Check the network connection on the API server and GEMINI_BASE_URL.") from error
            except httpx.HTTPError as error:
                logger.warning("Gemini request failed: %s", type(error).__name__)
                raise LanguageModelError("Gemini could not be reached. Check the network connection on the API server and GEMINI_BASE_URL.") from error

            if response.status_code == 503:
                try:
                    transient_payload = response.json()
                except (ValueError, TypeError):
                    transient_payload = None
                if attempt == 0 and _gemini_error_status(transient_payload) in {"", "UNAVAILABLE"}:
                    logger.warning("Gemini HTTP 503 is temporarily unavailable; retrying once")
                    time.sleep(_GEMINI_RETRY_DELAY_SECONDS)
                    continue
            break

        if response is None:
            raise LanguageModelError("Gemini could not be reached. Check the network connection on the API server and GEMINI_BASE_URL.")
        if response.status_code >= 400:
            try:
                error_payload = response.json()
            except (ValueError, TypeError):
                error_payload = None
            api_status = _gemini_error_status(error_payload)
            logger.warning("Gemini HTTP error status=%s googleStatus=%s", response.status_code, api_status or "unknown")
            raise LanguageModelError(_gemini_status_message(response.status_code, error_payload))
        try:
            payload = response.json()
            candidates = payload.get("candidates") or []
            if not candidates:
                reason = (payload.get("promptFeedback") or {}).get("blockReason")
                if reason:
                    raise LanguageModelError("Gemini declined to answer this question. Rephrase it or ask about a different part of the chapter.")
                raise LanguageModelError("Gemini returned an unexpected response.")
            candidate = candidates[0]
            parts = candidate.get("content", {}).get("parts") or []
            text = "".join(str(part.get("text", "")) for part in parts).strip()
            finish_reason = candidate.get("finishReason")
        except LanguageModelError:
            raise
        except (ValueError, AttributeError, TypeError) as error:
            raise LanguageModelError("Gemini returned an unexpected response.") from error
        if not text and finish_reason in _GEMINI_BLOCKED_FINISH_REASONS:
            raise LanguageModelError("Gemini declined to answer this question. Rephrase it or ask about a different part of the chapter.")
        if not text:
            raise LanguageModelError("Gemini returned an empty answer. Try rephrasing the question.")
        return text


class FakeModel:
    """Test double. It must not be used as a production provider."""

    name = "fake"
    configured = True

    def complete(self, instruction: str, context: str) -> str:
        return f"FAKE_MODEL {instruction[:80]} :: {context[:180]}"


def build_language_model(settings: Settings) -> LanguageModel:
    if settings.gemini_api_key:
        return GeminiModel(settings)
    return ExtractiveModel()
