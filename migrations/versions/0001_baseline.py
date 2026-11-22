"""Baseline: базовая схема, RLS/RBAC и расширения v3

Revision ID: 0001_baseline
Revises:
Create Date: 2026-11-17

Схема проекта описана в SQL-файлах. Baseline накатывает их по порядку, чтобы новая
база поднималась одной командой ``alembic upgrade head`` — без ручного docker-entrypoint
и без расхождения между «схемой в репозитории» и «схемой в проде».

Файлы идемпотентны (``IF NOT EXISTS``), повторный прогон на живой базе безопасен.
"""
from __future__ import annotations

from pathlib import Path

from alembic import op
from sqlalchemy import text

revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None

BACKEND_DIR = Path(__file__).resolve().parents[2]
FILES = ("schema.sql", "sql/rls_rbac.sql", "sql/v3.sql",
         # политики — последними и из одного источника (см. sql/rls_policies.sql)
         "sql/rls_policies.sql")


def _execute(path: Path) -> None:
    """Выполнить SQL-файл как есть, проверив его на «%».

    Драйвер psycopg использует paramstyle ``pyformat``: одиночный ``%`` в тексте
    (проценты в комментариях и в формулах) SQLAlchemy принимает за начало параметра и
    падает с «A value is required for bind parameter». Поэтому он экранируется, а сам
    файл остаётся читаемым для человека: правим текст, а не файл.
    """
    sql = path.read_text(encoding="utf-8")
    if "%" in sql:
        raise RuntimeError(f"{path.name}: символ «%» в SQL ломает pyformat-драйверы "
                           f"(psycopg); используйте quote_ident() вместо format(%I)")
    if text(sql)._bindparams:
        found = ", ".join(text(sql)._bindparams)
        raise RuntimeError(f"{path.name}: SQLAlchemy принял «:…» за bind-параметр ({found}). "
                           f"В комментариях пишите «имя = значение», а не JSON с двоеточием")
    op.execute(sql)


def upgrade() -> None:
    for name in FILES:
        _execute(BACKEND_DIR / name)


def downgrade() -> None:
    # Baseline сознательно не откатывается: удаление схемы в проде — отдельная
    # осознанная операция, а не следствие alembic downgrade.
    raise RuntimeError("Baseline-миграцию нельзя откатить: удалите базу целиком")
