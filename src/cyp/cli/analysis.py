"""Analysis commands: ``analyze``, ``trends``, ``readiness``, ``ftp`` (+ ``ftp accept``)."""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Any

import typer
from sqlalchemy import func, select

from cyp.cli.common import (
    SERVICE_ERRORS,
    AthleteConfigOpt,
    DateOpt,
    JsonOpt,
    app_context,
    cli_settings,
    echo_json,
    parse_day,
    today,
)
from cyp.schemas import FtpStatusOut, ReadinessOut
from cyp.services import ftp as ftp_service
from cyp.services import readiness as readiness_service
from cyp.services import trends as trends_service
from cyp.services.context import AppContext

ftp_app = typer.Typer(
    help="FTP evidence (CP fits, estimates, proposal) and the explicit accept.",
    invoke_without_command=True,
)


def analyze(
    force: Annotated[
        bool, typer.Option("--force", help="Recompute every ride with streams, ignoring versions.")
    ] = False,
    limit: Annotated[
        int | None, typer.Option("--limit", min=1, help="Analyse at most N activities.")
    ] = None,
    activity: Annotated[
        list[int] | None,
        typer.Option("--activity", help="Analyse only these activity ids (repeatable)."),
    ] = None,
    rides_only: Annotated[
        bool,
        typer.Option("--rides-only", help="Stop after per-ride metrics (skip trends + readiness)."),
    ] = False,
    athlete_config: AthleteConfigOpt = None,
) -> None:
    """Per-ride metrics, then longitudinal trends, then today's readiness."""
    from cyp.analysis.run import ALGO_VERSION, analyze_pending

    settings = cli_settings()
    with app_context(settings, athlete_config) as ctx:
        summary = analyze_pending(
            ctx.factory,
            store=ctx.store,
            limit=limit,
            force=force,
            activity_ids=activity,
            data_quality=ctx.data_quality(),
            log_path=str(settings.logs_dir / "cyp.jsonl"),
        )
        typer.echo(f"analyze (algo {ALGO_VERSION}, run {summary.run_id})")
        for key, value in sorted(summary.counts().items()):
            typer.echo(f"  {key}: {value}")
        for r in summary.results:
            if r.outcome == "analyzed" and r.metrics is not None:
                m = r.metrics
                tss = f"{m.tss:.0f}" if m.tss is not None else "-"
                np_w = f"{m.np_w:.0f}" if m.np_w is not None else "-"
                typer.echo(
                    f"  ride:{r.activity_id}  TSS {tss} ({m.tss_source})  NP {np_w}  "
                    f"{m.classification}  {m.status}/{m.next_recommendation}"
                )
            elif r.outcome == "failed":
                typer.echo(f"  ride:{r.activity_id}  FAILED {r.error}")
        if not rides_only:
            day = today(settings)
            echo_trends(run_trends(ctx, day))
            echo_readiness(run_readiness(ctx, [day]))
    if any(r.outcome == "failed" for r in summary.results):
        raise typer.Exit(code=1)


# ------------------------------------------------------------------------------------ trends


def run_trends(ctx: AppContext, day: dt.date) -> dict[str, Any]:
    """``services.trends.recompute``; exits 1 with ``trends failed: ...`` on errors."""
    try:
        return trends_service.recompute(ctx, as_of=day)
    except SERVICE_ERRORS as exc:
        typer.echo(f"trends failed: {exc}", err=True)
        raise typer.Exit(code=1) from exc


def _proposal_line(fp: dict[str, Any], current: float | None) -> str:
    """``FTP proposal: ...`` (proposed / withheld / none)."""
    cur = fp.get("current_ftp", current)
    if fp.get("proposed_ftp"):
        cur_s = f"{cur:.0f}" if cur is not None else "-"
        return (
            f"FTP proposal: {cur_s} -> {fp['proposed_ftp']:.0f} W "
            f"({fp['change_pct']:+.1f} %, {fp['days_sustained']} d) — NOT applied"
        )
    if fp.get("insufficient_evidence"):
        return (
            f"FTP proposal: withheld — estimates down for {fp['days_sustained']} d but no "
            "near-maximal effort in the last 42 d (近 42 天沒有接近極限的長時間努力，無法判斷)"
        )
    if fp.get("unsupported"):
        best = fp.get("best_20min_w")
        best_s = f"{best:.0f} W" if best is not None else "-"
        return (
            f"FTP proposal: withheld — estimates up for {fp['days_sustained']} d but best "
            f"20 min {best_s} does not support it"
        )
    return f"FTP proposal: none ({fp['days_sustained']} d beyond ±3 %)"


def _fit_line(window: str, model: str, fit: dict[str, Any]) -> str:
    diff = fit.get("cp_diff_vs_icu_pct")
    vs = f"  vs icu {diff:+.1f} %" if diff is not None else ""
    pmax = f"  Pmax {fit['p_max']}" if fit.get("p_max") else ""
    return (
        f"{window} {model}: CP {fit['cp']:.0f} W  W' {fit['w_prime'] / 1000:.1f} kJ"
        f"{pmax}  r2 {fit['r2']}{vs}"
    )


def echo_trends(report: dict[str, Any]) -> None:
    """Summary lines of a trends report dict (``TrendsReport.to_json()``)."""
    typer.echo(f"trends as of {report['as_of']} (FTP {report.get('ftp') or '-'} W)")
    p = report.get("pmc_today")
    if p:
        icu = f"  icu CTL {p['ctl_icu']:.1f}" if p.get("ctl_icu") is not None else ""
        acwr = f"{p['acwr_7_28']:.2f}" if p.get("acwr_7_28") is not None else "-"
        ramp = f"{p['ramp_rate']:+.1f}" if p.get("ramp_rate") is not None else "-"
        typer.echo(
            f"  PMC  CTL {p['ctl']:.1f}  ATL {p['atl']:.1f}  TSB {p['tsb']:+.1f}  "
            f"ramp {ramp}  ACWR {acwr}{icu}"
        )
    a = report.get("pmc_agreement")
    if a:
        flag = "OK" if a["within_tolerance"] else "OUT OF ±1"
        typer.echo(
            f"  PMC vs icu ({a['decay']}): max |ΔCTL| {a['max_abs_ctl_err']:.2f} over "
            f"{a['n_days']} d  [{flag}]"
        )
    for window, entry in (report.get("cp_fits") or {}).items():
        for model in ("cp_2p", "cp_3p"):
            fit = entry.get(model)
            if fit:
                typer.echo(f"  {_fit_line(window, model, fit)}")
    fp = report.get("ftp_proposal")
    if fp:
        typer.echo(f"  {_proposal_line(fp, report.get('ftp'))}")
    blocks = [b for b in report.get("durability_blocks", []) if b.get("median_ratio")]
    if blocks:
        b = blocks[-1]
        typer.echo(
            f"  durability (EF late/fresh, {b['end']}): {b['median_ratio']:.3f} "
            f"over {b['n_long']} long rides"
        )
    if report.get("tid_weeks"):
        w = report["tid_weeks"][-1]
        typer.echo(
            f"  TID week {w['week_start']}: {w['low'] * 100:.0f}/{w['mid'] * 100:.0f}/"
            f"{w['high'] * 100:.0f} %  {w['hours']} h  {w['model']}"
        )
    climbs = report.get("climbs") or []
    if climbs:
        typer.echo(f"  repeat climbs: {len(climbs)} (top: {climbs[0]['n']} efforts)")
    for lim in report.get("limiters", []):
        typer.echo(f"  limiter {lim['id']} ({lim['severity']:.2f}): {lim['title_zh']}")
    for chk in report.get("limiter_checks", []):
        typer.echo(f"  limiter {chk['id']}: {chk['status']} — {chk['reason_zh']}")
    pu = report.get("power_unreliable")
    if pu:
        typer.echo(
            f"  power_unreliable: {pu['n_rides']} rides {pu['first']}..{pu['last']} "
            "excluded from power models"
        )


def trends(
    date: DateOpt = None,
    as_json: JsonOpt = False,
    athlete_config: AthleteConfigOpt = None,
) -> None:
    """Longitudinal trends: PMC replay vs icu, CP/W', FTP proposal, durability, TID, limiters."""
    settings = cli_settings()
    day = parse_day(settings, date)
    with app_context(settings, athlete_config) as ctx:
        report = run_trends(ctx, day)
    if as_json:
        echo_json(report)
    else:
        echo_trends(report)


# --------------------------------------------------------------------------------- readiness


def run_readiness(ctx: AppContext, days: list[dt.date]) -> list[ReadinessOut]:
    """``services.readiness.recompute``; exits 1 with ``readiness failed: ...`` on errors."""
    try:
        return readiness_service.recompute(ctx, days)
    except SERVICE_ERRORS as exc:
        typer.echo(f"readiness failed: {exc}", err=True)
        raise typer.Exit(code=1) from exc


def echo_readiness(verdicts: list[ReadinessOut]) -> None:
    """One line per verdict plus its zh-TW headline."""
    for r in verdicts:
        missing = ",".join(r.missing) or "-"
        typer.echo(
            f"readiness {r.date}: {r.score:.0f} {r.status}/{r.recommendation}  (missing: {missing})"
        )
        if r.explanation is not None:
            typer.echo(f"  {r.explanation.headline_zh}")


def _wellness_coverage(ctx: AppContext, day: dt.date) -> tuple[int, dict[str, int]]:
    """``(rows, non-null count per wellness field)`` over the 60 days up to ``day``."""
    from cyp.analysis.readiness import WELLNESS_FIELDS, wellness_coverage
    from cyp.store.models import WellnessDaily

    with ctx.factory() as s:
        rows = s.scalars(
            select(WellnessDaily).where(
                WellnessDaily.date_local > day - dt.timedelta(days=60),
                WellnessDaily.date_local <= day,
            )
        ).all()
        dicts = [{f: getattr(w, f) for f in WELLNESS_FIELDS} for w in rows]
    return len(rows), dict(wellness_coverage(dicts))


def readiness(
    date: DateOpt = None,
    days: Annotated[
        int, typer.Option("--days", min=1, help="Also (re)compute the N-1 days before --date.")
    ] = 1,
    coverage: Annotated[
        bool, typer.Option("--coverage", help="Show which wellness fields the store holds.")
    ] = False,
    as_json: Annotated[
        bool,
        typer.Option(
            "--json",
            help="Print the verdicts as a JSON list (with --coverage: an object with both).",
        ),
    ] = False,
) -> None:
    """Daily readiness verdict (rules + weighted z-scores) with its explanation."""
    settings = cli_settings()
    day = parse_day(settings, date)
    with app_context(settings) as ctx:
        cov = _wellness_coverage(ctx, day) if coverage else None
        if cov is not None and not as_json:
            typer.echo(f"wellness coverage (last 60 d, {cov[0]} rows):")
            for f, n in cov[1].items():
                typer.echo(f"  {f:14s} {n:3d}")
        verdicts = run_readiness(ctx, [day - dt.timedelta(days=i) for i in range(days - 1, -1, -1)])
    if not as_json:
        echo_readiness(verdicts)
    elif cov is not None:
        echo_json(
            {
                "coverage": {"rows": cov[0], "fields": cov[1]},
                "readiness": [v.model_dump(mode="json") for v in verdicts],
            }
        )
    else:
        echo_json(verdicts)


# --------------------------------------------------------------------------------------- ftp


def echo_ftp(out: FtpStatusOut, *, what_if: float | None = None) -> None:
    """Text rendering of an FTP status."""
    cur = f"{out.current_ftp:.0f} W" if out.current_ftp is not None else "- W"
    wkg = f"  {out.w_kg:.2f} W/kg" if out.w_kg is not None else ""
    note = f"  (what-if FTP {what_if:.0f} W)" if what_if else ""
    typer.echo(f"FTP as of {out.as_of}: {cur}{wkg}{note}")
    for name, win in out.windows.items():
        for model in ("cp_2p", "cp_3p"):
            fit = getattr(win, model)
            if fit is not None:
                typer.echo(f"  {_fit_line(name, model, fit.model_dump())}")
    latest = out.estimates[-1] if out.estimates else None
    est = f"; latest {latest.date} {latest.ftp:.0f} W" if latest else "; no estimates"
    typer.echo(f"  estimate source: {out.estimate_source}{est}")
    if out.proposal is None:
        typer.echo("  FTP proposal: none (not enough evidence)")
    else:
        typer.echo(f"  {_proposal_line(out.proposal.model_dump(), out.current_ftp)}")
    typer.echo(f"  compute_ms: {out.compute_ms}")


@ftp_app.callback()
def ftp(
    ctx: typer.Context,
    date: DateOpt = None,
    what_if: Annotated[
        float | None,
        typer.Option(
            "--what-if", min=50, max=700, help="Evaluate the evidence against this FTP (W)."
        ),
    ] = None,
    as_json: JsonOpt = False,
) -> None:
    """Current FTP, 42/90-day CP fits, estimates and the (never auto-applied) proposal."""
    if ctx.invoked_subcommand is not None:
        return
    settings = cli_settings()
    day = parse_day(settings, date)
    with app_context(settings) as actx:
        try:
            out = ftp_service.status(actx, as_of=day, ftp=what_if)
        except SERVICE_ERRORS as exc:
            typer.echo(f"ftp failed: {exc}", err=True)
            raise typer.Exit(code=1) from exc
    if as_json:
        echo_json(out)
    else:
        echo_ftp(out, what_if=what_if)


@ftp_app.command("accept")
def ftp_accept(
    watts: Annotated[float, typer.Argument(min=50, max=700, help="New FTP in watts.")],
    effective_from: Annotated[
        str | None,
        typer.Option("--from", help="Effective local date YYYY-MM-DD (default: today)."),
    ] = None,
    yes: Annotated[bool, typer.Option("--yes", help="Really record the new FTP.")] = False,
) -> None:
    """Record WATTS as your FTP (source manual) and queue affected rides for re-analysis."""
    from cyp.store.models import Activity

    settings = cli_settings()
    if effective_from is None:
        day = today(settings)
    else:
        try:
            day = dt.date.fromisoformat(effective_from)
        except ValueError as exc:
            typer.echo(f"invalid --from {effective_from!r}; expected YYYY-MM-DD", err=True)
            raise typer.Exit(code=2) from exc
    with app_context(settings) as ctx:
        if not yes:
            with ctx.factory() as s:
                n = s.scalar(
                    select(func.count())
                    .select_from(Activity)
                    .where(Activity.is_ride.is_(True), Activity.start_utc >= day.isoformat())
                )
            typer.echo(
                f"would record FTP {watts:.0f} W effective {day} (source manual) and mark "
                f"{n or 0} ride(s) from that day for re-analysis"
            )
            typer.echo("re-run with --yes to apply")
            raise typer.Exit(code=2)
        try:
            out = ftp_service.accept(ctx, watts, effective_from=day)
        except SERVICE_ERRORS as exc:
            typer.echo(f"ftp accept failed: {exc}", err=True)
            raise typer.Exit(code=1) from exc
    typer.echo(f"FTP {watts:.0f} W recorded from {day} (source manual)")
    typer.echo(
        "提醒：intervals.icu 的運動設定不會被修改，請自行到 intervals.icu 更新 FTP；"
        "執行 `cyp analyze` 會以新 FTP 重新計算受影響的騎乘。"
    )
    echo_ftp(out)


def register(app: typer.Typer) -> None:
    """Attach ``analyze``, ``trends``, ``readiness`` and ``ftp`` to ``app``."""
    app.command()(analyze)
    app.command()(trends)
    app.command()(readiness)
    app.add_typer(ftp_app, name="ftp")
