"""Согласование ORM со схемой, рабочие RLS-политики и журнал попыток публикации

Revision ID: 0005_schema_alignment
Revises: 0004_publishing
Create Date: 2026-11-17

Закрывает находки второго аудита, подтверждённые по коду:

1. **`citext` не создавался.** `schema.sql` объявляет `users.email citext UNIQUE`, а
   расширение не ставил никто: чистая база падала на baseline с `type "citext" does not exist`.
   Здесь расширение ставится (и в `sql/00_cluster_init.sql` — для кластера).

2. **ORM и схема расходились.** В модели публикации были `status String`, `erid`, `is_ad`,
   `approved_by_role`, которых в таблице не было, а в схеме — `approved_by uuid`, `cost_actual_rub`.
   Любое чтение `pub_get()` формировало запрос к отсутствующим колонкам. Теперь схема
   догоняет модель: добавляем `channel`, `erid`, `is_ad`, `approved_by_role`, `completed_at`.

3. **RLS был «включён и запрещён».** `FORCE ROW LEVEL SECURITY` ставился на `workspaces`,
   `memberships`, `channel_accounts`, но политик для них не было — то есть функция
   `can_access_workspace()` не могла прочитать собственные таблицы, и после включения контекста
   любая выборка возвращала 0 строк. Здесь добавляются политики для всех таблиц клиента,
   включая `entity_docs` (в нём лежат сигналы и варианты), и платформенный обход для
   service/cron-контекста.

4. **Публикация могла уйти дважды после сетевого сбоя.** Добавляем `publication_attempts`:
   намерение публикации фиксируется в БД до обращения к площадке, поэтому после падения
   воркера видно, что попытка была, и решение принимается по журналу, а не по памяти процесса.
"""
from __future__ import annotations

from alembic import op

revision = "0005_schema_alignment"
down_revision = "0004_publishing"
branch_labels = None
depends_on = None

#: Таблицы, где изоляция идёт по `workspace_id`.
WORKSPACE_TABLES = (
    "publications", "variants", "signals", "short_links", "analytics_events",
    "channel_accounts", "entity_docs", "brands", "inbox_threads", "inbox_events",
    "knowledge_sources", "publication_attempts",
)


def upgrade() -> None:
    # --- 1. расширения, нужные схеме ---
    op.execute("CREATE EXTENSION IF NOT EXISTS citext")
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

    # --- 2. роли: RBAC использует copywriter/lawyer, в ENUM их не было ---
    for role in ("copywriter", "lawyer"):
        op.execute(f"ALTER TYPE member_role ADD VALUE IF NOT EXISTS '{role}'")

    # --- 3. недостающие колонки публикации (ORM их уже ожидает) ---
    op.execute(
        """
        ALTER TABLE publications
          ADD COLUMN IF NOT EXISTS channel      text,
          ADD COLUMN IF NOT EXISTS erid         text,
          ADD COLUMN IF NOT EXISTS is_ad        boolean NOT NULL DEFAULT false,
          ADD COLUMN IF NOT EXISTS approved_by_role text,
          ADD COLUMN IF NOT EXISTS completed_at timestamptz
        """
    )
    # FK на варианты/аккаунты остаются: наполнение типизированных таблиц делает слой данных
    op.execute("CREATE INDEX IF NOT EXISTS publications_ws_status ON publications (workspace_id, status)")

    # --- 3a. member_role: роли контент-процесса ---
    # ADD VALUE нельзя откатить и нельзя выполнять в транзакции с последующим использованием,
    # поэтому значения добавляются по одному и только если их нет.
    op.execute(
        """
        DO $$
        DECLARE missing text;
        BEGIN
          FOREACH missing IN ARRAY ARRAY['copywriter', 'lawyer']
          LOOP
            IF NOT EXISTS (SELECT 1 FROM pg_enum e JOIN pg_type t ON t.oid = e.enumtypid
                            WHERE t.typname = 'member_role' AND e.enumlabel = missing) THEN
              EXECUTE 'ALTER TYPE member_role ADD VALUE ' || quote_literal(missing);
            END IF;
          END LOOP;
        END $$;
        """
    )

    # --- 3b. channel_accounts: ORM ожидает capabilities/health/note ---
    op.execute(
        """
        ALTER TABLE channel_accounts
          ADD COLUMN IF NOT EXISTS capabilities jsonb NOT NULL DEFAULT '{}'::jsonb,
          ADD COLUMN IF NOT EXISTS health       jsonb NOT NULL DEFAULT '{}'::jsonb,
          ADD COLUMN IF NOT EXISTS note         text
        """
    )

    # --- 4. журнал попыток публикации ---
    # Раньше в schema.sql он назывался attempt_no/raw_error и не знал про workspace_id.
    # Приводим существующие базы к канонической форме ORM, не теряя данные.
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM information_schema.columns
                      WHERE table_name = 'publication_attempts' AND column_name = 'attempt_no')
             AND NOT EXISTS (SELECT 1 FROM information_schema.columns
                      WHERE table_name = 'publication_attempts' AND column_name = 'attempt') THEN
            ALTER TABLE publication_attempts RENAME COLUMN attempt_no TO attempt;
          END IF;
        END $$;
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS publication_attempts (
          id              bigserial PRIMARY KEY,
          publication_id  uuid NOT NULL REFERENCES publications(id) ON DELETE CASCADE,
          workspace_id    uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
          attempt         integer NOT NULL,
          worker_id       text,
          idempotency_key text,
          started_at      timestamptz NOT NULL DEFAULT now(),
          finished_at     timestamptz,
          outcome         text,          -- published | retry_wait | failed | needs_reauth | uncertain
          external_id     text,
          error           text
        )
        """
    )
    op.execute("""
        CREATE INDEX IF NOT EXISTS publication_attempts_pub
          ON publication_attempts (publication_id, attempt DESC)
    """)
    # Один «в полёте» попытки на публикацию: вторая попытка не начнётся, пока первая
    # не закрыта — это защита от двойной отправки при сетевом таймауте.
    op.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS publication_attempts_inflight
          ON publication_attempts (publication_id)
          WHERE finished_at IS NULL
    """)

    # --- 5. функции контекста ---
    op.execute("""
        CREATE OR REPLACE FUNCTION app_workspace_id() RETURNS uuid
          LANGUAGE sql STABLE AS $$
            SELECT nullif(current_setting('app.workspace_id', true), '')::uuid
          $$
    """)
    # Доступ определяется тем, что приложение уже проверило (JWT/сессия) и выставило в контекст.
    # Платформенный режим (cron, миграции, воркер) помечается явно ролью 'platform'.
    op.execute("""
        CREATE OR REPLACE FUNCTION can_access_workspace(ws uuid) RETURNS boolean
          LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS $$
            SELECT CASE
              WHEN coalesce(current_setting('app.actor_role', true), '') = 'platform' THEN true
              WHEN ws IS NULL THEN false
              WHEN app_workspace_id() = ws THEN true
              WHEN nullif(current_setting('app.tenant_id', true), '') IS NOT NULL
                THEN EXISTS (SELECT 1 FROM workspaces w
                              WHERE w.id = ws AND w.tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
              ELSE false
            END
          $$
    """)

    # --- 6. RLS: политики для всех таблиц клиента ---
    for table in WORKSPACE_TABLES:
        op.execute(f"""
            DO $$
            BEGIN
              IF EXISTS (SELECT 1 FROM information_schema.tables
                          WHERE table_name = '{table}' AND table_schema = 'public') THEN
                EXECUTE 'ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY';
                EXECUTE 'ALTER TABLE public.{table} FORCE ROW LEVEL SECURITY';
                EXECUTE 'DROP POLICY IF EXISTS {table}_tenant ON public.{table}';
                -- платформенная роль (cron, воркер, миграции) обходит арендатора:
                -- без этой ветки сервисные процессы не могли записать ни строки
                EXECUTE $p$CREATE POLICY {table}_tenant ON public.{table}
                          USING (coalesce(current_setting('app.actor_role', true), '') = 'platform'
                                 OR can_access_workspace(workspace_id))
                          WITH CHECK (coalesce(current_setting('app.actor_role', true), '') = 'platform'
                                 OR can_access_workspace(workspace_id))$p$;
                EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE ON public.{table} TO orbita_app';
              END IF;
            END
            $$;
        """)

    # --- 7. RLS: tenants, workspaces, memberships (без них can_access_workspace «слепая») ---
    op.execute("ALTER TABLE tenants ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE tenants FORCE ROW LEVEL SECURITY")
    op.execute("DROP POLICY IF EXISTS tenants_read ON tenants")
    op.execute("""
        CREATE POLICY tenants_read ON tenants FOR SELECT
          USING (coalesce(current_setting('app.actor_role', true), '') = 'platform'
                 OR id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    """)
    op.execute("DROP POLICY IF EXISTS tenants_write ON tenants")
    op.execute("""
        CREATE POLICY tenants_write ON tenants FOR ALL
          USING (coalesce(current_setting('app.actor_role', true), '') = 'platform')
          WITH CHECK (coalesce(current_setting('app.actor_role', true), '') = 'platform')
    """)

    op.execute("ALTER TABLE workspaces ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE workspaces FORCE ROW LEVEL SECURITY")
    op.execute("DROP POLICY IF EXISTS workspaces_access ON workspaces")
    op.execute("""
        -- Политика на workspaces НЕ вызывает can_access_workspace(): эта функция сама
        -- читает workspaces, и вызов из её политики даёт бесконечную рекурсию
        -- («stack depth limit exceeded»). Здесь достаточно сравнения с контекстом.
        CREATE POLICY workspaces_access ON workspaces FOR ALL
          USING (coalesce(current_setting('app.actor_role', true), '') = 'platform'
                 OR id = nullif(current_setting('app.workspace_id', true), '')::uuid
                 OR tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
          WITH CHECK (coalesce(current_setting('app.actor_role', true), '') = 'platform'
                      OR tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    """)

    op.execute("ALTER TABLE memberships ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE memberships FORCE ROW LEVEL SECURITY")
    op.execute("DROP POLICY IF EXISTS memberships_access ON memberships")
    op.execute("""
        -- Своё членство видно всегда (нужно для проверки прав), чужое — по совпадению
        -- арендатора или платформенной роли.
        CREATE POLICY memberships_access ON memberships FOR ALL
          USING (coalesce(current_setting('app.actor_role', true), '') = 'platform'
                 OR user_id = nullif(current_setting('app.user_id', true), '')::uuid
                 OR tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
          WITH CHECK (coalesce(current_setting('app.actor_role', true), '') = 'platform'
                      OR tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    """)

    op.execute("ALTER TABLE users ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE users FORCE ROW LEVEL SECURITY")
    op.execute("DROP POLICY IF EXISTS users_access ON users")
    op.execute("""
        CREATE POLICY users_access ON users FOR ALL
          USING (coalesce(current_setting('app.actor_role', true), '') = 'platform'
                 OR id = nullif(current_setting('app.user_id', true), '')::uuid)
          WITH CHECK (coalesce(current_setting('app.actor_role', true), '') = 'platform'
                      OR id = nullif(current_setting('app.user_id', true), '')::uuid)
    """)

    op.execute("""
        GRANT SELECT, INSERT, UPDATE, DELETE ON tenants, workspaces, memberships, users
          TO orbita_app
    """)
    op.execute("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO orbita_app")


def downgrade() -> None:
    """Откат согласования.

    Важно: колонки `is_ad`/`erid`/`channel` участвуют в политиках RLS и в индексах, поэтому
    сначала снимается зависимый объект, и только потом колонка. Иначе PostgreSQL отвечает
    `DependentObjectsStillExist`, и откат ломается на середине (так и было: downgrade падал,
    оставляя базу в промежуточном состоянии).
    """
    op.execute("DROP TABLE IF EXISTS publication_attempts")
    for table in ("workspaces", "memberships", "entity_docs", "channel_accounts", "users"):
        for suffix in ("access", "tenant", "admin", "read", "write"):
            op.execute(f"DROP POLICY IF EXISTS {table}_{suffix} ON {table}")
    op.execute("ALTER TABLE users DISABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE workspaces DISABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE memberships DISABLE ROW LEVEL SECURITY")
    op.execute("""
        ALTER TABLE publications
          DROP COLUMN IF EXISTS completed_at,
          DROP COLUMN IF EXISTS approved_by_role,
          DROP COLUMN IF EXISTS is_ad CASCADE,
          DROP COLUMN IF EXISTS erid CASCADE,
          DROP COLUMN IF EXISTS channel CASCADE
    """)
