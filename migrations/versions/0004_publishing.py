"""Publishing Engine и принудительный RLS

Revision ID: 0004_publishing
Revises: 0003_golive
Create Date: 2026-11-17

Закрывает два P0 из аудита:

1. **Зависшие публикации.** Статус ``publishing`` не имел срока: если воркер падал,
   пост оставался в этом статусе навсегда и больше никогда не попадал в выборку
   (``due_publications`` искал только ``queued``). Теперь есть аренда (``lease_until``),
   владелец аренды (``claimed_by``) и срок следующего повтора (``next_attempt_at``),
   а статус ``retry_wait`` отличает временную ошибку от окончательного провала.

2. **RLS обходился владельцем таблиц.** Политики существовали, но применялись только к
   ролям без прав владельца, а роль API была суперпользователем. Добавляем
   ``FORCE ROW LEVEL SECURITY`` — политики действуют и на владельца таблицы, и выдаём
   ``orbita_app`` (NOSUPERUSER/NOBYPASSRLS) права на объекты. Плюс функция установки
   контекста транзакции, которую вызывает ``persistence.Database`` перед каждым запросом.
"""
from __future__ import annotations

from alembic import op

revision = "0004_publishing"
down_revision = "0003_golive"
branch_labels = None
depends_on = None

#: Таблицы с политиками из `sql/rls_rbac.sql`, к которым применяем FORCE RLS.
RLS_TABLES = (
    "publications", "variants", "signals", "short_links", "analytics_events",
    "audit_log", "ord_report_queue", "channel_accounts", "inbox_events",
    "workspaces", "brands", "memberships",
)


def upgrade() -> None:
    # --- 1. статус retry_wait в перечислении ---
    op.execute("ALTER TYPE publication_status ADD VALUE IF NOT EXISTS 'retry_wait'")

    # --- 2. аренда и повторы ---
    op.execute(
        """
        ALTER TABLE publications
          ADD COLUMN IF NOT EXISTS lease_until     timestamptz,
          ADD COLUMN IF NOT EXISTS claimed_by      text,
          ADD COLUMN IF NOT EXISTS next_attempt_at timestamptz
        """
    )
    # Частичный индекс: «что забрать прямо сейчас» — самый частый запрос воркера.
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS publications_claimable
          ON publications (scheduled_at)
          WHERE status IN ('queued', 'retry_wait')
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS publications_lease
          ON publications (lease_until)
          WHERE status = 'publishing'
        """
    )

    # --- 3. принудительный RLS + права непривилегированной роли ---
    for table in RLS_TABLES:
        op.execute(
            f"""
            DO $$
            BEGIN
              IF EXISTS (SELECT 1 FROM information_schema.tables
                          WHERE table_name = '{table}' AND table_schema = 'public') THEN
                EXECUTE 'ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY';
                EXECUTE 'ALTER TABLE public.{table} FORCE ROW LEVEL SECURITY';
                EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE ON public.{table} TO orbita_app';
              END IF;
            END
            $$;
            """
        )

    # --- 4. контекст транзакции ---
    # `persistence.Database.session()` вызывает эту функцию в начале каждой транзакции.
    # Отдельная функция вместо SET LOCAL в тексте запроса: одно место, легко глянуть в логах.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION app_set_context(p_tenant uuid, p_workspace uuid,
                                                   p_user uuid, p_role text DEFAULT '')
        RETURNS void LANGUAGE plpgsql AS $$
        BEGIN
          PERFORM set_config('app.tenant_id', coalesce(p_tenant::text, ''), true);
          PERFORM set_config('app.workspace_id', coalesce(p_workspace::text, ''), true);
          PERFORM set_config('app.user_id', coalesce(p_user::text, ''), true);
          PERFORM set_config('app.actor_role', coalesce(p_role, ''), true);
        END
        $$;
        """
    )
    op.execute("GRANT EXECUTE ON FUNCTION app_set_context(uuid, uuid, uuid, text) TO orbita_app")


def downgrade() -> None:
    for table in RLS_TABLES:
        op.execute(
            f"""
            DO $$
            BEGIN
              IF EXISTS (SELECT 1 FROM information_schema.tables
                          WHERE table_name = '{table}' AND table_schema = 'public') THEN
                EXECUTE 'ALTER TABLE public.{table} NO FORCE ROW LEVEL SECURITY';
              END IF;
            END
            $$;
            """
        )
    # колонки аренды и значение перечисления не удаляем: откат статуса может потерять
    # историю публикаций, а лишние NULL-колонки безопасны
