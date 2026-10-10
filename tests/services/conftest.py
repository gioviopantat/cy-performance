"""Reuse the seeded synthetic store from the API tests (session-scoped, copied per test)."""

from tests.api.conftest import ctx, data, seeded_dir  # noqa: F401
