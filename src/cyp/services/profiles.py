"""Profile operations shared by the CLI and (later) a sign-up UI: onboard, adopt, status.

ADR-0006. The CLI only asks questions and prints; everything that touches intervals.icu, files
or the database lives here. Answers to the onboarding questions (docs/onboarding-questions.md)
arrive as :class:`OnboardingAnswers`.
"""

from __future__ import annotations

import datetime as dt
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dotenv import dotenv_values
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from cyp.core.errors import ConfigError, CypError
from cyp.ingest.intervals.auth import ApiKeyAuth
from cyp.ingest.intervals.client import IntervalsClient
from cyp.ingest.intervals.sync import CURSOR_ATHLETE_ID
from cyp.ingest.intervals.sync import SOURCE as ICU_SOURCE
from cyp.planning.athlete_rules import SENIOR_AGE
from cyp.profiles import FilesystemProfileStore, Profile, ProfileMeta, check_slug
from cyp.services.context import AppContext
from cyp.services.publish import store_upsert_mode
from cyp.settings import (
    PROFILE_ATHLETE,
    PROFILE_DATA,
    PROFILE_ENV,
    PROFILE_META,
    WEEKDAYS,
    Settings,
    load_athlete_config,
    profile_settings,
    with_feature_switches,
)
from cyp.store.db import engine_from_settings, session_factory
from cyp.store.migrate import upgrade_head
from cyp.store.models import Athlete, JobRun
from cyp.store.repo.sync_cursors import SyncCursorRepo

DEFAULT_AVAILABILITY = "mon=0,tue=60,wed=0,thu=60,fri=0,sat=150,sun=120"
HEALTH_FLAGS = ("heart_or_bp", "symptoms_on_exertion", "hr_medication", "injury")


def parse_health(text: str) -> list[str]:
    """``"none"`` / ``""`` -> ``[]``; else comma-separated flags from :data:`HEALTH_FLAGS`.

    Raises:
        ConfigError: unknown flag.
    """
    items = [i.strip() for i in text.split(",") if i.strip() and i.strip().lower() != "none"]
    bad = [i for i in items if i not in HEALTH_FLAGS]
    if bad:
        raise ConfigError(f"unknown health flag(s) {bad}; known: {', '.join(HEALTH_FLAGS)}")
    return items


AUTOPILOT_JOB = "autopilot"


# ------------------------------------------------------------------------------- intervals


@dataclass(frozen=True)
class IcuAthlete:
    """What onboarding reads from intervals.icu instead of asking."""

    id: str
    name: str
    ftp_w: int | None
    weight_kg: float | None
    timezone: str
    garmin_upload_workouts: bool | None


def parse_icu_athlete(raw: dict[str, Any]) -> IcuAthlete:
    """``GET /athlete/{id}`` body -> :class:`IcuAthlete`."""
    ftp = next(
        (
            int(s["ftp"])
            for s in raw.get("sportSettings") or []
            if "Ride" in (s.get("types") or []) and s.get("ftp")
        ),
        None,
    )
    weight = raw.get("icu_weight") or raw.get("weight")
    upload = raw.get("icu_garmin_upload_workouts")
    return IcuAthlete(
        id=str(raw.get("id") or ""),
        name=str(raw.get("name") or ""),
        ftp_w=ftp,
        weight_kg=float(weight) if weight else None,
        timezone=str(raw.get("timezone") or "Asia/Taipei"),
        garmin_upload_workouts=None if upload is None else bool(upload),
    )


def fetch_icu_athlete(
    key: str, *, client_factory: Callable[[str], IntervalsClient] | None = None
) -> IcuAthlete:
    """The key owner's athlete record (``GET /athlete/0``).

    Raises:
        ConfigError: intervals.icu rejected the key or returned no athlete id.
    """
    client = (client_factory or _client)(key)
    try:
        athlete = parse_icu_athlete(dict(client.get_athlete()))
    except CypError as exc:
        raise ConfigError(f"intervals.icu rejected the key: {exc}") from exc
    finally:
        client.close()
    if not athlete.id:
        raise ConfigError("intervals.icu returned no athlete id for this key")
    return athlete


def _client(key: str) -> IntervalsClient:
    return IntervalsClient(auth=ApiKeyAuth(key), athlete_id="0")


# ------------------------------------------------------------------------------ onboarding


@dataclass
class OnboardingAnswers:
    """Answers that intervals.icu cannot give (docs/onboarding-questions.md)."""

    display_name: str
    goal_ftp: int
    availability: dict[str, int]
    weeks: int = 26
    start: dt.date | None = None
    #: Question 1.
    birth_year: int | None = None
    #: Question 2: ``[]`` = screened, none; ``None`` = not asked.
    health_flags: list[str] | None = None
    #: Question 3 when the goal is a distance ("160 km without fatigue").
    distance_km: int | None = None


def parse_availability(text: str) -> dict[str, int]:
    """``"tue=60,sat=150"`` -> minutes per weekday (missing days = 0).

    Raises:
        ConfigError: unknown day or non-integer minutes.
    """
    out: dict[str, int] = dict.fromkeys(WEEKDAYS, 0)
    for item in filter(None, (i.strip() for i in text.split(","))):
        day, _, minutes = item.partition("=")
        day = day.strip().lower()[:3]
        if day not in out or not minutes.strip().isdigit():
            raise ConfigError(f"availability entry {item!r}: expected e.g. tue=60")
        out[day] = int(minutes)
    return out


def next_monday(day: dt.date) -> dt.date:
    """``day`` if it is a Monday, else the following Monday."""
    return day + dt.timedelta(days=(7 - day.weekday()) % 7)


def default_goal_ftp(ftp_w: int) -> int:
    """+10 % rounded to 5 W."""
    return int(round(ftp_w * 1.1 / 5) * 5)


def render_athlete_yaml(
    *,
    weight_kg: float,
    ftp_w: int,
    goal_ftp: int,
    season_start: dt.date,
    goal_date: dt.date,
    availability: dict[str, int],
    timezone: str,
    birth_year: int | None = None,
    health_flags: list[str] | None = None,
    distance_km: int | None = None,
) -> str:
    """Minimal athlete.yaml for a new profile; every optional section keeps its default."""
    days = "\n".join(f"  {d}: {availability[d]}" for d in WEEKDAYS)
    weekly = sum(availability.values())
    extra_athlete = ""
    if birth_year is not None:
        extra_athlete += f"  birth_year: {birth_year}\n"
    if health_flags is not None:
        extra_athlete += f"  health_flags: [{', '.join(health_flags)}]\n"
    distance_goal = ""
    long_cap = ""
    if distance_km:
        distance_goal = (
            f'  - name: "{distance_km} km"\n'
            f"    date: {goal_date.isoformat()}\n"
            "    category: TARGET\n"
            "    kind: distance\n"
            f"    target: {{km: {distance_km}}}\n"
        )
        long_day_minutes = max(availability.values(), default=0)
        long_cap = f"  long_ride_max_minutes: {max(long_day_minutes + 90, 240)}\n"
    if birth_year is not None and season_start.year - birth_year >= SENIOR_AGE:
        # Leave load_pattern unset so the age default (2:1) applies; no ramp test (athlete_rules).
        season_rules = (
            "  # load_pattern: age default 2:1 (planning/athlete_rules.py)\n"
            "  tests:\n"
            "    twenty_min: {every_weeks: 8, offset_week: 8}\n"
        )
    else:
        season_rules = (
            '  load_pattern: "3:1"\n'
            "  tests:\n"
            "    ramp:   {every_weeks: 8, offset_week: 4}\n"
            "    twenty_min: {every_weeks: 8, offset_week: 8}\n"
        )
    return f"""# Athlete intent for this profile (private: profiles/ is git-ignored).
# Format and every optional section: config/athlete.example.yaml. Validate: cyp doctor.
timezone: {timezone}
athlete:
  weight_kg: {weight_kg:g}
  ftp_w: {ftp_w}                       # from intervals.icu at creation; icu stays canonical
{extra_athlete}
goals:
  - name: "FTP {goal_ftp}"
    date: {goal_date.isoformat()}
    category: TARGET
    kind: ftp_target
    target: {{ftp: {goal_ftp}}}
{distance_goal}
season:
  start: {season_start.isoformat()}
  type: ftp_target
{season_rules}
availability:                      # minutes per weekday
  weekly_max_minutes: {weekly}
{long_cap}{days}

planner:
  mode: propose                    # `cyp profile promote <name>` switches to apply

features:                          # see docs/09-features.md; omitted = default
  sync.strava: false
"""


def onboard(
    store: FilesystemProfileStore,
    slug: str,
    *,
    key: str,
    athlete: IcuAthlete,
    answers: OnboardingAnswers,
) -> Profile:
    """Create ``profiles/<slug>/`` (propose mode), its DB at head, and the known upsert mode.

    Keeps unknown lines of an existing ``profiles/<slug>/.env`` (e.g. Strava app credentials).

    Raises:
        ConfigError: bad slug, profile exists, or the .env names a different athlete.
    """
    check_slug(slug)
    root = store.root / slug
    if (root / PROFILE_META).exists():
        raise ConfigError(f"profile {slug!r} already exists ({root})")
    existing = existing_env(store, slug)
    configured = existing.get("INTERVALS_ATHLETE_ID", "0") or "0"
    if configured not in ("0", athlete.id):
        raise ConfigError(
            f"the key belongs to {athlete.id}, but .env says INTERVALS_ATHLETE_ID={configured}"
        )
    start = answers.start or next_monday(dt.date.today())
    yaml_text = render_athlete_yaml(
        weight_kg=round(athlete.weight_kg or 70.0, 1),
        ftp_w=athlete.ftp_w or 200,
        goal_ftp=answers.goal_ftp,
        season_start=start,
        goal_date=start + dt.timedelta(weeks=answers.weeks) - dt.timedelta(days=3),
        availability=answers.availability,
        timezone=athlete.timezone,
        birth_year=answers.birth_year,
        health_flags=answers.health_flags,
        distance_km=answers.distance_km,
    )
    env = {
        **existing,
        "INTERVALS_API_KEY": key,
        "INTERVALS_ATHLETE_ID": athlete.id,
        "CYP_TIMEZONE": athlete.timezone,
    }
    meta = ProfileMeta(
        slug=slug,
        display_name=answers.display_name,
        icu_athlete_id=athlete.id,
        created=dt.date.today(),
    )
    profile = store.create(meta, env=env, athlete_yaml=yaml_text)
    load_athlete_config(profile.athlete_config_path)  # fail loudly on a template bug
    prepare_storage(profile_settings(slug, store.root))
    if not store.default():
        store.set_default(slug)
    return profile


def existing_env(store: FilesystemProfileStore, slug: str) -> dict[str, str]:
    """``profiles/<slug>/.env`` as a dict (empty when absent)."""
    path = store.root / slug / PROFILE_ENV
    return {k: v for k, v in dotenv_values(path).items() if v is not None}


def prepare_storage(settings: Settings) -> None:
    """Data layout + schema at head + known upsert mode for a profile."""
    for path in settings.data_layout:
        path.mkdir(parents=True, exist_ok=True)
    settings.tokens_dir.chmod(0o700)
    upgrade_head(settings.cyp_db_url)
    ctx = AppContext.from_settings(settings)
    try:
        # events/bulk upsert on external_id is an intervals.icu API property (the live spike on
        # 2026-10-03 confirmed it with an API key); it does not differ per athlete.
        store_upsert_mode(ctx, "upsert")
    finally:
        ctx.close()


@dataclass(frozen=True)
class BackfillResult:
    """History download outcome."""

    activities: int
    streams: int


def backfill(settings: Settings, days: int) -> BackfillResult:
    """Download ``days`` of history for a profile (resumable).

    Raises:
        CypError: a source failed.
    """
    from cyp.jobs.sync import run_sync

    result = run_sync(with_feature_switches(settings), backfill_days=days, no_wait=True)
    return BackfillResult(
        activities=sum(c.get("activities_new", 0) for c in result.intervals.values()),
        streams=sum(c.get("streams_written", 0) for c in result.intervals.values()),
    )


# ----------------------------------------------------------------------------------- adopt


@dataclass
class AdoptPlan:
    """What ``adopt_legacy`` will do (shown to the user before confirming)."""

    slug: str
    root: Path
    env: Path | None
    data: Path | None
    config: Path | None
    config_is_target_link: bool
    notes: list[str] = field(default_factory=list)


def plan_adopt(store: FilesystemProfileStore, slug: str, base: Settings) -> AdoptPlan:
    """Check that the legacy layout can move into ``profiles/<slug>/`` without loss.

    Legacy paths come from the root settings (``CYP_DATA_DIR``, ``CYP_ATHLETE_CONFIG``), not
    hard-coded names.

    Raises:
        ConfigError: profile exists, target files would be overwritten, or the DB lives outside
            the data dir (it would be left behind).
    """
    check_slug(slug)
    root = store.root / slug
    if (root / PROFILE_META).exists():
        raise ConfigError(f"profile {slug!r} already exists")
    env = Path(".env")
    data, cfg = base.cyp_data_dir, base.cyp_athlete_config
    target_cfg = root / PROFILE_ATHLETE
    if env.is_file() and (root / PROFILE_ENV).exists():
        raise ConfigError(f"{root / PROFILE_ENV} exists; refusing to overwrite it")
    target_data = root / PROFILE_DATA
    if data.is_dir() and target_data.exists() and any(target_data.iterdir()):
        raise ConfigError(f"{target_data} already has data; refusing to merge")
    db_path = base.cyp_db_url.removeprefix("sqlite:///")
    if data.is_dir() and not Path(db_path).resolve().is_relative_to(data.resolve()):
        raise ConfigError(f"CYP_DB_URL points outside {data}/; move the DB into it first")
    link_to_target = cfg.is_symlink() and cfg.resolve() == target_cfg.resolve()
    if not cfg.exists() and not target_cfg.is_file():
        raise ConfigError(f"no athlete config at {cfg} or {target_cfg}")
    if cfg.exists() and not link_to_target and target_cfg.exists():
        raise ConfigError(f"{target_cfg} exists; refusing to overwrite it")
    return AdoptPlan(
        slug=slug,
        root=root,
        env=env if env.is_file() else None,
        data=data if data.is_dir() else None,
        config=cfg if cfg.exists() or cfg.is_symlink() else None,
        config_is_target_link=link_to_target,
    )


def adopt_legacy(store: FilesystemProfileStore, plan: AdoptPlan, display_name: str) -> Profile:
    """Execute :func:`plan_adopt`'s plan under the profile lock; profile.yaml is written first.

    Raises:
        RunLockedError: an autopilot run is using the legacy data dir.
    """
    from cyp.jobs.runner import profile_lock

    plan.root.mkdir(parents=True, exist_ok=True)
    # Metadata first: a failure below leaves a visible profile, never an invisible half-move.
    store.save_meta(ProfileMeta(slug=plan.slug, display_name=display_name, created=dt.date.today()))
    lock_dir = plan.data or plan.root
    with profile_lock(lock_dir):
        if plan.env:
            shutil.move(str(plan.env), plan.root / PROFILE_ENV)
            (plan.root / PROFILE_ENV).chmod(0o600)
        if plan.config is not None:
            target = plan.root / PROFILE_ATHLETE
            if plan.config_is_target_link:
                plan.config.unlink()
            else:  # copy the content (a symlink elsewhere is followed), then remove the original
                shutil.copy2(plan.config, target)
                plan.config.unlink()
        if plan.data:  # the open lock fd stays valid while its directory moves
            target_data = plan.root / PROFILE_DATA
            if target_data.exists():
                target_data.rmdir()
            shutil.move(str(plan.data), target_data)
    settings = profile_settings(plan.slug, store.root)
    meta = store.get(plan.slug).meta
    meta.icu_athlete_id = synced_athlete_id(settings)
    store.save_meta(meta)
    if not store.default():
        store.set_default(plan.slug)
    return store.get(plan.slug)


# ---------------------------------------------------------------------------------- status


@dataclass(frozen=True)
class ProfileStatus:
    """One row of ``cyp profile list`` / ``show``."""

    slug: str
    display_name: str
    default: bool
    icu_athlete_id: str | None
    planner_mode: str | None
    key_set: bool
    last_run: str | None
    last_run_status: str | None
    garmin_upload_workouts: bool | None


def profile_status(store: FilesystemProfileStore, profile: Profile) -> ProfileStatus:
    """Status from files and the profile DB (offline; never raises for a broken DB)."""
    settings = profile_settings(profile.slug, store.root)
    last_run = last_status = None
    garmin: bool | None = None
    if (settings.cyp_data_dir / "cyp.sqlite").is_file():
        engine = engine_from_settings(settings)
        try:
            with session_factory(engine)() as s:
                row = s.scalars(
                    select(JobRun)
                    .where(JobRun.job == AUTOPILOT_JOB)
                    .order_by(JobRun.id.desc())
                    .limit(1)
                ).first()
                if row is not None:
                    last_run, last_status = _local_minute(row.started_at, settings), row.status
                athlete = s.scalars(select(Athlete).limit(1)).first()
                if athlete is not None and athlete.raw_intervals_json:
                    upload = athlete.raw_intervals_json.get("icu_garmin_upload_workouts")
                    garmin = None if upload is None else bool(upload)
        except SQLAlchemyError:
            last_status = "unreadable"
        finally:
            engine.dispose()
    return ProfileStatus(
        slug=profile.slug,
        display_name=profile.meta.display_name,
        default=store.default() == profile.slug,
        icu_athlete_id=profile.meta.icu_athlete_id,
        planner_mode=profile.planner_mode(),
        key_set=bool(settings.intervals_api_key.get_secret_value()),
        last_run=last_run,
        last_run_status=last_status,
        garmin_upload_workouts=garmin,
    )


def _local_minute(started_at: str, settings: Settings) -> str:
    """``job_runs.started_at`` (UTC ISO) as local ``YYYY-MM-DD HH:MM``."""
    from zoneinfo import ZoneInfo

    try:
        when = dt.datetime.fromisoformat(str(started_at).replace("Z", "+00:00"))
    except ValueError:
        return str(started_at)[:16]
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt.UTC)
    return when.astimezone(ZoneInfo(settings.cyp_timezone)).strftime("%Y-%m-%d %H:%M")


def synced_athlete_id(settings: Settings) -> str | None:
    """The icu athlete id the profile DB last synced (``None`` without a DB / sync)."""
    if not (settings.cyp_data_dir / "cyp.sqlite").is_file():
        return None
    engine = engine_from_settings(settings)
    try:
        with session_factory(engine)() as s:
            return SyncCursorRepo(s).get(ICU_SOURCE, CURSOR_ATHLETE_ID)
    except SQLAlchemyError:
        return None
    finally:
        engine.dispose()
