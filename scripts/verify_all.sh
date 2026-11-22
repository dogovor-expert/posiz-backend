#!/usr/bin/env bash
# Одна команда «проверить всё» — то же, что делает CI, но локально.
#
# Зачем: перед итерацией проверка состояла из шести команд, которые я собирал по памяти
# (`env -u …`, свои URL, свои пароли). Ошибка в одной переменной давала ложный «зелёный»
# прогон — например, без `REDIS_ADMIN_URL` два теста падали на правах, а без
# `BACKUP_DATABASE_URL` проверка восстановления просто пропускалась.
#
# Коды возврата: 0 — всё зелёное; 1 — есть падения; 2 — не заданы обязательные переменные.
set -uo pipefail

cd "$(dirname "$0")/.." || exit 2
PY="${PYTHON_BIN:-python3}"
FAILED=0
SKIPPED_INFRA=0

step() { printf '\n\033[1m── %s\033[0m\n' "$1"; }
ok()   { printf '   ✅ %s\n' "$1"; }
bad()  { printf '   ❌ %s\n' "$1"; FAILED=1; }

step "Быстрый набор (память/SQLite) — инфраструктура не нужна"
if env -u DATABASE_URL -u REDIS_URL -u ORBITA_TEST_PG_URL -u ORBITA_TEST_REDIS_URL \
      -u BACKUP_DATABASE_URL -u REDIS_ADMIN_URL \
      PYTHONPATH=src "$PY" -m pytest tests -q >/tmp/orbita_fast.log 2>&1; then
  ok "$(tail -1 /tmp/orbita_fast.log)"
else
  bad "быстрый набор падает — см. /tmp/orbita_fast.log"
  tail -20 /tmp/orbita_fast.log
fi

if [ -z "${ORBITA_TEST_PG_URL:-}" ] || [ -z "${ORBITA_TEST_REDIS_URL:-}" ]; then
  printf '\n   ⚠  ORBITA_TEST_PG_URL / ORBITA_TEST_REDIS_URL не заданы — живая часть пропущена.\n'
  printf '      Тесты на SQLite не доказывают RLS, права Redis и восстановление бэкапа.\n'
  SKIPPED_INFRA=1
else
  # Гейт читает DATABASE_URL/REDIS_URL, тесты — ORBITA_TEST_*. Раньше это приводило к
  # «тесты зелёные, гейт красный»: подставляем тестовые адреса, если явных нет.
  export DATABASE_URL="${DATABASE_URL:-$ORBITA_TEST_PG_URL}"
  export REDIS_URL="${REDIS_URL:-${REDIS_ADMIN_URL:-$ORBITA_TEST_REDIS_URL}}"
  export JWT_SECRET="${JWT_SECRET:-verify-all-local-secret-at-least-32-chars}"

  step "Интеграция и эксплуатационная зрелость (PostgreSQL + Redis)"
  if PYTHONPATH=src "$PY" -m pytest tests/test_pg_integration.py tests/test_ops_hardening.py \
       -q >/tmp/orbita_live.log 2>&1; then
    ok "$(tail -1 /tmp/orbita_live.log)"
  else
    bad "живые тесты падают — см. /tmp/orbita_live.log"
    tail -20 /tmp/orbita_live.log
  fi

  step "Release gate"
  if PYTHONPATH=src "$PY" -m orbita.release_gate >/tmp/orbita_gate.log 2>&1; then
    ok "$(tail -1 /tmp/orbita_gate.log)"
  else
    bad "гейт не пройден"
    grep -E "❌" -A 2 /tmp/orbita_gate.log || tail -20 /tmp/orbita_gate.log
  fi
fi

step "OpenAPI не отстаёт от кода"
if PYTHONPATH=src "$PY" scripts/dump_openapi.py --check >/tmp/orbita_openapi.log 2>&1; then
  ok "$(tail -1 /tmp/orbita_openapi.log)"
else
  bad "openapi.yaml разошёлся с кодом"
  tail -5 /tmp/orbita_openapi.log
fi

if [ -d ../50-SITE ] && command -v npm >/dev/null 2>&1; then
  step "Сайт (node --test)"
  if ( cd ../50-SITE && npm test >/tmp/orbita_site.log 2>&1 ); then
    ok "$(grep -E '^# (pass|fail)' /tmp/orbita_site.log | tr '\n' ' ')"
  else
    bad "тесты сайта падают — см. /tmp/orbita_site.log"
    grep -E "^not ok" /tmp/orbita_site.log | head -10
  fi
fi

printf '\n'
if [ "$FAILED" != "0" ]; then
  echo "ИТОГ: есть падения."
  exit 1
fi
if [ "$SKIPPED_INFRA" != "0" ]; then
  echo "ИТОГ: зелёно, но БЕЗ живой инфраструктуры — это не доказательство изоляции и бэкапа."
  exit 0
fi
echo "ИТОГ: всё зелёное на целевой инфраструктуре."
