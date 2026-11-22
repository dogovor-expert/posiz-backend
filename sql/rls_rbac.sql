-- =====================================================================
-- Миграция v2: серверный RBAC на уровне PostgreSQL (RLS), короткие ссылки,
-- автоматизация ОРД. Применяется после schema.sql.
--
-- Модель: интерфейс и FastAPI-зависимости — удобство; настоящая граница прав — здесь.
-- Приложение в начале транзакции выставляет контекст:
--   SET LOCAL app.tenant_id  = '<uuid>';
--   SET LOCAL app.workspace_id = '<uuid>';
--   SET LOCAL app.user_id    = '<uuid>';
--   SET LOCAL app.actor_role = 'owner|copywriter|lawyer|analyst|client_viewer';
-- Для фоновых воркеров и миграций используется отдельная роль с BYPASSRLS.
-- =====================================================================

BEGIN;

-- ---------------------------------------------------------------------
-- 1. Роли. member_role расширяется до терминов продукта.
--    ALTER TYPE ... ADD VALUE нельзя откатить внутри транзакции на PG < 16,
--    поэтому в проде этот шаг выносится в отдельную миграцию без BEGIN/COMMIT.
-- ---------------------------------------------------------------------
ALTER TYPE member_role ADD VALUE IF NOT EXISTS 'copywriter';
ALTER TYPE member_role ADD VALUE IF NOT EXISTS 'lawyer';

-- ---------------------------------------------------------------------
-- 2. Поля, без которых правило «реклама только после подписи» не проверить на уровне БД
-- ---------------------------------------------------------------------
ALTER TABLE publications ADD COLUMN IF NOT EXISTS is_ad boolean NOT NULL DEFAULT false;
ALTER TABLE publications ADD COLUMN IF NOT EXISTS erid  text;
CREATE INDEX IF NOT EXISTS publications_ad_unapproved
  ON publications (workspace_id, scheduled_at)
  WHERE is_ad AND approved_at IS NULL;

-- ---------------------------------------------------------------------
-- 3. Короткие ссылки (редирект-сервис posiz.ru)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS short_links (
  code          text PRIMARY KEY,                     -- без 0/o, 1/l/i
  workspace_id  uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  target_url    text NOT NULL,                        -- с UTM-метками
  original_url  text NOT NULL,
  channel       channel_kind,
  campaign      text,
  variant_id    uuid REFERENCES variants(id) ON DELETE SET NULL,
  created_by    uuid REFERENCES users(id),
  clicks_count  integer NOT NULL DEFAULT 0,           -- быстрый счётчик для UI
  created_at    timestamptz NOT NULL DEFAULT now(),
  expires_at    timestamptz
);
CREATE INDEX IF NOT EXISTS short_links_workspace ON short_links (workspace_id, created_at DESC);

-- Клик: отдельная строка в analytics_events (kind='click', meta.link_code).
-- Для больших объёмов то же событие уходит в ClickHouse, а здесь остаётся агрегат.
COMMENT ON TABLE short_links IS
  'Короткие ссылки. Клики пишутся в analytics_events (kind=click, meta={link_code, ua_hash, ip_hash}); '
  'ip/ua хранятся только в виде хэша — 152-ФЗ.';

-- ---------------------------------------------------------------------
-- 4. Очередь ОРД: erid + статус отчёта в ЕРИР
-- ---------------------------------------------------------------------
ALTER TABLE ord_report_queue ADD COLUMN IF NOT EXISTS erid text;
ALTER TABLE ord_report_queue ADD COLUMN IF NOT EXISTS creative_id text;
CREATE INDEX IF NOT EXISTS ord_queue_due ON ord_report_queue (due_at) WHERE status = 'pending';

-- ---------------------------------------------------------------------
-- 5. Функции контекста
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION app_role() RETURNS text
  LANGUAGE sql STABLE AS $$ SELECT coalesce(current_setting('app.actor_role', true), '') $$;

CREATE OR REPLACE FUNCTION app_user_id() RETURNS uuid
  LANGUAGE sql STABLE AS $$ SELECT nullif(current_setting('app.user_id', true), '')::uuid $$;

CREATE OR REPLACE FUNCTION app_tenant_id() RETURNS uuid
  LANGUAGE sql STABLE AS $$ SELECT nullif(current_setting('app.tenant_id', true), '')::uuid $$;

-- Доступ к workspace: членство в организации + принадлежность бренда этой организации.
-- ВАЖНО: политики на workspaces/memberships НЕ должны вызывать эту функцию — она читает
-- те же таблицы, и вызов изнутри политики даёт рекурсию (stack depth limit exceeded).
-- Эти две таблицы защищены сравнением с контекстом запроса, всё остальное — этой функцией.
CREATE OR REPLACE FUNCTION can_access_workspace(ws uuid) RETURNS boolean
  LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
    SELECT EXISTS (
      SELECT 1 FROM workspaces w
      JOIN memberships m ON m.tenant_id = w.tenant_id
      WHERE w.id = ws AND m.user_id = app_user_id()
    ) AND EXISTS (SELECT 1 FROM workspaces w2 WHERE w2.id = ws AND w2.tenant_id = app_tenant_id())
  $$;

-- Права записи по роли (дубль матрицы из orbita/rbac.py, но здесь — обязательный)
CREATE OR REPLACE FUNCTION role_in(variadic roles text[]) RETURNS boolean
  LANGUAGE sql IMMUTABLE AS $$ SELECT app_role() = ANY (roles) $$;

-- Платформенная роль (cron/воркер/миграции) видна политикам как 'platform';
-- клиентская — только через can_access_workspace(). Функции объявлены здесь,
-- потому что ими пользуются и триггеры, и sql/rls_policies.sql.
CREATE OR REPLACE FUNCTION platform_or_access(ws uuid) RETURNS boolean
  LANGUAGE sql STABLE AS $$
    SELECT app_role() = 'platform' OR can_access_workspace(ws)
  $$;

-- Политики RLS этих таблиц создаются в sql/rls_policies.sql (единственный источник
-- истины; выполняется baseline-миграцией 0001 и миграцией 0006 последним). См. там
-- объяснение модели «RESTRICTIVE-рамка + PERMISSIVE-права» и правила комплаенса.

-- ---------------------------------------------------------------------
-- 8. Публичный редиректор: отдельная роль БД без доступа к tenants/users.
--    Она видит только short_links (select) и пишет analytics_events (insert).
-- ---------------------------------------------------------------------
-- CREATE ROLE posiz_link_redirect LOGIN PASSWORD '...' NOSUPERUSER;
-- GRANT SELECT ON short_links TO posiz_link_redirect;
-- GRANT INSERT ON analytics_events TO posiz_link_redirect;
-- GRANT USAGE ON SCHEMA public TO posiz_link_redirect;
-- REVOKE ALL ON tenants, users, memberships, publications FROM posiz_link_redirect;

COMMIT;

-- Тест-кейсы, которые должны падать в интеграционных тестах:
-- 1) role=copywriter, UPDATE publications SET status='queued' WHERE is_ad → 0 строк.
-- 2) role=lawyer, то же с approved_by IS NULL → 0 строк.
-- 3) role=owner, is_ad=true, erid задан, approved_by/approved_at заданы → 1 строка.
-- 4) app_tenant_id другого арендатора → SELECT возвращает 0 строк на всех таблицах.
