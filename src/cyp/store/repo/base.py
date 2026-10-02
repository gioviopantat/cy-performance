"""Repository base: wraps a ``Session``; subclasses are the only code that issues SQL."""

from __future__ import annotations

from sqlalchemy.orm import Session


class Repo:
    """Thin base holding the session. Repos never commit; the caller owns the transaction."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def flush(self) -> None:
        """Push pending changes to the DB without committing."""
        self.session.flush()
