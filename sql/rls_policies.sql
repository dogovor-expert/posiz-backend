-- =====================================================================
--  Политики RLS: единственный источник истины
-- =====================================================================
--  Почему отдельный файл. Раньше политики создавались в трёх местах:
--  `schema.sql`/`rls_rbac.sql` (артефакт первого дня), `v3.sql` (таблицы v3) и
--  миграциях 0004/0005. Третий аудит показал, чем это заканчивается: миграция 0006
--  исправила модель, но повторный прогон baseline (он идемпотентен и используется
--  инструментами) возвращал старые «широкие» политики — то есть защита зависела от
--  того, какой файл выполнился последним.
--
--  Теперь DDL политик живёт только здесь. Файл выполняется:
--    * baseline-миграцией 0001 (после схемы),
--    * миграцией 0006 (поверх любых прежних политик),
--    * любым инструментом, который пересобирает политики.
--  Повторный прогон безопасен: все политики снимаются по имени перед созданием.
--
--  Модель (две роли политик, не путать):
--
--    RESTRICTIVE — «рамка». Объединяется через AND со всеми остальными политиками,
--                  обойти её новой permissive-политикой нельзя.
--    PERMISSIVE  — «права». Объединяется через OR. Здесь живут матрица ролей,
--                  комплаенс (реклама/erid) и сервисный доступ платформы.
--
--  Ошибка, которую эта модель устраняет: ограничение, положенное в permissive-политику,
--  ничего не ограничивает, если рядом есть другая permissive-политика с более широким
--  условием — OR пропускает строку по любой из них. Именно так «роль copywriter не
--  создаёт рекламу без erid» превращалось в «создаёт».
-- =====================================================================

-- ---------------------------------------------------------------------
-- 0. Вспомогательные функции (idempotent)
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION app_role() RETURNS text
  LANGUAGE sql STABLE AS $$
    SELECT coalesce(current_setting('app.actor_role', true), '')
  $$;

CREATE OR REPLACE FUNCTION app_tenant_id() RETURNS uuid
  LANGUAGE sql STABLE AS $$
    SELECT nullif(current_setting('app.tenant_id', true), '')::uuid
  $$;

CREATE OR REPLACE FUNCTION platform_or_access(ws uuid) RETURNS boolean
  LANGUAGE sql STABLE AS $$
    SELECT app_role() = 'platform' OR can_access_workspace(ws)
  $$;

-- ---------------------------------------------------------------------
-- 1. publications: рамка + права + комплаенс
-- ---------------------------------------------------------------------
DO $$
DECLARE r record;
BEGIN
  IF to_regclass('public.publications') IS NULL THEN RETURN; END IF;

  EXECUTE 'ALTER TABLE publications ENABLE ROW LEVEL SECURITY';
  EXECUTE 'ALTER TABLE publications FORCE ROW LEVEL SECURITY';

  -- Снимаем ВСЕ политики таблицы: набор политик этой таблицы полностью наш.
  -- Перечислять legacy-имена поштучно — источник ошибок: исторические политики
  -- назывались по-разному (publications_tenant_isolation, inbox_tenant_isolation для
  -- inbox_threads и т. п.), и пропущенное имя оставляло в базе слабое правило.
  FOR r IN SELECT policyname FROM pg_policies
            WHERE schemaname = 'public' AND tablename = 'publications'
  LOOP
    EXECUTE 'DROP POLICY ' || quote_ident(r.policyname) || ' ON publications';
  END LOOP;

  -- РАМКА: свой workspace, либо платформенная роль (cron, воркер, миграции)
  EXECUTE $p$CREATE POLICY publications_scope ON publications AS RESTRICTIVE FOR ALL
      USING (platform_or_access(workspace_id))
      WITH CHECK (platform_or_access(workspace_id))$p$;

  -- ПРАВА
  EXECUTE $p$CREATE POLICY publications_service ON publications FOR ALL
      USING (app_role() = 'platform') WITH CHECK (app_role() = 'platform')$p$;
  EXECUTE $p$CREATE POLICY publications_read ON publications FOR SELECT
      USING (can_access_workspace(workspace_id))$p$;
  EXECUTE $p$CREATE POLICY publications_write ON publications FOR ALL
      USING (can_access_workspace(workspace_id)
             AND role_in('owner','admin','editor','copywriter','lawyer'))
      WITH CHECK (can_access_workspace(workspace_id)
             AND role_in('owner','admin','editor','copywriter','lawyer'))$p$;
  EXECUTE $p$CREATE POLICY publications_delete ON publications FOR DELETE
      USING (can_access_workspace(workspace_id) AND role_in('owner','admin'))$p$;

  -- КОМПЛАЕНС (RESTRICTIVE — иначе обходится широкой permissive-политикой):
  -- реклама без erid существует только как черновик и только у согласующего.
  EXECUTE $p$CREATE POLICY publications_compliance ON publications
      AS RESTRICTIVE FOR INSERT
      WITH CHECK (app_role() = 'platform' OR NOT is_ad OR erid IS NOT NULL
                  OR (role_in('owner','admin','lawyer') AND status = 'draft'))$p$;

  -- Перевести рекламу в очередь может только согласующий — с erid и подписью.
  EXECUTE $p$CREATE POLICY publications_approval ON publications
      AS RESTRICTIVE FOR UPDATE
      WITH CHECK (app_role() = 'platform' OR NOT is_ad OR erid IS NOT NULL
                  OR (role_in('owner','admin','lawyer')
                      AND (status = 'draft'
                           OR (approved_by IS NOT NULL AND approved_at IS NOT NULL))))$p$;
END $$;

-- ---------------------------------------------------------------------
-- 2. Таблицы с workspace_id: рамка + права
-- ---------------------------------------------------------------------
DO $$
DECLARE t text; r record;
BEGIN
  FOREACH t IN ARRAY ARRAY[
      'signals', 'variants', 'briefs', 'inbox_threads', 'inbox_events',
      'channel_accounts', 'entity_docs', 'brands', 'knowledge_sources',
      'publication_attempts', 'short_links', 'analytics_events', 'assets',
      'bio_pages', 'automation_rules', 'ab_tests', 'listening_keywords',
      'listening_mentions', 'quota_counters', 'payments'
  ]
  LOOP
    IF to_regclass('public.' || quote_ident(t)) IS NULL THEN
      CONTINUE;
    END IF;

    EXECUTE 'ALTER TABLE ' || quote_ident(t) || ' ENABLE ROW LEVEL SECURITY';
    EXECUTE 'ALTER TABLE ' || quote_ident(t) || ' FORCE ROW LEVEL SECURITY';

    -- Снимаем все политики таблицы: дальше создаём полный набор сами.
    FOR r IN SELECT policyname FROM pg_policies
              WHERE schemaname = 'public' AND tablename = t
    LOOP
      EXECUTE 'DROP POLICY ' || quote_ident(r.policyname) || ' ON ' || quote_ident(t);
    END LOOP;

    EXECUTE 'CREATE POLICY ' || quote_ident(t || '_scope') || ' ON ' || quote_ident(t)
            || ' AS RESTRICTIVE FOR ALL USING (platform_or_access(workspace_id))'
            || ' WITH CHECK (platform_or_access(workspace_id))';
    EXECUTE 'CREATE POLICY ' || quote_ident(t || '_service') || ' ON ' || quote_ident(t)
            || ' FOR ALL USING (app_role() = ''platform'')'
            || ' WITH CHECK (app_role() = ''platform'')';
    EXECUTE 'CREATE POLICY ' || quote_ident(t || '_read') || ' ON ' || quote_ident(t)
            || ' FOR SELECT USING (can_access_workspace(workspace_id))';
    EXECUTE 'CREATE POLICY ' || quote_ident(t || '_write') || ' ON ' || quote_ident(t)
            || ' FOR ALL USING (can_access_workspace(workspace_id)'
            || ' AND role_in(''owner'',''admin'',''editor'',''copywriter'',''lawyer''))'
            || ' WITH CHECK (can_access_workspace(workspace_id)'
            || ' AND role_in(''owner'',''admin'',''editor'',''copywriter'',''lawyer''))';
  END LOOP;
END $$;

-- ---------------------------------------------------------------------
-- 3. Журнал решений: append-only, в границах арендатора
-- ---------------------------------------------------------------------
DO $$
BEGIN
  IF to_regclass('public.audit_log') IS NULL THEN RETURN; END IF;
  EXECUTE 'ALTER TABLE audit_log ENABLE ROW LEVEL SECURITY';
  EXECUTE 'ALTER TABLE audit_log FORCE ROW LEVEL SECURITY';
  EXECUTE 'DROP POLICY IF EXISTS audit_read ON audit_log';
  EXECUTE 'DROP POLICY IF EXISTS audit_append ON audit_log';
  EXECUTE 'DROP POLICY IF EXISTS audit_log_scope ON audit_log';

  EXECUTE $p$CREATE POLICY audit_log_scope ON audit_log AS RESTRICTIVE FOR ALL
      USING (app_role() = 'platform' OR tenant_id = app_tenant_id())
      WITH CHECK (app_role() = 'platform' OR tenant_id = app_tenant_id())$p$;
  EXECUTE $p$CREATE POLICY audit_read ON audit_log FOR SELECT
      USING (tenant_id = app_tenant_id() AND role_in('owner','admin','lawyer'))$p$;
  EXECUTE $p$CREATE POLICY audit_append ON audit_log FOR INSERT
      WITH CHECK (tenant_id = app_tenant_id())$p$;
  -- UPDATE/DELETE политик нет: журнал решений неизменяем.
END $$;

-- ---------------------------------------------------------------------
-- 4. ОРД-очередь и имперсонация
-- ---------------------------------------------------------------------
DO $$
BEGIN
  IF to_regclass('public.ord_report_queue') IS NOT NULL THEN
    EXECUTE 'ALTER TABLE ord_report_queue ENABLE ROW LEVEL SECURITY';
    EXECUTE 'DROP POLICY IF EXISTS ord_queue_rw ON ord_report_queue';
    EXECUTE 'DROP POLICY IF EXISTS ord_report_queue_scope ON ord_report_queue';
    EXECUTE $p$CREATE POLICY ord_report_queue_scope ON ord_report_queue AS RESTRICTIVE FOR ALL
        USING (app_role() = 'platform' OR role_in('owner','admin','lawyer'))
        WITH CHECK (app_role() = 'platform' OR role_in('owner','admin','lawyer'))$p$;
  END IF;

  IF to_regclass('public.impersonation_sessions') IS NOT NULL THEN
    EXECUTE 'ALTER TABLE impersonation_sessions ENABLE ROW LEVEL SECURITY';
    EXECUTE 'DROP POLICY IF EXISTS impersonation_admin ON impersonation_sessions';
    EXECUTE $p$CREATE POLICY impersonation_admin ON impersonation_sessions
        AS RESTRICTIVE FOR ALL
        USING (app_role() = 'platform') WITH CHECK (app_role() = 'platform')$p$;
  END IF;
END $$;
