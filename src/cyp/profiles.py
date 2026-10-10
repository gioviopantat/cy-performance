"""Profiles: one workspace directory per athlete (ADR-0006 L1).

Layout of ``profiles/<slug>/`` (constants live in :mod:`cyp.settings`)::

    .env           secrets + machine settings for this athlete only (mode 600)
    athlete.yaml   intent: goals, season, availability, planner knobs, feature flags
    profile.yaml   metadata: display name, the icu athlete id the key belongs to (write guard)
    data/          SQLite DB, Parquet streams, tokens, logs, reports

Everything above this layer reaches profiles through :class:`ProfileStore`, so a DB-backed
store (ADR-0006 L4) can replace :class:`FilesystemProfileStore` without touching callers.
``profiles/`` is git-ignored: it holds personal data (ADR-0006 §8).
"""

from __future__ import annotations

import datetime as dt
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import yaml
from pydantic import BaseModel, ConfigDict

from cyp.core.errors import ConfigError
from cyp.settings import (
    PROFILE_ATHLETE,
    PROFILE_DATA,
    PROFILE_DEFAULT_FILE,
    PROFILE_ENV,
    PROFILE_META,
    SLUG_PATTERN,
    load_athlete_config,
)

SLUG_RE = re.compile(SLUG_PATTERN)


class ProfileMeta(BaseModel):
    """``profile.yaml``: who this profile is, independent of training intent."""

    model_config = ConfigDict(extra="forbid")

    slug: str
    display_name: str
    #: intervals.icu athlete id the API key resolved to at creation (``i123456``). Publishing
    #: refuses to write when the key now resolves to someone else.
    icu_athlete_id: str | None = None
    created: dt.date


@dataclass(frozen=True)
class Profile:
    """A profile on disk."""

    slug: str
    root: Path
    meta: ProfileMeta

    @property
    def env_path(self) -> Path:
        """``.env`` with this athlete's secrets."""
        return self.root / PROFILE_ENV

    @property
    def athlete_config_path(self) -> Path:
        """``athlete.yaml``."""
        return self.root / PROFILE_ATHLETE

    @property
    def data_dir(self) -> Path:
        """``data/``."""
        return self.root / PROFILE_DATA

    def planner_mode(self) -> str | None:
        """``planner.mode`` from athlete.yaml, or ``None`` when it is missing/invalid."""
        try:
            return load_athlete_config(self.athlete_config_path).planner.mode
        except ConfigError:
            return None


def check_slug(slug: str) -> str:
    """Return ``slug`` if valid (lowercase letter, then letters/digits/-, ≤ 32 chars).

    Raises:
        ConfigError: invalid slug.
    """
    if not SLUG_RE.match(slug):
        raise ConfigError(
            f"invalid profile name {slug!r}: lowercase letters, digits and '-', "
            "starting with a letter"
        )
    return slug


class ProfileStore(Protocol):
    """Where profiles live (filesystem today, a DB at ADR-0006 L4)."""

    def list(self) -> list[Profile]:
        """Every profile, sorted by slug."""
        ...

    def get(self, slug: str) -> Profile:
        """One profile; raises ``ConfigError`` when missing."""
        ...

    def create(self, meta: ProfileMeta, *, env: dict[str, str], athlete_yaml: str) -> Profile:
        """Create a new profile; raises ``ConfigError`` when it exists."""
        ...

    def save_meta(self, meta: ProfileMeta) -> None:
        """Rewrite ``profile.yaml``."""
        ...

    def default(self) -> str:
        """Slug used when none is selected, or ``""``."""
        ...

    def set_default(self, slug: str) -> None:
        """Make ``slug`` the default profile."""
        ...


class FilesystemProfileStore:
    """Profiles as directories under ``root`` (default ``profiles/``)."""

    def __init__(self, root: Path) -> None:
        """Store rooted at ``root`` (created on first write)."""
        self.root = root

    def list(self) -> list[Profile]:
        """Every directory with a ``profile.yaml``, sorted by slug."""
        if not self.root.is_dir():
            return []
        return [
            self.get(p.name)
            for p in sorted(self.root.iterdir())
            if p.is_dir() and (p / PROFILE_META).is_file()
        ]

    def get(self, slug: str) -> Profile:
        """Load ``<root>/<slug>/profile.yaml``.

        Raises:
            ConfigError: missing directory or invalid metadata.
        """
        root = self.root / check_slug(slug)
        meta_path = root / PROFILE_META
        if not meta_path.is_file():
            raise ConfigError(
                f"profile {slug!r} not found ({meta_path} missing); see `cyp profile list`"
            )
        try:
            meta = ProfileMeta.model_validate(yaml.safe_load(meta_path.read_text("utf-8")))
        except (yaml.YAMLError, ValueError) as exc:
            raise ConfigError(f"invalid {meta_path}: {exc}") from exc
        return Profile(slug, root, meta)

    def create(self, meta: ProfileMeta, *, env: dict[str, str], athlete_yaml: str) -> Profile:
        """Write ``.env`` (mode 600), ``athlete.yaml``, ``profile.yaml`` and ``data/``.

        Raises:
            ConfigError: the profile already has a ``profile.yaml``.
        """
        root = self.root / check_slug(meta.slug)
        if (root / PROFILE_META).exists():
            raise ConfigError(f"profile {meta.slug!r} already exists in {root}")
        root.mkdir(parents=True, exist_ok=True)
        (root / PROFILE_DATA).mkdir(exist_ok=True)
        write_env(root / PROFILE_ENV, env)
        (root / PROFILE_ATHLETE).write_text(athlete_yaml, encoding="utf-8")
        self.save_meta(meta)
        return self.get(meta.slug)

    def save_meta(self, meta: ProfileMeta) -> None:
        """Rewrite ``<root>/<slug>/profile.yaml``."""
        path = self.root / check_slug(meta.slug) / PROFILE_META
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            yaml.safe_dump(meta.model_dump(mode="json"), allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )

    def default(self) -> str:
        """Slug in ``<root>/.default`` or ``""``."""
        marker = self.root / PROFILE_DEFAULT_FILE
        return marker.read_text(encoding="utf-8").strip() if marker.is_file() else ""

    def set_default(self, slug: str) -> None:
        """Write ``<root>/.default``.

        Raises:
            ConfigError: no such profile.
        """
        self.get(slug)
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / PROFILE_DEFAULT_FILE).write_text(slug + "\n", encoding="utf-8")


def _env_value(value: str) -> str:
    """``value`` quoted so python-dotenv reads it back unchanged.

    Single quotes (no escapes, no expansion) when it has spaces, ``#``, ``$`` or ``=``;
    double quotes with escapes when it contains a single quote or a newline.
    """
    if value and not any(c in value for c in " \t#'\"$=\\"):
        return value
    if "'" in value or "\n" in value:
        escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
        return f'"{escaped}"'
    return f"'{value}'"


def write_env(path: Path, values: dict[str, str]) -> None:
    """Write ``KEY=value`` lines with mode 600 (created private, never world-readable)."""
    body = "".join(f"{k}={_env_value(v)}\n" for k, v in values.items())
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(body)
    path.chmod(0o600)


def set_planner_mode(athlete_yaml: Path, mode: str) -> None:
    """Set ``planner.mode`` in place, keeping comments; atomic and verified.

    Edits only the ``mode:`` line directly under the top-level ``planner:`` key (adding one when
    missing), writes via a temp file + rename, and checks that the parsed result differs from
    the original in ``planner.mode`` only.

    Raises:
        ConfigError: invalid mode, no single top-level ``planner:`` section to edit safely, or
            the edit would change anything else.
    """
    if mode not in ("propose", "apply"):
        raise ConfigError(f"planner mode must be propose or apply, not {mode!r}")
    text = athlete_yaml.read_text(encoding="utf-8")
    before = yaml.safe_load(text)
    lines = text.splitlines(keepends=True)
    starts = [i for i, line in enumerate(lines) if re.match(r"^planner:\s*(#.*)?$", line)]
    if len(starts) > 1:
        raise ConfigError(f"{athlete_yaml}: more than one top-level planner: section")
    if not starts:
        sep = "" if text.endswith("\n") or not text else "\n"
        new = f"{text}{sep}planner:\n  mode: {mode}\n"
    else:
        i = starts[0] + 1
        end = i
        while end < len(lines) and (lines[end].startswith((" ", "\t")) or not lines[end].strip()):
            end += 1
        body = lines[i:end]
        idx = next((j for j, line in enumerate(body) if re.match(r"^  mode:\s*\S+", line)), None)
        if idx is None:
            body.insert(0, f"  mode: {mode}\n")
        else:
            body[idx] = re.sub(r"^(  mode:\s*)\S+", lambda m: m.group(1) + mode, body[idx])
        new = "".join(lines[:i] + body + lines[end:])
    after = yaml.safe_load(new)
    expected = dict(before or {})
    expected["planner"] = {**(before or {}).get("planner", {}), "mode": mode}
    if after != expected:
        raise ConfigError(f"{athlete_yaml}: editing planner.mode would change other settings")
    tmp = athlete_yaml.with_name(f".{athlete_yaml.name}.tmp")
    tmp.write_text(new, encoding="utf-8")
    try:
        load_athlete_config(tmp)
    except ConfigError:
        tmp.unlink()
        raise
    tmp.replace(athlete_yaml)


def open_store(profiles_dir: Path) -> FilesystemProfileStore:
    """The profile store for this machine (the one seam to swap for a DB store, ADR-0006 L4)."""
    return FilesystemProfileStore(profiles_dir)
