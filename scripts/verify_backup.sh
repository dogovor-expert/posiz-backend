#!/usr/bin/env bash
# Проверка восстановления из резервной копии — не «описана в документе», а выполнена.
#
# Смысл: резервная копия, из которой никто не восстанавливался, — это не копия, а надежда.
# Скрипт делает полный цикл на текущей базе:
#   1. pg_dump (custom format) — так же, как это делает эксплуатация;
#   2. создаёт временную базу;
#   3. pg_restore в неё;
#   4. сверяет число таблиц, ревизию Alembic и число RLS-политик с исходной базой;
#   5. удаляет временную базу.
#
# Требования: pg_dump/pg_restore/psql текущей версии; права на создание временной базы.
# Если прав нет, скрипт не притворяется, что проверка прошла: exit code 3 и явная причина.
#
# Использование:
#   DATABASE_URL=postgresql+psycopg://user:pass@host:5432/db ./scripts/verify_backup.sh
set -uo pipefail

# Источник дампа — роль резервного копирования (BYPASSRLS). Рабочая роль для pg_dump
# не годится: у таблиц FORCE ROW LEVEL SECURITY, и дамп падает на политиках.
RAW_URL="${BACKUP_DATABASE_URL:-}"
if [ -z "$RAW_URL" ]; then
  echo "НЕТ BACKUP_DATABASE_URL (роль с BYPASSRLS): pg_dump под рабочей ролью не снимается" >&2
  exit 2
fi
# Временную базу создаём от рабочей роли/суперпользователя

# SQLAlchemy-схема (postgresql+psycopg://) не понимается утилитами PostgreSQL
PG_URL="${RAW_URL/postgresql+psycopg:\/\//postgresql:\/\/}"
PG_URL="${PG_URL/postgresql+asyncpg:\/\//postgresql:\/\/}"

PREFIX="${PG_URL%/*}"                 # всё до имени базы
QUERY=""
BASE="${PG_URL##*/}"
case "$BASE" in
  *\?*) QUERY="?${BASE#*\?}"; BASE="${BASE%%\?*}" ;;
esac
TARGET="${BACKUP_CHECK_DB:-orbita_restore_check_$$}"
TARGET_URL="$PREFIX/$TARGET$QUERY"
# psql с одновременно conninfo-URL и -d теряет хост и уходит на локальный сокет —
# поэтому служебная база адресуется отдельным URL, а не флагом -d.
ADMIN_URL="$PREFIX/postgres$QUERY"
WORKDIR="$(mktemp -d)"
DUMP="$WORKDIR/dump.pgc"

drop_target() {
  psql "$ADMIN_URL" -v ON_ERROR_STOP=1 -c "DROP DATABASE IF EXISTS \"$TARGET\"" >/dev/null 2>&1 \
    || sudo -n -u postgres psql -d postgres -v ON_ERROR_STOP=1 \
         -c "DROP DATABASE IF EXISTS \"$TARGET\"" >/dev/null 2>&1
}

cleanup() {
  drop_target || true
  rm -rf "$WORKDIR"
}
trap cleanup EXIT

create_target() {
  # Роль бэкапа имеет CREATEDB именно для этой проверки; если прав нет — честный exit 3
  psql "$ADMIN_URL" -v ON_ERROR_STOP=1 -c "CREATE DATABASE \"$TARGET\"" >/dev/null 2>&1 \
    || sudo -n -u postgres psql -h /tmp -d postgres -v ON_ERROR_STOP=1 \
         -c "CREATE DATABASE \"$TARGET\"" >/dev/null 2>&1 \
    || sudo -n -u postgres createdb "$TARGET" >/dev/null 2>&1
}

echo "→ pg_dump исходной базы"
if ! pg_dump "$PG_URL" -Fc --no-owner --no-privileges -f "$DUMP"; then
  echo "ОШИБКА: pg_dump не выполнился" >&2
  exit 1
fi
echo "  дамп: $(wc -c < "$DUMP" | tr -d ' ') байт"

echo "→ создаю временную базу $TARGET"
if ! create_target; then
  echo "НЕТ ПРАВ на создание временной базы (роль без CREATEDB и нет sudo на postgres)." >&2
  echo "Проверка восстановления НЕ выполнена — это не «успех»." >&2
  exit 3
fi

# Расширения создаются в дампе как CREATE EXTENSION — а это привилегия суперпользователя.
# На боевой базе они уже стоят, поэтому в целевой создаём их заранее: иначе «проверка
# восстановления» упирается в права, а не в данные.
echo "→ расширения в целевой базе"
for ext in vector citext pgcrypto pg_stat_statements; do
  psql "$TARGET_URL" -v ON_ERROR_STOP=1 -c "CREATE EXTENSION IF NOT EXISTS $ext" >/dev/null 2>&1 \
    || sudo -n -u postgres psql -h /tmp -d "$TARGET" -v ON_ERROR_STOP=1 \
         -c "CREATE EXTENSION IF NOT EXISTS $ext" >/dev/null 2>&1 \
    || echo "! расширение $ext не создано (нужен суперпользователь)"
done

# Из оглавления дампа исключаются записи о расширениях: они уже созданы в целевой базе
# (и принадлежат суперпользователю), а COMMENT ON EXTENSION от лица роли бэкапа падает.
pg_restore -l "$DUMP" | grep -v -E "^[0-9]+; [0-9]+ [0-9]+ (EXTENSION|COMMENT - EXTENSION)" \
  > "$WORKDIR/toc" || true

echo "→ pg_restore"
if ! pg_restore --no-owner --no-privileges --exit-on-error -L "$WORKDIR/toc" \
      --dbname="$TARGET_URL" "$DUMP" 2>"$WORKDIR/restore.log"; then
  echo "ОШИБКА восстановления:" >&2
  tail -5 "$WORKDIR/restore.log" >&2
  exit 1
fi

count_tables() {
  psql "$1" -tAc "SELECT count(*) FROM information_schema.tables WHERE table_schema='public'" | tr -d ' '
}
count_policies() {
  psql "$1" -tAc "SELECT count(*) FROM pg_policies WHERE schemaname='public'" | tr -d ' '
}
revision() {
  psql "$1" -tAc "SELECT version_num FROM alembic_version" 2>/dev/null | tr -d ' '
}

SRC_TABLES="$(count_tables "$PG_URL")";   DST_TABLES="$(count_tables "$TARGET_URL")"
SRC_POLICIES="$(count_policies "$PG_URL")"; DST_POLICIES="$(count_policies "$TARGET_URL")"
SRC_REV="$(revision "$PG_URL")";          DST_REV="$(revision "$TARGET_URL")"

echo "  таблиц: исходно $SRC_TABLES, восстановлено $DST_TABLES"
echo "  политик RLS: исходно $SRC_POLICIES, восстановлено $DST_POLICIES"
echo "  ревизия Alembic: исходно ${SRC_REV:-—}, восстановлено ${DST_REV:-—}"

if [ "$SRC_TABLES" != "$DST_TABLES" ]; then
  echo "ОШИБКА: число таблиц не совпадает" >&2
  exit 1
fi
if [ "$SRC_REV" != "$DST_REV" ]; then
  echo "ОШИБКА: ревизия Alembic не совпадает" >&2
  exit 1
fi
# Политики — часть схемы: без них «восстановили», но изоляция пропала
if [ "$SRC_POLICIES" != "$DST_POLICIES" ]; then
  echo "ОШИБКА: восстановленная база потеряла RLS-политики" >&2
  exit 1
fi

echo "OK: копия восстанавливается; таблицы, ревизия и политики RLS совпадают"
