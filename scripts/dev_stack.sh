#!/usr/bin/env bash
# Локальный PostgreSQL + Redis для тестов без Docker.
#
# Зачем скрипт: тесты изоляции и атомарности должны идти на целевой инфраструктуре.
# В окружениях без Docker (CI-раннеры, песочницы) проще поднять пакеты напрямую —
# скрипт делает это воспроизводимо и печатает готовые переменные окружения.
#
#   ./scripts/dev_stack.sh up      # initdb + старт + роль orbita_app + база orbita
#   ./scripts/dev_stack.sh env     # напечатать export-строки для pytest
#   ./scripts/dev_stack.sh down    # остановить
#
# Требуется: postgresql-17 (+ postgresql-17-pgvector) и redis-server в PATH.
set -euo pipefail

PG_BIN="${PG_BIN:-/usr/lib/postgresql/17/bin}"
PYTHON_BIN="${PYTHON_BIN:-python3}"      # интерпретатор с alembic/sqlalchemy в окружении
PGDATA="${PGDATA:-/var/lib/pgdata}"
PGLOG="${PGLOG:-/tmp/postgres-orbita.log}"
DB_PASSWORD="${ORBITA_DB_PASSWORD:-orbita_local_pw}"
REDIS_PASSWORD="${ORBITA_REDIS_PASSWORD:-redis_local_pw}"
REDIS_API_PASSWORD="${ORBITA_REDIS_API_PASSWORD:-redis_api_pw}"
REDIS_WORKER_PASSWORD="${ORBITA_REDIS_WORKER_PASSWORD:-redis_worker_pw}"
DB_NAME="${DB_NAME:-orbita}"
BACKUP_PASSWORD="${ORBITA_BACKUP_PASSWORD:-orbita_backup_pw}"

as_postgres() { sudo -n -u postgres "$@"; }

up() {
  # уже инициализированный кластер (остался от прошлого запуска) не пересоздаём
  # каталог принадлежит postgres с правами 700 — проверяем через sudo
  if ! sudo -n test -s "$PGDATA/PG_VERSION"; then
    echo "→ initdb ($PGDATA, UTF8)"
    sudo -n mkdir -p "$PGDATA" && sudo -n chown postgres:postgres "$PGDATA"
    sudo -n rmdir "$PGDATA" 2>/dev/null || true
    as_postgres "$PG_BIN/initdb" -D "$PGDATA" -A trust -E UTF8 --locale=C.UTF-8 >/dev/null
  fi
  # pg_stat_statements нужен для эксплуатации: без него нельзя ответить на вопрос
  # «какой запрос съедает базу» и не видно, как ведут себя выборки под RLS.
  # Библиотека подключается только через shared_preload_libraries → правка конфига и
  # перезапуск; проверка идемпотентна, чтобы не трогать рабочий кластер каждый раз.
  if ! sudo -n grep -q "pg_stat_statements" "$PGDATA/postgresql.conf" 2>/dev/null; then
    sudo -n tee -a "$PGDATA/postgresql.conf" >/dev/null <<'CONF'
shared_preload_libraries = 'pg_stat_statements'
pg_stat_statements.max = 5000
pg_stat_statements.track = all
CONF
    PG_NEEDS_RESTART=1
  fi
  as_postgres "$PG_BIN/pg_ctl" -D "$PGDATA" -l "$PGLOG" -o "-p 5432 -k /tmp" start >/dev/null 2>&1 || true
  if [ "${PG_NEEDS_RESTART:-0}" = "1" ] && as_postgres "$PG_BIN/pg_ctl" -D "$PGDATA" status >/dev/null 2>&1; then
    as_postgres "$PG_BIN/pg_ctl" -D "$PGDATA" -m fast restart >/dev/null 2>&1 || true
  fi
  sleep 1

  as_postgres psql -h /tmp -d postgres -v ON_ERROR_STOP=1 <<SQL >/dev/null
DO \$\$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'orbita_app') THEN
    CREATE ROLE orbita_app LOGIN PASSWORD '${DB_PASSWORD}'
      NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;
  ELSE
    -- пароль синхронизируется: роль могла остаться от прошлого запуска с другим паролем
    ALTER ROLE orbita_app PASSWORD '${DB_PASSWORD}';
  END IF;

  -- Роль резервного копирования. Нужна именно отдельная роль с BYPASSRLS: у таблиц
  -- включён FORCE ROW LEVEL SECURITY, поэтому pg_dump под рабочей ролью (даже под её
  -- владельцем) читает через политики и падает с «query would be affected by
  -- row-level security policy». Права — только чтение, только для pg_dump.
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'orbita_backup') THEN
    CREATE ROLE orbita_backup LOGIN PASSWORD '${BACKUP_PASSWORD}' NOSUPERUSER CREATEDB BYPASSRLS;
  ELSE
    ALTER ROLE orbita_backup PASSWORD '${BACKUP_PASSWORD}';
    ALTER ROLE orbita_backup BYPASSRLS CREATEDB;
  END IF;
END \$\$;
SQL
  as_postgres psql -h /tmp -d postgres -tAc \
    "SELECT 1 FROM pg_database WHERE datname='${DB_NAME}'" | grep -q 1 \
    || as_postgres psql -h /tmp -d postgres -c "CREATE DATABASE ${DB_NAME} OWNER orbita_app" >/dev/null
  as_postgres psql -h /tmp -d "${DB_NAME}" -v ON_ERROR_STOP=1 \
    -c "CREATE EXTENSION IF NOT EXISTS vector; CREATE EXTENSION IF NOT EXISTS citext; CREATE EXTENSION IF NOT EXISTS pgcrypto; CREATE EXTENSION IF NOT EXISTS pg_stat_statements;" >/dev/null

  # Redis: если он уже поднят с нашим паролем — не перезапускаем
  if redis-cli --no-auth-warning -a "$REDIS_PASSWORD" ping 2>/dev/null | grep -q PONG; then
    echo "→ Redis уже работает"
  else
    redis-server --port 6379 --requirepass "$REDIS_PASSWORD" --daemonize yes \
      --save '' --appendonly no --dir /tmp >/dev/null 2>&1 || true
    sleep 1
    redis-cli --no-auth-warning -a "$REDIS_PASSWORD" ping >/dev/null \
      || echo "! Redis не отвечает на пароль из ORBITA_REDIS_PASSWORD — вероятно, уже запущен другой экземпляр"
  fi

  # Redis ACL: приложение работает двумя пользователями с разными правами.
  # Права живут в scripts/redis_acl.sh — тот же файл использует CI, чтобы контуры
  # не разъезжались.
  REDIS_HOST=127.0.0.1 REDIS_PORT=6379 \
  REDIS_PASSWORD="$REDIS_PASSWORD" \
  REDIS_API_PASSWORD="$REDIS_API_PASSWORD" \
  REDIS_WORKER_PASSWORD="$REDIS_WORKER_PASSWORD" \
    bash "$(dirname "$0")/redis_acl.sh" || true

  echo "→ PostgreSQL и Redis подняты; применяю миграции"
  ( cd "$(dirname "$0")/.." \
    && DATABASE_URL="postgresql+psycopg://orbita_app:${DB_PASSWORD}@127.0.0.1:5432/${DB_NAME}" \
       PYTHONPATH=src "$PYTHON_BIN" -m alembic upgrade head )
  # Права роли бэкапа выдаются после миграций: таблицы уже существуют
  as_postgres psql -h /tmp -d "${DB_NAME}" -v ON_ERROR_STOP=1 >/dev/null <<SQL || true
GRANT CONNECT ON DATABASE ${DB_NAME} TO orbita_backup;
GRANT USAGE ON SCHEMA public TO orbita_backup;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO orbita_backup;
-- последовательности нужны pg_dump: без USAGE он падает на last_value
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO orbita_backup;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO orbita_backup;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO orbita_backup;
SQL
  env
}

env() {
  cat <<ENV
export DATABASE_URL="postgresql+psycopg://orbita_app:${DB_PASSWORD}@127.0.0.1:5432/${DB_NAME}"
export REDIS_URL="redis://:${REDIS_PASSWORD}@127.0.0.1:6379/0"
export ORBITA_TEST_PG_URL="\$DATABASE_URL"
export ORBITA_TEST_REDIS_URL="\$REDIS_URL"
export JWT_SECRET="local-dev-secret-at-least-32-characters"
# Права Redis по ролям: процесс API берёт orbita_api, воркер — orbita_worker
export BACKUP_DATABASE_URL="postgresql+psycopg://orbita_backup:${BACKUP_PASSWORD}@127.0.0.1:5432/${DB_NAME}"
export REDIS_API_PASSWORD="${REDIS_API_PASSWORD}"
export REDIS_WORKER_PASSWORD="${REDIS_WORKER_PASSWORD}"
# export ORBITA_REDIS_ROLE=worker   # для процесса воркера
ENV
}

down() {
  as_postgres "$PG_BIN/pg_ctl" -D "$PGDATA" stop -m fast >/dev/null 2>&1 || true
  redis-cli -a "$REDIS_PASSWORD" shutdown nosave >/dev/null 2>&1 || true
  echo "→ остановлено"
}

case "${1:-up}" in
  up) up ;;
  env) env ;;
  down) down ;;
  *) echo "usage: $0 {up|env|down}" >&2; exit 2 ;;
esac
