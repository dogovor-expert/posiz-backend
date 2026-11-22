#!/usr/bin/env bash
# Права Redis по ролям — единственный источник истины для dev-стека и CI.
#
# Зачем отдельный файл: раньше ACL настраивался строкой внутри `dev_stack.sh`, и CI
# создавал бы «свои» права копипастой. Копипаста неизбежно расходится — и тогда тест
# проходит на одних правах, а прод работает на других. Теперь оба контура берут права
# отсюда, а `tests/test_ops_hardening.py` проверяет поведение (FLUSHALL/`pub:*`/`sess:*`).
#
# Роли:
#   orbita_api    — сессии, состояния входа, rate-limit, постановка задач ARQ;
#                   НЕ имеет доступа к ключам публикаций (`pub:*`);
#   orbita_worker — очередь, публикации, rate-limit каналов;
#                   НЕ имеет доступа к сессиям (`sess:*`, `user_sessions:*`).
#
# Опасные команды снимаются явно (только `-@dangerous` недостаточно: он не убирает
# FLUSHALL/FLUSHDB у пользователя с полным набором категорий).
set -uo pipefail

REDIS_HOST="${REDIS_HOST:-127.0.0.1}"
REDIS_PORT="${REDIS_PORT:-6379}"
: "${REDIS_PASSWORD:?нужен REDIS_PASSWORD (пароль администратора Redis)}"
: "${REDIS_API_PASSWORD:?нужен REDIS_API_PASSWORD}"
: "${REDIS_WORKER_PASSWORD:?нужен REDIS_WORKER_PASSWORD}"

redis_admin() {
  redis-cli --no-auth-warning -h "$REDIS_HOST" -p "$REDIS_PORT" -a "$REDIS_PASSWORD" "$@"
}

DENY_DANGEROUS=(
  -flushall -flushdb -swapdb -config -acl -shutdown -monitor -debug -replicaof
  -failover -migrate -save -bgsave -bgrewriteaof -module -cluster -client -keys
  -randomkey -slowlog -latency -script
)
BASE_CATS=(+@read +@write +@keyspace +@scripting +@transaction +@pubsub +@connection)
EXTRA_CMDS=(+eval +evalsha +multi +exec +ping +info)

ACL_FAILED=0
redis_admin ACL SETUSER orbita_api on \
  ">$REDIS_API_PASSWORD" resetkeys \
  "~sess:*" "~user_sessions:*" "~state:*" "~auth:state:*" "~rl:*" "~chanrl:*" "~arq:*" \
  -@all "${BASE_CATS[@]}" "${EXTRA_CMDS[@]}" "${DENY_DANGEROUS[@]}" >/dev/null 2>&1 || ACL_FAILED=1

redis_admin ACL SETUSER orbita_worker on \
  ">$REDIS_WORKER_PASSWORD" resetkeys \
  "~arq:*" "~chanrl:*" "~pub:*" "~rl:*" \
  -@all "${BASE_CATS[@]}" "${EXTRA_CMDS[@]}" "${DENY_DANGEROUS[@]}" >/dev/null 2>&1 || ACL_FAILED=1

redis_admin ACL SAVE >/dev/null 2>&1 || true

if [ "$ACL_FAILED" = "1" ]; then
  # Молча продолжать нельзя: приложение запустится под общим пользователем, и
  # «разделение прав» превратится в надпись в документации.
  echo "! Не удалось создать ACL-пользователей orbita_api/orbita_worker." >&2
  echo "  Причина обычно одна: Redis уже запущен с другим паролем (ORBITA_REDIS_PASSWORD)." >&2
  echo "  Проверьте: redis-cli -a <пароль> ACL LIST" >&2
  exit 1
fi

echo "→ Redis ACL: orbita_api / orbita_worker"
