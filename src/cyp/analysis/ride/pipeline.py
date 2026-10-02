"""Pure per-ride pipeline: ``(RideFrame, RideInputs) -> RideMetrics`` (docs/04 §2).

No I/O here; :mod:`cyp.analysis.run` loads the frame and resolves the athlete inputs.
Bump :data:`ALGO_VERSION` whenever any metric definition changes so stored rows recompute.
"""

from __future__ import annotations

from typing import Any

from cyp.analysis.longitudinal.durability import ride_durability_json
from cyp.analysis.ride import climbs as climbs_mod
from cyp.analysis.ride import durability, efforts, estimate, power
from cyp.analysis.ride.classify import classify_ride
from cyp.analysis.ride.explain import explain_ride
from cyp.analysis.ride.frames import FloatArray, RideFrame, elevation_gain
from cyp.analysis.ride.result import RideInputs, RideMetrics, TssSource

ALGO_VERSION = "ride-1.1.0"  # 1.1: per-ride durability (EF by kJ bucket)


def _pacing(frame: RideFrame, p_rec: FloatArray | None, pm: power.PowerMetrics) -> dict[str, Any]:
    out: dict[str, Any] = {"vi": None, "cv": None, "split_pct": None}
    if p_rec is None or p_rec.size == 0:
        return out
    mean = float(p_rec.mean())
    out["vi"] = round(pm.vi, 3) if pm.vi is not None else None
    out["cv"] = round(float(p_rec.std() / mean), 3) if mean > 0 else None
    mp = power.moving_power(frame)
    if mp is not None and mp.size >= 4:
        half = mp.size // 2
        a, b = float(mp[:half].mean()), float(mp[half:].mean())
        if a > 0:
            out["split_pct"] = round((b - a) / a * 100.0, 1)  # + = negative split (faster)
    return out


def _comparison(
    m: RideMetrics,
    inputs: RideInputs,
    pm: power.PowerMetrics,
    dec: durability.Decoupling | None,
) -> dict[str, Any]:
    comp: dict[str, Any] = {
        "ftp_used": inputs.ftp,
        "ftp_source": inputs.ftp_source,
        "icu_ftp": inputs.icu_ftp,
        "icu_training_load": inputs.icu_training_load,
        "tss_ours": round(m.tss, 1) if m.tss is not None else None,
        "tss_source": m.tss_source,
        "icu_np_w": inputs.icu_np_w,
        "np_ours": round(m.np_w, 1) if m.np_w is not None else None,
        "np_recorded_w": round(pm.np_recorded_w, 1) if pm.np_recorded_w is not None else None,
        "icu_decoupling": inputs.icu_decoupling,
        "decoupling_ours": round(dec.pct, 2) if dec is not None else None,
    }
    if m.tss is not None and inputs.icu_training_load is not None:
        delta = m.tss - inputs.icu_training_load
        comp["tss_delta"] = round(delta, 1)
        comp["tss_rel_err"] = (
            round(delta / inputs.icu_training_load, 4) if inputs.icu_training_load else None
        )
    if m.np_w is not None and inputs.icu_np_w and m.tss_source == "power":
        delta = m.np_w - inputs.icu_np_w
        comp["np_delta"] = round(delta, 1)
        comp["np_rel_err"] = round(delta / inputs.icu_np_w, 4)
    return comp


def compute_ride_metrics(frame: RideFrame, inputs: RideInputs) -> RideMetrics:
    """Run every per-ride metric and attach the Explanation."""
    th = inputs.thresholds
    use_power = inputs.has_power and frame.has("watts")
    est: estimate.EstimateResult | None = None
    watts: FloatArray | None = None
    basis: durability.Basis = "power"
    if use_power:
        watts = frame.watts
    else:
        est = estimate.estimate_for_ride(
            frame,
            weight_kg=inputs.weight_kg,
            lthr=inputs.lthr,
            max_hr=inputs.max_hr,
            resting_hr=inputs.resting_hr,
        )
        watts = est.power_est
        basis = "estimated"

    pm = power.compute_power_metrics(
        frame,
        ftp=inputs.ftp,
        weight_kg=inputs.weight_kg,
        zones=inputs.power_zones,
        cp=inputs.cp if use_power else None,
        w_prime=inputs.w_prime if use_power else None,
        watts=watts,
        basis="power" if use_power else "estimated",
    )

    # TSS source: measured power > hrTSS > physics estimate.
    tss_source: TssSource
    tss: float | None
    if use_power:
        tss_source, tss = "power", pm.tss
    elif est is not None and est.hr_tss is not None:
        tss_source, tss = "hr", est.hr_tss
    else:
        tss_source, tss = "estimated", pm.tss

    m = RideMetrics(
        activity_id=inputs.activity_id,
        algo_version=ALGO_VERSION,
        tss_source=tss_source,
        tss=tss,
        moving_s=frame.moving_s,
        recording_s=frame.recording_s,
        elapsed_s=frame.elapsed_s,
    )
    # Power figures are only "ours" when measured; estimated ones live in estimated_power_meta.
    if use_power:
        m.np_w, m.if_, m.vi = pm.np_w, pm.if_, pm.vi
        m.avg_w, m.max_w, m.kj = pm.avg_w, pm.max_w, pm.kj
        m.power_curve = {str(k): v for k, v in pm.power_curve.items()}
        m.power_curve_wkg = {str(k): v for k, v in pm.power_curve_wkg.items()}
        m.time_in_zone_power = pm.time_in_zone
        m.wbal_min_j = pm.wbal_min_j
    elif est is not None:
        meta = est.to_meta()
        meta.update(
            {
                "np_est_w": round(pm.np_w, 1) if pm.np_w is not None else None,
                "if_est": round(pm.if_, 3) if pm.if_ is not None else None,
                "tss_est": round(pm.tss, 1) if pm.tss is not None else None,
                "avg_est_w": round(pm.avg_w, 1) if pm.avg_w is not None else None,
                "kj_est": round(pm.kj, 1) if pm.kj is not None else None,
                "time_in_zone_est": pm.time_in_zone,
                "power_curve_est": {str(k): round(v, 1) for k, v in pm.power_curve.items()},
            }
        )
        m.estimated_power_meta = meta
        if tss_source == "estimated":
            m.np_w, m.if_ = pm.np_w, pm.if_

    # HR side
    m.avg_hr = durability.average_hr(frame)
    m.max_hr = durability.max_hr(frame)
    m.time_in_zone_hr = durability.hr_time_in_zones(frame, inputs.hr_zones)
    if use_power:
        m.ef = durability.efficiency_factor(pm.np_w, m.avg_hr)

    output: FloatArray | None = watts
    if output is None and frame.speed is not None:
        output, basis = frame.speed, "speed"
    dec = durability.decoupling(frame, output, basis=basis, if_=pm.if_, vi=pm.vi, thresholds=th)
    if dec is not None:
        m.decoupling_pct = dec.pct
        m.decoupling_reliable = dec.reliable
        m.hr_drift_detail = dec.to_json()
    lag = durability.hr_lag(frame, watts, thresholds=th) if watts is not None else None
    if lag is not None:
        m.hr_lag_s = float(lag.lag_s)
        m.hr_lag_corr = lag.corr
        if m.hr_drift_detail is None:
            m.hr_drift_detail = {}
        m.hr_drift_detail["hr_lag"] = lag.to_json()

    # Durability: EF per kJ bucket at endurance intensity (read by the trends job).
    if use_power and inputs.ftp and frame.hr is not None:
        m.durability = ride_durability_json(frame, inputs.ftp)

    # Terrain + efforts
    m.climbs = climbs_mod.detect_climbs(frame, weight_kg=inputs.weight_kg, watts=watts)
    alt_s = frame.smoothed_altitude()
    m.elev_gain_m = elevation_gain(alt_s) if alt_s is not None else None
    if use_power:
        sprints = efforts.detect_sprints(
            frame, ftp=inputs.ftp, weight_kg=inputs.weight_kg, zones=inputs.power_zones
        )
        sustained = efforts.detect_efforts(
            frame, ftp=inputs.ftp, weight_kg=inputs.weight_kg, zones=inputs.power_zones
        )
        m.efforts = sorted(sustained + sprints, key=lambda e: e["start_s"])
    p_rec = power.recorded_power(frame, watts) if use_power else None
    m.pacing = _pacing(frame, p_rec, pm)
    m.pacing["surges"] = sum(1 for e in m.efforts if e["kind"] == "sprint")
    m.pacing["structure"] = efforts.effort_structure(m.efforts)
    m.pacing["elev_gain_m"] = round(m.elev_gain_m, 1) if m.elev_gain_m is not None else None

    # Classification + verdict
    if_for_class = pm.if_ if (use_power or tss_source == "estimated") else None
    if if_for_class is None and tss_source == "hr" and inputs.lthr and m.avg_hr:
        if_for_class = m.avg_hr / inputs.lthr  # HR-based intensity proxy
    m.classification = classify_ride(
        pm.time_in_zone, if_for_class, moving_s=frame.moving_s, race=inputs.race
    )
    m.pacing["classification"] = m.classification
    m.status, m.next_recommendation = durability.classify_status(
        decoupling_result=dec, lag=lag, tss=m.tss, moving_s=frame.moving_s, thresholds=th
    )

    m.comparison = _comparison(m, inputs, pm, dec)
    m.explanation = explain_ride(m, inputs)
    return m
