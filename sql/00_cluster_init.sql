-- =====================================================================
-- Кластерная инициализация PostgreSQL для Posiz (выполняется один раз,
-- от суперпользователя при создании пустого тома).
--
-- Зачем отдельный файл: расширения и роли — уровень КЛАСТЕРА, а не приложения.
-- Раньше compose монтировал в initdb всю схему (`schema.sql`, `rls_rbac.sql`, `v3.sql`),
-- после чего `alembic upgrade head` накатывал ТО ЖЕ САМОЕ второй раз — и падал на
-- `CREATE TYPE channel_kind`, потому что тип уже существовал.
--
-- Теперь разделение такое:
--   1. этот файл (superuser): расширения + непривилегированная роль приложения;
--   2. `alembic upgrade head` (роль приложения): вся схема, RLS-политики, данные.
-- Один путь создания схемы — для чистой базы, локального стенда и прода.
--
-- ВАЖНО: API и воркер подключаются ролью `orbita_app` — NOSUPERUSER, NOBYPASSRLS.
-- Суперпользователь обходил бы RLS, и «изоляция» оставалась бы только на бумаге.
-- =====================================================================

CREATE EXTENSION IF NOT EXISTS pgcrypto;      -- gen_random_uuid()
CREATE EXTENSION IF NOT EXISTS vector;        -- pgvector: Brand Brain / RAG
CREATE EXTENSION IF NOT EXISTS pg_trgm;       -- поиск по тексту
CREATE EXTENSION IF NOT EXISTS citext;        -- users.email citext UNIQUE (без него baseline падает)

-- Пароль роли приложения приходит из окружения контейнера (ORBITA_DB_PASSWORD),
-- а не из файла в репозитории. Если переменная не задана — инициализация падает
-- с понятным сообщением: лучше не подняться, чем поднять базу с паролем «orbita_app».
\getenv app_password ORBITA_DB_PASSWORD

\if :{?app_password}
\else
  \echo 'ОШИБКА: ORBITA_DB_PASSWORD не задан — пароль роли orbita_app обязан приходить из окружения'
  \quit 1
\endif

SELECT format('CREATE ROLE orbita_app LOGIN PASSWORD %L '
              'NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS', :'app_password')
 WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'orbita_app')
   AND :'app_password' <> ''
\gexec

-- Роль уже существует (повторный запуск на существующем кластере) — только обновляем пароль
SELECT format('ALTER ROLE orbita_app PASSWORD %L', :'app_password')
 WHERE EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'orbita_app')
   AND :'app_password' <> ''
\gexec

GRANT CONNECT ON DATABASE orbita TO orbita_app;
GRANT USAGE, CREATE ON SCHEMA public TO orbita_app;
GRANT USAGE ON SCHEMA public TO orbita_app;

-- Роль-владелец тестовой/миграционной схемы: приложение создаёт таблицы само
-- (Alembic), поэтому выдаём права на будущие объекты.
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO orbita_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO orbita_app;

COMMENT ON ROLE orbita_app IS
  'Рабочая роль API и воркера: без SUPERUSER/BYPASSRLS, чтобы RLS применялся к приложению.';
