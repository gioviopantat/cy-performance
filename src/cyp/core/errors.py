"""Exception hierarchy shared by every layer."""

from __future__ import annotations


class CypError(Exception):
    """Base class for all cy-performance errors."""


class ConfigError(CypError):
    """Settings or athlete config is missing or invalid."""


class StoreError(CypError):
    """Persistence failure (database, Parquet, migrations)."""


class SchemaError(StoreError):
    """Data does not match the expected schema (e.g. stream columns)."""


class NotFoundError(StoreError):
    """A requested row or file does not exist."""


class IngestError(CypError):
    """Upstream API or sync failure; ``status`` is the HTTP status when one was received."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class RateLimitError(IngestError):
    """Upstream rate limit hit; carries the retry delay when known."""

    def __init__(self, message: str, retry_after_s: float | None = None) -> None:
        super().__init__(message, status=429)
        self.retry_after_s = retry_after_s


class AnalysisError(CypError):
    """A metric could not be computed from the stored data."""


class PlanningError(CypError):
    """Planner could not satisfy the constraints."""


class GuardrailViolation(PlanningError):
    """A hard physiological guardrail would be breached."""


class PublishError(CypError):
    """Writing to the intervals.icu calendar failed or could not be verified."""
