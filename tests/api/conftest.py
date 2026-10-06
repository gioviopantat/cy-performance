"""A seeded synthetic store (built once per session, copied per test) and an API client."""

from __future__ import annotations

import datetime as dt
import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from cyp.api.app import create_app
from cyp.dataset import CACHE
from cyp.devtools.synthetic import seed_synthetic
from cyp.services.context import AppContext
from cyp.settings import Settings
from cyp.store.db import make_engine, session_factory
from cyp.store.migrate import upgrade_head
from cyp.store.streams import StreamStore

REPO = Path(__file__).resolve().parents[2]
TODAY = dt.date(2026, 10, 6)  # season week 1, Tuesday
NOW = dt.datetime(2026, 10, 6, 7, 0)


@pytest.fixture(scope="session")
def seeded_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("seeded") / "data"
    url = f"sqlite:///{root / 'cyp.sqlite'}"
    upgrade_head(url)
    engine = make_engine(url)
    seed_synthetic(
        session_factory(engine),
        StreamStore(root / "streams"),
        days=70,
        end=TODAY - dt.timedelta(days=1),
    )
    engine.dispose()
    return root


def synthetic_athlete_config(data: Path) -> Path:
    """The repo athlete.yaml without ``data_quality``.

    Its entries describe the real athlete's power-meter history (dates in mid-2026) and would
    flag the synthetic rides, which carry no such problem.
    """
    cfg = yaml.safe_load((REPO / "config" / "athlete.yaml").read_text(encoding="utf-8"))
    cfg.pop("data_quality", None)
    path = data / "athlete.synthetic.yaml"
    path.write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return path


def make_ctx(data: Path, **settings: object) -> AppContext:
    s = Settings(cyp_data_dir=data, cyp_db_url=f"sqlite:///{data / 'cyp.sqlite'}", **settings)  # type: ignore[arg-type]
    c = AppContext.from_settings(s, athlete_config=synthetic_athlete_config(data))
    c.fixed_now = NOW
    return c


@pytest.fixture
def data(seeded_dir: Path, tmp_path: Path) -> Path:
    target = tmp_path / "data"
    shutil.copytree(seeded_dir, target)
    CACHE.clear()
    return target


@pytest.fixture
def ctx(data: Path) -> Iterator[AppContext]:
    c = make_ctx(data)
    yield c
    c.close()


@pytest.fixture
def client(ctx: AppContext) -> Iterator[TestClient]:
    with TestClient(create_app(ctx)) as cl:
        yield cl
