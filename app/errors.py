"""The one error type every Sosopo module raises for safe-to-display failures."""

from __future__ import annotations


class ProviderError(Exception):
    """A safe-to-display provider delivery error with retry guidance."""

    def __init__(self, message: str, *, retryable: bool = True, retry_after: int | None = None) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.retry_after = retry_after
