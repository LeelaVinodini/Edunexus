from __future__ import annotations

import hashlib
import math
import re

TOKEN_PATTERN = re.compile(r"[a-z0-9]+")
DIMENSIONS = 64
STOPWORDS = {
    "a", "an", "the", "and", "or", "of", "to", "in", "on", "for", "with", "is", "are", "was", "were",
    "be", "this", "that", "from", "by", "as", "at", "it", "its", "into", "your", "you", "we", "our",
}


def embed_text(text: str) -> list[float]:
    """Deterministic hashing embedding used when no embedding provider is configured.

    It supports retrieval tests and local operation. It is not a hosted embedding model.
    """
    vector = [0.0] * DIMENSIONS
    tokens = TOKEN_PATTERN.findall(text.lower())
    if not tokens:
        return vector
    for token in tokens:
        digest = int(hashlib.sha256(token.encode("utf-8")).hexdigest(), 16)
        index = digest % DIMENSIONS
        sign = 1.0 if ((digest // DIMENSIONS) % 2 == 0) else -1.0
        vector[index] += sign
    norm = math.sqrt(sum(value * value for value in vector)) or 1.0
    return [value / norm for value in vector]


def cosine(left: list[float], right: list[float]) -> float:
    if len(left) != len(right) or not left:
        return 0.0
    return float(sum(a * b for a, b in zip(left, right, strict=True)))


def content_tokens(text: str) -> list[str]:
    return [token for token in TOKEN_PATTERN.findall(text.lower()) if token not in STOPWORDS and len(token) > 2]
