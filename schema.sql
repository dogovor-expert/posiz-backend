-- =====================================================================
-- Posiz — схема PostgreSQL 16+ (posiz.ru)
-- Иерархия изоляции: tenant (организация) → workspace (бренд) → channel_account
-- Применение: psql -d orbita -f schema.sql  (имя БД orbita — внутреннее, не бренд)
-- =====================================================================

-- Расширения — уровень кластера (см. sql/00_cluster_init.sql): их ставит суперпользователь
-- при создании пустого тома. Здесь они в защищённом блоке, чтобы `alembic upgrade head`
-- от непривилегированной роли приложения не падал на «permission denied to create extension».
DO $$
BEGIN
  CREATE EXTENSION IF NOT EXISTS pgcrypto;    -- gen_random_uuid()
  CREATE EXTENSION IF NOT EXISTS vector;      -- pgvector для Brand Brain
  CREATE EXTENSION IF NOT EXISTS pg_trgm;     -- поиск по тексту
EXCEPTION WHEN insufficient_privilege THEN
  RAISE NOTICE 'Расширения не созданы: нужен суперпользователь (см. sql/00_cluster_init.sql)';
END
$$;

-- ---------------------------------------------------------------------
-- Перечисления
-- ---------------------------------------------------------------------
-- 29 каналов: 15 базовых + 14 из v3 (Дзен, TenChat, VK Видео, Rutube, VC.ru, Habr,
-- Pinterest, Яндекс Бизнес, 2ГИС, Discord, Twitch, Kick, Email, Web Push).
-- Список обязан совпадать с orbita/catalog.py: расхождение ловится тестом
-- tests/test_schema_sync.py, иначе INSERT упадёт с invalid input value for enum.
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'channel_kind') THEN
    CREATE TYPE channel_kind AS ENUM (
      -- РФ-ядро
      'max', 'vk', 'telegram', 'ok',
      -- РФ: расширение охвата
      'dzen', 'tenchat', 'vk_video', 'rutube', 'vcru', 'habr',
      'yandex_business', 'twogis',
      -- Глобальные
      'x', 'pinterest', 'linkedin', 'threads', 'instagram', 'facebook',
      'youtube', 'bluesky', 'mastodon', 'gbp',
      -- Мессенджеры и комьюнити
      'vk_messenger', 'whatsapp', 'discord', 'twitch', 'kick',
      -- Собственные каналы
      'email', 'webpush'
    );
  END IF;
END $$;

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'account_status') THEN
    CREATE TYPE account_status AS ENUM ('active', 'needs_reauth', 'rate_limited', 'paused', 'revoked');
  END IF;
END $$;
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'publication_status') THEN
    CREATE TYPE publication_status AS ENUM ('draft', 'queued', 'publishing', 'published', 'failed',
                                            'needs_reauth', 'blocked_by_compliance', 'cancelled',
                                            'retry_wait');
  END IF;
END $$;
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'compliance_verdict') THEN
    CREATE TYPE compliance_verdict AS ENUM ('informational', 'ad_requires_marking', 'forbidden_resource', 'unknown');
  END IF;
END $$;
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'signal_kind') THEN
    CREATE TYPE signal_kind AS ENUM ('url', 'manual', 'voice', 'rss', 'trend', 'recycle', 'law_change');
  END IF;
END $$;
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'member_role') THEN
    -- copywriter и lawyer — рабочие роли контент-процесса: их проверяют RLS-политики,
    -- поэтому они обязаны быть в типе (иначе INSERT роли падает на живой базе)
    CREATE TYPE member_role AS ENUM ('owner', 'admin', 'editor', 'analyst', 'client_viewer',
                                    'copywriter', 'lawyer');
  END IF;
END $$;
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'inbox_kind') THEN
    CREATE TYPE inbox_kind AS ENUM ('dm', 'comment', 'mention', 'review');
  END IF;
END $$;

-- ---------------------------------------------------------------------
-- Арендаторы и доступ
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS tenants (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name            text NOT NULL,
  country         char(2) NOT NULL DEFAULT 'RU',
  plan_code       text NOT NULL DEFAULT 'free',
  data_region     text NOT NULL DEFAULT 'ru-central',   -- 152-ФЗ: хранение ПДн в РФ
  settings        jsonb NOT NULL DEFAULT '{}'::jsonb,   -- профиль «запрещённые каналы» (41-ФЗ) и т.п.
  created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS users (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  email           citext UNIQUE,
  phone           text,
  name            text,
  password_hash   text,
  vk_id           bigint UNIQUE,
  locale          text NOT NULL DEFAULT 'ru',
  -- администратор ПЛАТФОРМЫ (суперадминка), а не владелец воркспейса:
  -- права platform:* не выдаются ни одной роли воркспейса
  is_platform_admin boolean NOT NULL DEFAULT false,
  last_seen_at    timestamptz,
  created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS memberships (
  tenant_id  uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  user_id    uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  role       member_role NOT NULL DEFAULT 'editor',
  PRIMARY KEY (tenant_id, user_id)
);

CREATE TABLE IF NOT EXISTS api_keys (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id   uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  name        text NOT NULL,
  key_hash    text NOT NULL,               -- sha256, сам ключ не хранится
  scopes      text[] NOT NULL DEFAULT '{read}',
  last_used_at timestamptz,
  created_at  timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------
-- Бренды (workspace) и каналы
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS workspaces (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id   uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  name        text NOT NULL,                       -- «dogovor.expert»
  site_url    text,
  timezone    text NOT NULL DEFAULT 'Europe/Moscow',
  brand       jsonb NOT NULL DEFAULT '{}'::jsonb,  -- tone, palette, logo, stoplist, cta_default
  created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON workspaces (tenant_id);

CREATE TABLE IF NOT EXISTS channel_accounts (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id      uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  channel           channel_kind NOT NULL,
  external_id       text NOT NULL,                 -- id канала/сообщества/страницы
  title             text,
  audience_size     integer,                        -- подписчики (порог реестра 10 000)
  status            account_status NOT NULL DEFAULT 'active',
  -- секреты: только шифротекст, ключ — в Vault/KMS
  token_ciphertext  bytea,
  token_key_version integer,
  token_expires_at  timestamptz,
  scopes            text[],
  capabilities      jsonb NOT NULL DEFAULT '{}'::jsonb, -- что умеет этот конкретный аккаунт
  limits            jsonb NOT NULL DEFAULT '{}'::jsonb, -- например: posts_per_24h = 100, rps = 3
  health            jsonb NOT NULL DEFAULT '{}'::jsonb, -- например: last_ok_at, error_rate = 0.01
  created_at        timestamptz NOT NULL DEFAULT now(),
  UNIQUE (channel, external_id)
);
CREATE INDEX ON channel_accounts (workspace_id, channel);

-- ---------------------------------------------------------------------
-- Brand Brain: источники знаний и чанки
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS knowledge_sources (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id  uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  kind          text NOT NULL,               -- 'site' | 'pdf' | 'docx' | 'video' | 'post_archive' | 'manual'
  location      text,                        -- URL или путь в S3
  status        text NOT NULL DEFAULT 'pending', -- pending|indexing|ready|failed
  pages_total   integer,
  pages_done    integer DEFAULT 0,
  last_error    text,
  updated_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS knowledge_cards (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id  uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  source_id     uuid REFERENCES knowledge_sources(id) ON DELETE CASCADE,
  kind          text NOT NULL,              -- product|usp|audience|objection|case|proof|tone|stoplist|faq
  title         text NOT NULL,
  body          text NOT NULL,
  facts         jsonb NOT NULL DEFAULT '[]'::jsonb,
  source_url    text,
  confidence    numeric(3,2) NOT NULL DEFAULT 0.80,
  valid_until   timestamptz,                -- устаревание фактов (цены, законы)
  created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON knowledge_cards (workspace_id, kind);

CREATE TABLE IF NOT EXISTS knowledge_chunks (
  id          bigserial PRIMARY KEY,
  card_id     uuid NOT NULL REFERENCES knowledge_cards(id) ON DELETE CASCADE,
  workspace_id uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  content     text NOT NULL,
  tsv         tsvector GENERATED ALWAYS AS (to_tsvector('russian', content)) STORED,
  embedding   vector(1024),                 -- размерность под выбранную модель эмбеддингов
  tokens      integer
);
CREATE INDEX ON knowledge_chunks USING gin (tsv);
CREATE INDEX ON knowledge_chunks USING hnsw (embedding vector_cosine_ops);
CREATE INDEX ON knowledge_chunks (workspace_id);

-- ---------------------------------------------------------------------
-- Документные коллекции доменов, ещё не переведённых на типизированные
-- таблицы (Brand Brain, Инбокс, Bio, автоматизации, мониторинг, A/B).
-- ADR 0014: collection = имя БУДУЩЕЙ таблицы, переход — замена Collection.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS entity_docs (
  collection   text NOT NULL,
  id           text NOT NULL,
  workspace_id uuid NOT NULL,
  payload      jsonb NOT NULL DEFAULT '{}'::jsonb,
  position     integer NOT NULL DEFAULT 0,
  created_at   timestamptz NOT NULL DEFAULT now(),
  updated_at   timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (collection, id)
);
CREATE INDEX IF NOT EXISTS entity_docs_ws ON entity_docs (collection, workspace_id);
CREATE INDEX IF NOT EXISTS entity_docs_pos ON entity_docs (collection, position);

-- ---------------------------------------------------------------------
-- Контентный граф: signal → brief → variants → publications
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS signals (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id  uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  kind          signal_kind NOT NULL,
  title         text NOT NULL,
  source_url    text,
  raw_text      text,
  goal          text,                        -- lead|trust|launch|engage|educate
  status        text NOT NULL DEFAULT 'new', -- new|briefed|generated|scheduled|done|archived
  created_by    uuid REFERENCES users(id),
  created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON signals (workspace_id, status, created_at DESC);

CREATE TABLE IF NOT EXISTS briefs (
  workspace_id   uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,  -- граница изоляции (RLS)
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  signal_id    uuid NOT NULL REFERENCES signals(id) ON DELETE CASCADE,
  version      integer NOT NULL DEFAULT 1,
  payload      jsonb NOT NULL,   -- {audience, offer, thesis, cta, proof_points[], restrictions[]}
  ai_run_id    uuid,
  approved_by  uuid REFERENCES users(id),
  created_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS assets (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id  uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  kind          text NOT NULL,          -- image|video|document|generated_card
  storage_key   text NOT NULL,          -- S3
  mime          text,
  width         integer,
  height        integer,
  bytes         bigint,
  meta          jsonb NOT NULL DEFAULT '{}'::jsonb,  -- {template, headline, variants:{'1x1':'key',...}}
  created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS variants (
  workspace_id   uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,  -- граница изоляции (RLS)
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  signal_id      uuid NOT NULL REFERENCES signals(id) ON DELETE CASCADE,
  channel        channel_kind NOT NULL,
  format         text NOT NULL,           -- longread|short|thread|carousel|story|poll|video_script
  title          text,
  body           text NOT NULL,
  body_html      text,
  media_ids      uuid[] NOT NULL DEFAULT '{}',
  link_url       text,
  link_in_reply  boolean NOT NULL DEFAULT false, -- экономия на X ($0,20 vs $0,015)
  hashtags       text[] NOT NULL DEFAULT '{}',
  ai_run_id      uuid,
  char_count     integer GENERATED ALWAYS AS (char_length(body)) STORED,
  review_score   numeric(3,2),             -- оценка Editor-агента
  created_at     timestamptz NOT NULL DEFAULT now(),
  UNIQUE (signal_id, channel, format)
);
CREATE INDEX ON variants (signal_id);

CREATE TABLE IF NOT EXISTS publications (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id      uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  variant_id        uuid NOT NULL REFERENCES variants(id) ON DELETE CASCADE,
  channel_account_id uuid NOT NULL REFERENCES channel_accounts(id) ON DELETE CASCADE,
  scheduled_at      timestamptz NOT NULL,
  status            publication_status NOT NULL DEFAULT 'queued',
  idempotency_key   text NOT NULL,
  external_id       text,
  external_url      text,
  attempt_count     integer NOT NULL DEFAULT 0,
  last_error        text,
  cost_estimate_rub numeric(10,2) NOT NULL DEFAULT 0,
  cost_actual_rub   numeric(10,2),
  -- вложения публикации: id артефактов медиа-конвейера (ADR 0023). Ссылки на них
  -- генерируются в момент отправки, потому что каждая подписана и живёт час
  media_ids         jsonb NOT NULL DEFAULT '[]'::jsonb,
  approved_by       uuid REFERENCES users(id),
  approved_at       timestamptz,
  published_at      timestamptz,
  created_at        timestamptz NOT NULL DEFAULT now(),
  UNIQUE (idempotency_key)
);
CREATE INDEX ON publications (workspace_id, scheduled_at);
CREATE INDEX ON publications (status, scheduled_at) WHERE status IN ('queued','publishing');

CREATE TABLE IF NOT EXISTS publication_attempts (
  id              bigserial PRIMARY KEY,
  publication_id  uuid NOT NULL REFERENCES publications(id) ON DELETE CASCADE,
  workspace_id    uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,  -- граница изоляции
  attempt         integer NOT NULL,
  worker_id       text,
  idempotency_key text,
  started_at      timestamptz NOT NULL DEFAULT now(),
  finished_at     timestamptz,
  outcome         text,        -- published|retry_wait|failed|needs_reauth|uncertain
  external_id     text,
  error           text
);
-- Одна открытая попытка на публикацию: закрывает окно «таймаут после отправки»
CREATE UNIQUE INDEX IF NOT EXISTS publication_attempts_inflight
  ON publication_attempts (publication_id) WHERE finished_at IS NULL;
CREATE INDEX IF NOT EXISTS publication_attempts_pub
  ON publication_attempts (publication_id, attempt DESC);

-- ---------------------------------------------------------------------
-- Комплаенс (38-ФЗ / ЕРИР / реестр блогеров)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS compliance_rules (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  code         text NOT NULL,        -- 'AD_MARKING_REQUIRED', 'FORBIDDEN_RESOURCE', …
  version      integer NOT NULL,
  jurisdiction char(2) NOT NULL DEFAULT 'RU',
  severity     text NOT NULL,        -- info|warn|block
  title        text NOT NULL,
  logic        jsonb NOT NULL,       -- машиночитаемое правило
  effective_from date NOT NULL,
  effective_to   date,
  source_url   text
);

CREATE TABLE IF NOT EXISTS compliance_reviews (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  variant_id      uuid NOT NULL REFERENCES variants(id) ON DELETE CASCADE,
  verdict         compliance_verdict NOT NULL,
  reasons         jsonb NOT NULL,        -- [{rule_code, severity, message}]
  marking_block   text,                  -- готовый блок «Реклама. … erid: …»
  erid            text,
  advertiser_name text,
  advertiser_inn  text,
  reviewed_by     uuid REFERENCES users(id),
  rules_version   integer NOT NULL,
  created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS ord_report_queue (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  publication_id uuid NOT NULL REFERENCES publications(id) ON DELETE CASCADE,
  ord_provider   text,                   -- yandex|vk|ozon|mts
  payload        jsonb NOT NULL,
  status         text NOT NULL DEFAULT 'pending', -- pending|sent|failed
  due_at         timestamptz             -- до 30-го числа следующего месяца
);

-- ---------------------------------------------------------------------
-- Инбокс
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS inbox_threads (
  id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id       uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  channel_account_id uuid NOT NULL REFERENCES channel_accounts(id) ON DELETE CASCADE,
  kind               inbox_kind NOT NULL,
  external_thread_id text NOT NULL,
  participant_name   text,
  participant_ref    text,             -- псевдоним, ПДн минимизируем
  status             text NOT NULL DEFAULT 'open',   -- open|waiting|escalated|closed
  last_message_at    timestamptz,
  created_at         timestamptz NOT NULL DEFAULT now(),
  UNIQUE (channel_account_id, external_thread_id)
);
CREATE INDEX ON inbox_threads (workspace_id, status, last_message_at DESC);

CREATE TABLE IF NOT EXISTS inbox_messages (
  id                 bigserial PRIMARY KEY,
  thread_id          uuid NOT NULL REFERENCES inbox_threads(id) ON DELETE CASCADE,
  direction          text NOT NULL,     -- in|out
  external_message_id text,
  body               text,
  attachments        jsonb NOT NULL DEFAULT '[]'::jsonb,
  ai_suggested       boolean NOT NULL DEFAULT false,
  sent_by            uuid REFERENCES users(id),
  occurred_at        timestamptz NOT NULL,
  UNIQUE (thread_id, external_message_id)
);

-- ---------------------------------------------------------------------
-- Аналитика
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS analytics_events (
  -- PK партиционированной таблицы обязан включать ключ партиционирования
  -- (occurred_at), иначе PostgreSQL отклоняет CREATE TABLE целиком.
  id             bigserial NOT NULL,
  workspace_id   uuid NOT NULL,
  publication_id uuid,
  channel        channel_kind,
  kind           text NOT NULL,       -- impression|reaction|comment|click|follow|unsubscribe
  value          numeric NOT NULL DEFAULT 1,
  occurred_at    timestamptz NOT NULL,
  meta           jsonb NOT NULL DEFAULT '{}'::jsonb,
  PRIMARY KEY (id, occurred_at)
) PARTITION BY RANGE (occurred_at);

-- PARTITION OF не поддерживает IF NOT EXISTS: повторный прогон baseline падал бы на
-- «relation already exists». Секции создаём под проверкой to_regclass.
DO $$
BEGIN
  IF to_regclass('analytics_events_2026_11') IS NULL THEN
    CREATE TABLE analytics_events_2026_11 PARTITION OF analytics_events
      FOR VALUES FROM ('2026-11-01') TO ('2026-12-01');
  END IF;
  IF to_regclass('analytics_events_2026_12') IS NULL THEN
    CREATE TABLE analytics_events_2026_12 PARTITION OF analytics_events
      FOR VALUES FROM ('2026-12-01') TO ('2027-01-01');
  END IF;
END $$;
CREATE INDEX IF NOT EXISTS analytics_events_ws_time
  ON analytics_events (workspace_id, occurred_at DESC);

CREATE TABLE IF NOT EXISTS publication_metrics_daily (
  publication_id uuid NOT NULL REFERENCES publications(id) ON DELETE CASCADE,
  day            date NOT NULL,
  impressions    integer NOT NULL DEFAULT 0,
  reactions      integer NOT NULL DEFAULT 0,
  comments       integer NOT NULL DEFAULT 0,
  clicks         integer NOT NULL DEFAULT 0,
  cost_rub       numeric(10,2) NOT NULL DEFAULT 0,
  PRIMARY KEY (publication_id, day)
);

-- ---------------------------------------------------------------------
-- AI-трассировка (для evals, стоимости и аудита)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ai_runs (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id   uuid NOT NULL,
  agent          text NOT NULL,          -- strategist|copywriter|adapter|fact_checker|compliance|visual|editor
  provider       text NOT NULL,          -- gigachat|yandexgpt|deepseek|openai|local
  model          text NOT NULL,
  prompt_version text NOT NULL,
  input_tokens   integer,
  output_tokens  integer,
  cost_rub       numeric(10,4),
  latency_ms     integer,
  status         text NOT NULL DEFAULT 'ok',
  payload        jsonb,                  -- обрезанный вход/выход (без ПДн)
  created_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON ai_runs (workspace_id, created_at DESC);

-- ---------------------------------------------------------------------
-- Биллинг и аудит
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS subscriptions (
  tenant_id     uuid PRIMARY KEY REFERENCES tenants(id) ON DELETE CASCADE,
  plan_code     text NOT NULL,
  status        text NOT NULL DEFAULT 'trialing',
  period_start  date,
  period_end    date,
  generations_left integer,
  seats         integer NOT NULL DEFAULT 1,
  provider_ref  text
);

CREATE TABLE IF NOT EXISTS usage_counters (
  tenant_id   uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  period      date NOT NULL,
  metric      text NOT NULL,       -- publications|generations|inbox_replies|ai_tokens
  value       bigint NOT NULL DEFAULT 0,
  PRIMARY KEY (tenant_id, period, metric)
);

CREATE TABLE IF NOT EXISTS audit_log (
  id          bigserial PRIMARY KEY,
  tenant_id   uuid NOT NULL,
  actor_id    uuid,
  actor_kind  text NOT NULL DEFAULT 'user',  -- user|api_key|system|agent
  action      text NOT NULL,
  object_type text,
  object_id   uuid,
  meta        jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON audit_log (tenant_id, created_at DESC);

-- ---------------------------------------------------------------------
-- Изоляция арендаторов (RLS)
-- ---------------------------------------------------------------------
-- Политики здесь НЕ создаются: единственный источник истины — sql/rls_policies.sql
-- (его выполняет baseline-миграция 0001 и миграция 0006). Раньше дубли в этом файле создавали
-- `publications_tenant_isolation` с прямым `current_setting('app.tenant_id', true)::uuid`:
-- на пустом контексте она падала («invalid input syntax for type uuid: пустая строка»),
-- а рядом с новой рамкой просто дублировала её. Схема — это схема.
