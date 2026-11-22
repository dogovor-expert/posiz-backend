"""RLS: жёсткая модель политик — рамка (RESTRICTIVE) + права (PERMISSIVE)

Revision ID: 0006_rls_strict
Revises: 0005_schema_alignment
Create Date: 2026-11-17

Два дефекта, найденных третьим аудитом (00-STRATEGY/10-deep-audit.md).

**1. Обход комплаенса на уровне БД.** В PostgreSQL permissive-политики объединяются
через OR. На `publications` рядом лежали:

* ``publications_insert`` — «роль из списка И (не реклама ИЛИ есть erid)»;
* ``publications_tenant`` — «платформенная роль ИЛИ свой workspace».

Второй политики одной хватало для INSERT: copywriter создавал рекламную публикацию
без erid, хотя ``publications_insert`` это запрещала. Та же схема была у
`signals`/`variants`: матрица ролей из ``*_write`` обходилась через ``*_tenant``.
Ограничение, положенное в permissive-политику, не ограничивает ничего, если рядом
есть permissive-политика с более широким условием.

**2. Три источника истины для политик.** DDL создавался в `sql/rls_rbac.sql`,
`sql/v3.sql` и в миграциях 0004/0005. Повторный прогон baseline (он идемпотентен и
используется инструментами) возвращал старые политики поверх исправленных — защита
зависела от порядка выполнения файлов.

Решение: единственный канонический набор политик — ``sql/rls_policies.sql``. Его
выполняют и baseline (0001), и эта миграция. Модель:

* **рамка арендатора** — RESTRICTIVE (AND): `platform OR can_access_workspace(...)`;
* **комплаенс** — RESTRICTIVE: реклама без erid существует только черновиком у согласующего;
* **права** — PERMISSIVE: чтение, запись по матрице ролей, сервисная политика платформы.

SQL написан через DO-блоки с проверками `to_regclass`, поэтому работает и в
offline-режиме `alembic upgrade --sql`.
"""
from __future__ import annotations

from pathlib import Path

from alembic import op

revision = "0006_rls_strict"
down_revision = "0005_schema_alignment"
branch_labels = None
depends_on = None

BACKEND_DIR = Path(__file__).resolve().parents[2]
POLICIES = BACKEND_DIR / "sql" / "rls_policies.sql"

#: Таблицы, которые откат возвращает к модели 0005 (permissive ``*_tenant``).
WORKSPACE_TABLES = (
    "publications", "variants", "signals", "briefs", "inbox_threads", "inbox_events",
    "channel_accounts", "entity_docs", "brands", "knowledge_sources", "publication_attempts",
    "short_links", "analytics_events", "assets", "bio_pages", "automation_rules", "ab_tests",
)

PLATFORM = "coalesce(current_setting('app.actor_role', true), '') = 'platform'"


def upgrade() -> None:
    op.execute(POLICIES.read_text(encoding="utf-8"))


def downgrade() -> None:
    """Возврат к модели 0005 (permissive-политики OR-ятся).

    Модель 0005 работоспособна, но не удерживает комплаенс и матрицу ролей — именно
    это и было дефектом. Откат оставлен для воспроизводимости, а не как рабочий путь.
    """
    for table in WORKSPACE_TABLES:
        op.execute(f"""
DO $$
BEGIN
  IF to_regclass('public.{table}') IS NULL THEN
    RETURN;
  END IF;
  EXECUTE 'DROP POLICY IF EXISTS {table}_scope ON public.{table}';
  EXECUTE 'DROP POLICY IF EXISTS {table}_service ON public.{table}';
  EXECUTE 'DROP POLICY IF EXISTS {table}_read ON public.{table}';
  EXECUTE 'DROP POLICY IF EXISTS {table}_write ON public.{table}';
  IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                  WHERE table_schema = 'public' AND table_name = '{table}'
                    AND column_name = 'workspace_id') THEN
    RETURN;
  END IF;
  EXECUTE $p$CREATE POLICY {table}_tenant ON public.{table} FOR ALL
      USING ({PLATFORM} OR can_access_workspace(workspace_id))
      WITH CHECK ({PLATFORM} OR can_access_workspace(workspace_id))$p$;
END $$;
""")
    op.execute("""
DO $$
BEGIN
  IF to_regclass('public.publications') IS NULL THEN
    RETURN;
  END IF;
  EXECUTE 'DROP POLICY IF EXISTS publications_compliance ON public.publications';
  EXECUTE 'DROP POLICY IF EXISTS publications_approval ON public.publications';
END $$;
""")
