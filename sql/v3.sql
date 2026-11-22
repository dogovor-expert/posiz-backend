-- =====================================================================
-- Миграция v3: карта каналов (29 площадок), семь функций, контуры SaaS.
-- Применяется после schema.sql и sql/rls_rbac.sql.
-- =====================================================================

BEGIN;

-- ---------------------------------------------------------------------
-- 1. Новые каналы в справочнике channel_kind
--    Дзен, TenChat, VK Видео, Rutube, VC.ru, Habr, Pinterest,
--    Яндекс Бизнес, 2ГИС, Discord, Twitch, Kick, Email, Web Push.
-- ---------------------------------------------------------------------
ALTER TYPE channel_kind ADD VALUE IF NOT EXISTS 'dzen';
ALTER TYPE channel_kind ADD VALUE IF NOT EXISTS 'tenchat';
ALTER TYPE channel_kind ADD VALUE IF NOT EXISTS 'vk_video';
ALTER TYPE channel_kind ADD VALUE IF NOT EXISTS 'rutube';
ALTER TYPE channel_kind ADD VALUE IF NOT EXISTS 'vcru';
ALTER TYPE channel_kind ADD VALUE IF NOT EXISTS 'habr';
ALTER TYPE channel_kind ADD VALUE IF NOT EXISTS 'pinterest';
ALTER TYPE channel_kind ADD VALUE IF NOT EXISTS 'yandex_business';
ALTER TYPE channel_kind ADD VALUE IF NOT EXISTS 'twogis';
ALTER TYPE channel_kind ADD VALUE IF NOT EXISTS 'discord';
ALTER TYPE channel_kind ADD VALUE IF NOT EXISTS 'twitch';
ALTER TYPE channel_kind ADD VALUE IF NOT EXISTS 'kick';
ALTER TYPE channel_kind ADD VALUE IF NOT EXISTS 'email';
ALTER TYPE channel_kind ADD VALUE IF NOT EXISTS 'webpush';

-- Способ интеграции — часть контракта канала, а не догадка в коде
CREATE TABLE IF NOT EXISTS channel_capabilities (
  kind        channel_kind PRIMARY KEY,
  api_mode    text NOT NULL CHECK (api_mode IN ('api','rss','headless','blog','webhook','local','email','manual')),
  publish     boolean NOT NULL DEFAULT false,
  read        boolean NOT NULL DEFAULT true,
  connect     text NOT NULL DEFAULT 'oauth',
  risk        text NOT NULL DEFAULT 'low',
  limit_text  text,
  note        text,
  priority    text NOT NULL DEFAULT 'p1',
  updated_at  timestamptz NOT NULL DEFAULT now()
);
COMMENT ON TABLE channel_capabilities IS
  'Карта адаптеров: чем публикуем и что честно можем. headless = черновик+напоминание или Playwright-воркер.';

-- ---------------------------------------------------------------------
-- 2. Link-in-Bio
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS bio_pages (
  slug          text PRIMARY KEY,
  workspace_id  uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  title         text NOT NULL,
  bio           text,
  theme         text NOT NULL DEFAULT 'graphite',
  custom_domain text,
  is_published  boolean NOT NULL DEFAULT true,
  views         bigint NOT NULL DEFAULT 0,
  clicks        bigint NOT NULL DEFAULT 0,
  created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS bio_blocks (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  slug        text NOT NULL REFERENCES bio_pages(slug) ON DELETE CASCADE,
  kind        text NOT NULL CHECK (kind IN ('link','post','magnet','messenger','subscribe')),
  label       text NOT NULL,
  url         text NOT NULL,
  badge       text,
  channel     channel_kind,
  sort_order  integer NOT NULL DEFAULT 0,
  clicks      bigint NOT NULL DEFAULT 0,
  created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS bio_blocks_slug ON bio_blocks (slug, sort_order);

-- ---------------------------------------------------------------------
-- 3. Комментарий → личка (comment-to-DM)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS automation_rules (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id   uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  kind           text NOT NULL DEFAULT 'comment_dm',
  keyword        text NOT NULL,
  channels       channel_kind[] NOT NULL,
  magnet         text,
  link_campaign  text,
  dm_template    text NOT NULL,
  active         boolean NOT NULL DEFAULT true,
  sends_count    integer NOT NULL DEFAULT 0,
  conversion     numeric(5,4),
  created_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS automation_rules_kw ON automation_rules (workspace_id, keyword);

CREATE TABLE IF NOT EXISTS automation_events (
  id          bigserial PRIMARY KEY,
  rule_id     uuid NOT NULL REFERENCES automation_rules(id) ON DELETE CASCADE,
  channel     channel_kind NOT NULL,
  author      text,
  comment     text,
  dm_text     text,
  short_code  text REFERENCES short_links(code) ON DELETE SET NULL,
  status      text NOT NULL DEFAULT 'sent',
  created_at  timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------
-- 4. Social Listening
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS listening_keywords (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  phrase      text NOT NULL,
  channels    channel_kind[] NOT NULL,
  active      boolean NOT NULL DEFAULT true,
  created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS listening_mentions (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  channel      channel_kind NOT NULL,
  source       text,
  text         text NOT NULL,
  url          text,
  keyword_id   uuid REFERENCES listening_keywords(id) ON DELETE SET NULL,
  sentiment    text,
  priority     smallint NOT NULL DEFAULT 2,
  status       text NOT NULL DEFAULT 'new',
  found_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS mentions_priority ON listening_mentions (workspace_id, priority, found_at DESC);

-- ---------------------------------------------------------------------
-- 5. Медиа-банк (DAM)
-- ---------------------------------------------------------------------
-- Медиа-банк: таблица создана в schema.sql (базовая схема), здесь только доводим её
-- до вида DAM. Раньше в этом файле стоял второй CREATE TABLE IF NOT EXISTS assets —
-- он молча пропускался, и индекс ниже падал на несуществующей колонке folder.
ALTER TABLE assets
  ADD COLUMN IF NOT EXISTS folder     text NOT NULL DEFAULT 'root',
  ADD COLUMN IF NOT EXISTS name       text NOT NULL DEFAULT '',
  ADD COLUMN IF NOT EXISTS size_bytes bigint NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS tags       text[] NOT NULL DEFAULT '{}',
  ADD COLUMN IF NOT EXISTS license    text NOT NULL DEFAULT 'own',
  ADD COLUMN IF NOT EXISTS created_by uuid REFERENCES users(id);
CREATE INDEX IF NOT EXISTS assets_folder ON assets (workspace_id, folder);

-- ---------------------------------------------------------------------
-- 6. A/B-тесты заголовков и обложек
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ab_tests (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id     uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  publication_id   uuid NOT NULL REFERENCES publications(id) ON DELETE CASCADE,
  metric           text NOT NULL DEFAULT 'ctr',
  check_after      interval NOT NULL DEFAULT interval '120 minutes',
  status           text NOT NULL DEFAULT 'running',
  winner           text,
  relative_diff    numeric(6,4),
  edit_applied     boolean NOT NULL DEFAULT false,
  created_at       timestamptz NOT NULL DEFAULT now(),
  decided_at       timestamptz
);

CREATE TABLE IF NOT EXISTS ab_variants (
  test_id     uuid NOT NULL REFERENCES ab_tests(id) ON DELETE CASCADE,
  variant     char(1) NOT NULL CHECK (variant IN ('a','b')),
  headline    text,
  cover_asset uuid REFERENCES assets(id) ON DELETE SET NULL,
  impressions integer NOT NULL DEFAULT 0,
  clicks      integer NOT NULL DEFAULT 0,
  ctr         numeric(6,4),
  PRIMARY KEY (test_id, variant)
);

-- ---------------------------------------------------------------------
-- 7. Quota Engine и биллинг (ЮKassa + СБП, 54-ФЗ, ЭДО)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS quota_counters (
  workspace_id uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  period       date NOT NULL,
  key          text NOT NULL,
  used         numeric(12,2) NOT NULL DEFAULT 0,
  limit_value  numeric(12,2) NOT NULL,
  PRIMARY KEY (workspace_id, period, key)
);

CREATE TABLE IF NOT EXISTS payments (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id   uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  provider       text NOT NULL DEFAULT 'yookassa',
  amount_rub     numeric(10,2) NOT NULL,
  plan_code      text,
  payer_name     text,
  inn            text,
  is_legal_entity boolean GENERATED ALWAYS AS (inn IS NOT NULL) STORED,
  status         text NOT NULL DEFAULT 'pending',
  payment_method text,                  -- card | sbp | invoice
  provider_id    text,
  receipt_url    text,                  -- 54-ФЗ
  invoice_pdf    text,
  upd_url        text,                  -- ЭДО Диадок/СБИС
  created_at     timestamptz NOT NULL DEFAULT now(),
  paid_at        timestamptz
);
CREATE INDEX IF NOT EXISTS payments_ws ON payments (workspace_id, created_at DESC);

-- ---------------------------------------------------------------------
-- 8. Имперсонация поддержки (только чтение, с причиной)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS impersonation_sessions (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id    uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  actor_id     uuid REFERENCES users(id),
  mode         text NOT NULL DEFAULT 'support_readonly',
  reason       text NOT NULL CHECK (length(reason) >= 5),
  created_at   timestamptz NOT NULL DEFAULT now(),
  expires_at   timestamptz NOT NULL DEFAULT now() + interval '30 minutes'
);

-- 9. Политики RLS новых таблиц — в sql/rls_policies.sql (единый источник истины).

COMMIT;

-- Проверки для интеграционных тестов:
-- 1) SELECT count(*) FROM channel_capabilities → 29 строк после сидинга.
-- 2) role=analyst, INSERT INTO bio_blocks через join bio_pages → 0 строк (write-политика только на bio_pages).
-- 3) INSERT INTO impersonation_sessions (reason='ok') → ошибка CHECK length>=5.
-- 4) role=copywriter, INSERT INTO payments → 0 строк (биллинг только владелец/админ).
