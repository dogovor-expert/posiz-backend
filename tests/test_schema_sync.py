"""Согласованность справочников: каталог ↔ ENUM в БД ↔ pydantic ↔ миграции.

Эти тесты ловят класс ошибок, которые не видны в юнит-тестах на памяти:
сохранили публикацию для Дзена — а в PostgreSQL ENUM из 15 значений, и INSERT падает
с ``invalid input value for enum channel_kind`` уже в проде.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from orbita.catalog import CATALOG
from orbita.schemas import CHANNEL_KINDS

BACKEND = Path(__file__).resolve().parents[1]
REPO = BACKEND.parent


def _enum_values(path: Path) -> list[str]:
    sql = path.read_text(encoding="utf-8")
    match = re.search(r"CREATE TYPE channel_kind AS ENUM \((.*?)\);", sql, re.S)
    assert match, f"в {path.name} не найден CREATE TYPE channel_kind"
    return re.findall(r"'([a-z_]+)'", match.group(1))


@pytest.mark.parametrize("relative", ["schema.sql", "../10-ARCHITECTURE/data-model.sql"])
def test_enum_matches_catalog(relative):
    path = BACKEND / relative
    values = _enum_values(path)
    kinds = [spec.kind for spec in CATALOG]
    assert set(values) == set(kinds), (
        f"{path.name}: лишние {set(values) - set(kinds)}, "
        f"отсутствуют {set(kinds) - set(values)}"
    )
    assert len(values) == len(set(values)) == 29


def test_pydantic_channel_kind_comes_from_catalog():
    assert CHANNEL_KINDS == tuple(spec.kind for spec in CATALOG)


def test_every_new_platform_is_accepted_by_api():
    """Раньше Дзен/TenChat/Pinterest отбивались валидацией 422, хотя есть в каталоге."""
    from fastapi.testclient import TestClient

    from orbita.main import app

    client = TestClient(app)
    for kind in ("dzen", "tenchat", "pinterest", "twogis", "kick", "webpush"):
        r = client.get(f"/api/v1/channels/kinds/{kind}/spec", headers={"X-Actor-Role": "owner"})
        assert r.status_code == 200, (kind, r.status_code)


def test_alembic_revision_chain_is_linear():
    versions = sorted((BACKEND / "migrations" / "versions").glob("*.py"))
    revisions = {}
    for path in versions:
        text = path.read_text(encoding="utf-8")
        rev = re.search(r'^revision = "([^"]+)"', text, re.M).group(1)
        down = re.search(r'^down_revision = (None|"[^"]+")', text, re.M).group(1)
        revisions[rev] = None if down == "None" else down.strip('"')
    assert len([r for r in revisions.values() if r is None]) == 1, "должен быть один baseline"
    for rev, parent in revisions.items():
        if parent is not None:
            assert parent in revisions, f"{rev} ссылается на неизвестную ревизию {parent}"


def test_alembic_offline_sql_contains_enum_extension():
    """``alembic upgrade head --sql`` рендерится без подключения к БД и содержит новые каналы."""
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head", "--sql"],
        cwd=BACKEND, capture_output=True, text=True, timeout=120,
        env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(BACKEND / "src"),
             "DATABASE_URL": "postgresql+psycopg://u:p@localhost:5432/orbita"},
    )
    assert result.returncode == 0, result.stderr[-2000:]
    assert "ALTER TYPE channel_kind ADD VALUE IF NOT EXISTS 'dzen'" in result.stdout
    assert "short_links" in result.stdout and "CREATE TABLE" in result.stdout


def test_openapi_matches_app():
    """Контракт в репозитории больше не отстаёт от кода."""
    import yaml

    from orbita.main import app

    path = REPO / "10-ARCHITECTURE" / "openapi.yaml"
    spec = yaml.safe_load(path.read_text(encoding="utf-8"))
    live = app.openapi()
    assert set(spec["paths"]) == set(live["paths"]), (
        "openapi.yaml расходится с кодом — пересоберите: python scripts/dump_openapi.py"
    )
