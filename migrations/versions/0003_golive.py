"""Миграция Go-Live: платформенный администратор и документные коллекции v3

Revision ID: 0003_golive
Revises: 0002_channel_kind_29
Create Date: 2026-11-17

1. ``users.is_platform_admin`` — суперадминка отделяется от владельца воркспейса.
   Права ``platform:*`` не выдаются ни одной роли воркспейса, проверка идёт в
   ``rbac.require_platform_admin``.
2. ``entity_docs`` — персистентность доменов, которые ещё не переведены на
   типизированные таблицы (Brand Brain, Инбокс, Link-in-Bio, автоматизации,
   мониторинг, A/B). ``collection`` совпадает с именем целевой таблицы из
   ``sql/v3.sql``/``schema.sql``, поэтому перенос на типизированную схему —
   механический (см. ADR 0014).
"""
from __future__ import annotations

from alembic import op

revision = "0003_golive"
down_revision = "0002_channel_kind_29"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS is_platform_admin boolean NOT NULL DEFAULT false")
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS entity_docs (
          collection   text NOT NULL,
          id           text NOT NULL,
          workspace_id uuid NOT NULL,
          payload      jsonb NOT NULL DEFAULT '{}'::jsonb,
          position     integer NOT NULL DEFAULT 0,
          created_at   timestamptz NOT NULL DEFAULT now(),
          updated_at   timestamptz NOT NULL DEFAULT now(),
          PRIMARY KEY (collection, id)
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS entity_docs_ws ON entity_docs (collection, workspace_id)")
    op.execute("CREATE INDEX IF NOT EXISTS entity_docs_pos ON entity_docs (collection, position)")
    # первый платформенный администратор ставится вручную и только осознанно:
    #   UPDATE users SET is_platform_admin = true WHERE email = '…';


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS entity_docs")
    # колонку не удаляем: потеря флага доступа опаснее, чем лишний столбец
