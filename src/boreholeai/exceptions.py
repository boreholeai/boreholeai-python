"""BoreholeAI SDK exceptions."""

from __future__ import annotations


class BoreholeAIError(Exception):
    """Base exception for all SDK errors."""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class AuthenticationError(BoreholeAIError):
    """Invalid or missing API key (401)."""


class InsufficientCreditsError(BoreholeAIError):
    """Not enough credits to process the request (402)."""


class RateLimitError(BoreholeAIError):
    """Too many requests (429)."""


class ServerError(BoreholeAIError):
    """Worker returned 5xx."""


class JobFailedError(BoreholeAIError):
    """Job processing failed on the server."""
