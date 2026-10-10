"""Profile commands (ADR-0006): ``profile list|show|add|adopt|promote|demote|use``, ``features``.

Thin: prompts and printing only; the work is in :mod:`cyp.services.profiles`. ``profile add``
asks only what intervals.icu cannot answer (docs/onboarding-questions.md).
"""

from __future__ import annotations

import contextlib
from dataclasses import asdict
from pathlib import Path
from typing import Annotated, Literal

import typer

from cyp.cli.common import JsonOpt, echo_json, fail
from cyp.core.errors import ConfigError, CypError
from cyp.profiles import FilesystemProfileStore, Profile, open_store, set_planner_mode
from cyp.services import profiles as svc
from cyp.settings import (
    Settings,
    get_settings,
    load_athlete_config,
    profile_settings,
    resolve_features,
)

profile_app = typer.Typer(help="Athlete profiles: one workspace per athlete.", no_args_is_help=True)

YesOpt = Annotated[bool, typer.Option("--yes", "-y", help="Do not ask for confirmation.")]


def _store() -> FilesystemProfileStore:
    # Root settings only: the profiles directory must not depend on a selected profile.
    return open_store(Settings().cyp_profiles_dir)


def _get(store: FilesystemProfileStore, slug: str) -> Profile:
    try:
        return store.get(slug)
    except ConfigError as exc:
        fail(str(exc), code=2)


# ------------------------------------------------------------------------------- list / show


@profile_app.command("list")
def list_profiles(as_json: JsonOpt = False) -> None:
    """Every profile with its athlete id, planner mode and last autopilot run."""
    store = _store()
    rows = [svc.profile_status(store, p) for p in store.list()]
    if as_json:
        echo_json([asdict(r) for r in rows])
        return
    if not rows:
        typer.echo("no profiles yet: `cyp profile add <name>` or `cyp profile adopt <name>`")
        return
    for r in rows:
        last = f"{r.last_run} {r.last_run_status}" if r.last_run else "never"
        typer.echo(
            f"{'*' if r.default else ' '} {r.slug:<10s} {r.display_name:<12s} "
            f"{r.icu_athlete_id or '?':<10s} mode={r.planner_mode or 'invalid'}  last run: {last}"
        )


@profile_app.command("show")
def show(slug: Annotated[str, typer.Argument(help="Profile name.")]) -> None:
    """Paths, metadata, planner mode, Garmin push and feature flags of one profile."""
    store = _store()
    p = _get(store, slug)
    st = svc.profile_status(store, p)
    garmin = {True: "on", False: "OFF (enable in intervals.icu)", None: "unknown (sync first)"}
    typer.echo(f"profile {p.slug}: {p.meta.display_name}")
    typer.echo(f"  icu athlete id : {p.meta.icu_athlete_id or 'unknown'}")
    typer.echo(f"  created        : {p.meta.created}")
    typer.echo(f"  directory      : {p.root}")
    typer.echo(f"  planner mode   : {st.planner_mode or 'athlete.yaml invalid'}")
    typer.echo(f"  icu API key    : {'set' if st.key_set else 'MISSING'}")
    typer.echo(f"  garmin upload  : {garmin[st.garmin_upload_workouts]}")
    last = f"{st.last_run} {st.last_run_status}" if st.last_run else "never"
    typer.echo(f"  last run       : {last}")
    _echo_features(profile_settings(slug, store.root), p.athlete_config_path)


# ------------------------------------------------------------------------------------- add


@profile_app.command("add")
def add(
    slug: Annotated[str, typer.Argument(help="Short name, e.g. dad.")],
    key: Annotated[
        str | None,
        typer.Option("--key", help="intervals.icu API key (else profiles/<name>/.env)."),
    ] = None,
    name: Annotated[str | None, typer.Option("--name", help="Display name.")] = None,
    goal_ftp: Annotated[int | None, typer.Option("--goal-ftp", help="Target FTP (W).")] = None,
    weeks: Annotated[int, typer.Option("--weeks", min=8, max=52, help="Season length.")] = 26,
    availability: Annotated[
        str | None,
        typer.Option("--availability", help=f"Minutes per day, e.g. {svc.DEFAULT_AVAILABILITY}."),
    ] = None,
    backfill_days: Annotated[
        int, typer.Option("--backfill-days", min=0, help="History to download (0 = skip).")
    ] = 365,
    birth_year: Annotated[
        int | None, typer.Option("--birth-year", help="Question 1 (drives age defaults).")
    ] = None,
    health: Annotated[
        str | None,
        typer.Option(
            "--health",
            help="Question 2: none, or comma-separated heart_or_bp, symptoms_on_exertion, "
            "hr_medication, injury.",
        ),
    ] = None,
    distance_km: Annotated[
        int | None,
        typer.Option("--distance-km", help="Question 3 as a distance goal, e.g. 150."),
    ] = None,
    yes: YesOpt = False,
) -> None:
    """Create a profile from an intervals.icu API key (the only thing the athlete provides).

    Questions: docs/onboarding-questions.md. With --yes, unanswered questions stay unset.
    """
    store = _store()
    try:
        key = key or svc.existing_env(store, slug).get("INTERVALS_API_KEY")
        key = key or typer.prompt("intervals.icu API key", hide_input=True)
        athlete = svc.fetch_icu_athlete(key)
    except ConfigError as exc:
        fail(str(exc), code=2)
    typer.echo(
        f"key belongs to {athlete.name or athlete.id} ({athlete.id}): FTP {athlete.ftp_w} W, "
        f"{athlete.weight_kg} kg, {athlete.timezone}"
    )
    if athlete.garmin_upload_workouts is False:
        typer.echo(
            "NOTE: intervals.icu is not allowed to upload planned workouts to Garmin; enable it "
            "in intervals.icu Settings -> Garmin, or workouts stay off the head unit"
        )
    default_goal = svc.default_goal_ftp(athlete.ftp_w or 200)
    try:
        answers = svc.OnboardingAnswers(
            display_name=name or (slug if yes else typer.prompt("display name", default=slug)),
            goal_ftp=goal_ftp
            or (default_goal if yes else typer.prompt("goal FTP (W)", default=default_goal)),
            availability=svc.parse_availability(
                availability
                or (
                    svc.DEFAULT_AVAILABILITY
                    if yes
                    else typer.prompt("minutes per weekday", default=svc.DEFAULT_AVAILABILITY)
                )
            ),
            weeks=weeks,
            birth_year=birth_year
            or (
                None
                if yes
                else typer.prompt("birth year (blank = skip)", default=0, show_default=False)
                or None
            ),
            health_flags=svc.parse_health(health)
            if health is not None
            else (
                None
                if yes
                else svc.parse_health(
                    typer.prompt(
                        "health screen: none or " + ",".join(svc.HEALTH_FLAGS), default="none"
                    )
                )
            ),
            distance_km=distance_km
            or (None if yes else typer.prompt("distance goal km (0 = none)", default=0) or None),
        )
        profile = svc.onboard(store, slug, key=key, athlete=athlete, answers=answers)
    except ConfigError as exc:
        fail(str(exc), code=2)
    typer.echo(f"profile {slug} created in {profile.root} (planner mode: propose)")
    typer.echo(f"default profile: {store.default()}")
    if backfill_days:
        typer.echo(f"downloading {backfill_days} days of history (resumable)...")
        try:
            got = svc.backfill(profile_settings(slug, store.root), backfill_days)
            typer.echo(f"history ready: {got.activities} activities, {got.streams} with streams")
        except CypError as exc:
            typer.echo(
                f"backfill stopped: {exc} (resume: cyp --profile {slug} backfill "
                f"--days {backfill_days})",
                err=True,
            )
    typer.echo(
        f"next: `cyp --profile {slug} run --no-write` to review the plan, then "
        f"`cyp profile promote {slug}` to let the autopilot write to the calendar"
    )


# ----------------------------------------------------------------------------------- adopt


@profile_app.command("adopt")
def adopt(
    slug: Annotated[str, typer.Argument(help="Name for the existing single-athlete setup.")],
    name: Annotated[str | None, typer.Option("--name", help="Display name.")] = None,
    yes: YesOpt = False,
) -> None:
    """Move the legacy layout (.env, athlete config, data dir) into profiles/<slug>/."""
    store = _store()
    try:
        plan = svc.plan_adopt(store, slug, Settings())
    except ConfigError as exc:
        fail(str(exc), code=2)
    typer.echo(f"will move into {plan.root}/:")
    typer.echo(f"  {plan.env} -> .env" if plan.env else "  (no .env)")
    typer.echo(f"  {plan.data}/ -> data/" if plan.data else "  (no data dir)")
    if plan.config_is_target_link:
        typer.echo(f"  {plan.config} (symlink to the target) removed")
    elif plan.config:
        typer.echo(f"  {plan.config} -> athlete.yaml")
    if not yes and not typer.confirm("proceed?", default=False):
        raise typer.Exit(code=1)
    try:
        profile = svc.adopt_legacy(store, plan, name or slug)
    except CypError as exc:
        fail(f"adopt failed: {exc}", code=1)
    typer.echo(
        f"adopted as profile {slug} "
        f"(icu athlete {profile.meta.icu_athlete_id or 'unknown: run a sync, then set it'})"
    )
    typer.echo(f"default profile: {store.default()}")


# ------------------------------------------------------------------- promote / demote / use


def _set_mode(slug: str, mode: Literal["propose", "apply"], yes: bool) -> None:
    p = _get(_store(), slug)
    if mode == "apply":
        if not p.meta.icu_athlete_id:
            fail(f"{slug}: icu_athlete_id unknown in profile.yaml; set it first", code=2)
        typer.echo(
            f"apply: the autopilot will WRITE workouts to {p.meta.display_name}'s intervals.icu "
            f"calendar ({p.meta.icu_athlete_id}) every run"
        )
        if not yes and not typer.confirm("proceed?", default=False):
            raise typer.Exit(code=1)
    try:
        set_planner_mode(p.athlete_config_path, mode)
    except ConfigError as exc:
        fail(str(exc), code=2)
    typer.echo(f"{slug}: planner.mode = {mode}")


@profile_app.command("promote")
def promote(slug: Annotated[str, typer.Argument()], yes: YesOpt = False) -> None:
    """Let the autopilot write this profile's plan to its calendar (planner.mode=apply)."""
    _set_mode(slug, "apply", yes)


@profile_app.command("demote")
def demote(slug: Annotated[str, typer.Argument()]) -> None:
    """Back to propose: the autopilot only computes the diff."""
    _set_mode(slug, "propose", True)


@profile_app.command("use")
def use(slug: Annotated[str, typer.Argument()]) -> None:
    """Make ``slug`` the default profile (used when --profile / CYP_PROFILE are absent)."""
    try:
        _store().set_default(slug)
    except ConfigError as exc:
        fail(str(exc), code=2)
    typer.echo(f"default profile: {slug}")


# -------------------------------------------------------------------------------- features


def _echo_features(settings: Settings, cfg_path: Path) -> None:
    # Display only: resolve_features directly (ADR-0008 allows it for read-only listings).
    try:
        cfg = load_athlete_config(cfg_path)
    except ConfigError:
        cfg = None
    typer.echo("  features:")
    for st in resolve_features(settings, cfg):
        flag = "on " if st.on else "off"
        why = f" (needs {', '.join(st.blocked_by)})" if st.blocked_by else ""
        typer.echo(f"    {flag} {st.feature.id:<18s} {st.source:<13s} {st.feature.title_zh}{why}")


def features(as_json: JsonOpt = False) -> None:
    """Feature flags of the selected profile: value, where it came from, what it does."""
    try:
        settings = get_settings()
        cfg = None
        with contextlib.suppress(ConfigError):
            cfg = load_athlete_config(settings.cyp_athlete_config)
        resolved = resolve_features(settings, cfg)
    except ConfigError as exc:
        fail(str(exc), code=2)
    if as_json:
        echo_json(
            [
                {
                    "id": st.feature.id,
                    "on": st.on,
                    "source": st.source,
                    "blocked_by": list(st.blocked_by),
                    "stage": st.feature.stage,
                    "title_zh": st.feature.title_zh,
                    "summary": st.feature.summary,
                }
                for st in resolved
            ]
        )
        return
    typer.echo(f"profile: {settings.cyp_profile or '(legacy layout)'}")
    _echo_features(settings, settings.cyp_athlete_config)


def register(app: typer.Typer) -> None:
    """Attach ``profile`` and ``features`` to ``app``."""
    app.add_typer(profile_app, name="profile")
    app.command()(features)
