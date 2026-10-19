"""Profiles: who the server can show (spec web-ui)."""

from __future__ import annotations

from fastapi import APIRouter, Request

from cyp.api.registry import ProfileRegistry
from cyp.profiles import open_store
from cyp.schemas import ProfileOut
from cyp.services.profiles import profile_status

router = APIRouter(prefix="/profiles", tags=["profiles"])


@router.get("", response_model=list[ProfileOut])
def list_profiles(request: Request) -> list[ProfileOut]:
    """Every profile with its mode, last run and whether the UI may write its calendar."""
    registry: ProfileRegistry = request.app.state.profiles
    store = open_store(registry.default.settings.cyp_profiles_dir)
    out = []
    for p in store.list():
        st = profile_status(store, p)
        try:
            flags = registry.get(p.slug).features()
            allowed = flags.enabled("api.calendar_write") and st.planner_mode == "apply"
            strava = flags.enabled("strava.write_description")
        except Exception:  # noqa: BLE001 - a broken profile is listed, never writable
            allowed = strava = False
        out.append(
            ProfileOut(
                slug=st.slug,
                display_name=st.display_name,
                default=st.default,
                icu_athlete_id=st.icu_athlete_id,
                planner_mode=st.planner_mode,
                key_set=st.key_set,
                last_run=st.last_run,
                last_run_status=st.last_run_status,
                garmin_upload_workouts=st.garmin_upload_workouts,
                calendar_write_allowed=allowed,
                strava_write_allowed=strava,
            )
        )
    return out
