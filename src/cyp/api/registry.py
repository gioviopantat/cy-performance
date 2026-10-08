"""One cached :class:`AppContext` per profile, so one server serves every athlete (spec web-ui).

Requests pick a profile with the ``X-CYP-Profile`` header; without it they get the default
context the server was started with. Each profile context is built exactly like
``cyp --profile <slug>`` builds one (hermetic settings, its own DB and athlete.yaml).
"""

from __future__ import annotations

import threading
from collections.abc import Callable

from cyp.core.errors import ConfigError, NotFoundError
from cyp.services.context import AppContext
from cyp.settings import profile_settings

PROFILE_HEADER = "X-CYP-Profile"


class ProfileRegistry:
    """Lazily built, cached contexts keyed by profile slug."""

    def __init__(
        self,
        default: AppContext,
        *,
        factory: Callable[[str], AppContext] | None = None,
    ) -> None:
        """``default`` serves header-less requests; ``factory`` builds a profile's context."""
        self.default = default
        self._factory = factory or self._from_profiles_dir
        self._contexts: dict[str, AppContext] = {}
        self._lock = threading.Lock()
        if default.settings.cyp_profile:
            self._contexts[default.settings.cyp_profile] = default

    def _from_profiles_dir(self, slug: str) -> AppContext:
        settings = profile_settings(slug, self.default.settings.cyp_profiles_dir)
        ctx = AppContext.from_settings(settings)
        ctx.fixed_now = self.default.fixed_now
        return ctx

    def get(self, slug: str | None) -> AppContext:
        """The context for ``slug`` (``None`` / empty = default).

        Raises:
            NotFoundError: unknown or invalid profile.
        """
        if not slug:
            return self.default
        with self._lock:
            if slug not in self._contexts:
                try:
                    self._contexts[slug] = self._factory(slug)
                except ConfigError as exc:
                    raise NotFoundError(str(exc)) from exc
            return self._contexts[slug]

    def close(self) -> None:
        """Dispose every profile context except the default (owned by the caller)."""
        with self._lock:
            for ctx in self._contexts.values():
                if ctx is not self.default:
                    ctx.close()
            self._contexts.clear()
