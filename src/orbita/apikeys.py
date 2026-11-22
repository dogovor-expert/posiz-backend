"""API-ключи платформы Posiz: префикс ``psz_live_``, скоупы и валидация.

Публичный контракт (важен для интеграций и документации):

* ключ выглядит как ``psz_live_<32 символа base62>`` — префикс виден в логах,
  секретах и в тексте ошибок, поэтому он и есть публичное имя продукта;
* в БД (``entity_docs``/``api_keys``) хранится **только хэш** `sha256`, наружу
  отдаётся ``hint`` вида ``psz_live_9f2c…7b1e`` — полный ключ показывается один раз;
* скоупы: ``read | write | publish`` — MCP и интеграции не получают больше прав,
  чем выдал пользователь (ADR 0008);
* ключи с устаревшим префиксом ``orb_live_`` (рабочее название продукта)
  **отклоняются**: при ребрендинге старые ключи отзываются, а не «работают тихо».
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import string
from datetime import datetime, timezone

from . import tenancy
from .docs import Collection

PREFIX = "psz_live_"
LEGACY_PREFIXES = ("orb_live_", "orb_test_")
SCOPES = ("read", "write", "publish")

KEYS = Collection("api_keys", id_field="id")
_BODY_ALPHABET = string.ascii_lowercase + string.digits


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def hint(key: str) -> str:
    """Маска для интерфейса: префикс + 4 символа + хвост."""
    body = key.split("_", 2)[-1]
    return f"{PREFIX}{body[:4]}…{body[-4:]}"


def looks_like_key(value: str) -> bool:
    return value.startswith(PREFIX) and len(value) == len(PREFIX) + 32


def legacy(value: str) -> bool:
    return any(value.startswith(p) for p in LEGACY_PREFIXES)


def issue(name: str, scopes: list[str] | None = None, workspace_id: str = "ws_de") -> dict:
    """Выпустить ключ. Полное значение возвращается ровно один раз."""
    chosen = [s for s in (scopes or ["read", "publish"]) if s in SCOPES]
    if not chosen:
        raise ValueError(f"нужен хотя бы один скоуп из {SCOPES}")
    body = "".join(secrets.choice(_BODY_ALPHABET) for _ in range(32))
    key = PREFIX + body
    workspace_id = workspace_id or tenancy.scope_workspace() or "ws_de"
    record = {
        "id": f"key_{secrets.token_hex(16)}",     # 128 бит: id не должен подбираться
        "workspace_id": workspace_id,
        "name": name,
        "scopes": chosen,
        "hint": hint(key),
        "hash": _hash(key),
        "status": "active",
        "created_at": _now(),
        "last_used_at": None,
    }
    KEYS.put(record)
    return {**{k: v for k, v in record.items() if k != "hash"}, "key": key}


def list_keys(workspace_id: str | None = None) -> list[dict]:
    """Ключи пространства (по умолчанию — текущего контекста).

    Раньше значением по умолчанию был ``"ws_de"``: клиент видел демо-ключи, а выпущенные
    им ключи уходили в демо-пространство. Теперь область берётся из контекста запроса.
    """
    scope = workspace_id or tenancy.scope_workspace()
    rows = KEYS.all()
    if scope is not None:
        rows = [row for row in rows if str(row.get("workspace_id")) == str(scope)]
    return [{k: v for k, v in row.items() if k != "hash"} for row in rows]


def revoke(key_id: str) -> dict:
    row = KEYS.get(key_id)                      # Collection уже сужает по контексту
    if not row:
        return {"revoked": False, "reason": "ключ не найден"}
    row["status"] = "revoked"
    row["revoked_at"] = _now()
    KEYS.put(row)
    return {"revoked": True, "id": key_id}


def verify(key: str, required: str | None = None) -> dict:
    """Проверить ключ и (опционально) скоуп. Возвращает ``{ok, reason, scopes}``."""
    if not key:
        return {"ok": False, "reason": "ключ не передан", "code": "missing"}
    if legacy(key):
        return {"ok": False, "code": "legacy_prefix",
                "reason": "ключ рабочего названия (orb_live_) отозван при ребрендинге — "
                          "выпустите новый: psz_live_…"}
    if not looks_like_key(key):
        return {"ok": False, "code": "bad_format",
                "reason": f"ключ должен иметь вид {PREFIX}<32 символа>"}
    digest = _hash(key)
    for row in KEYS.unscoped():       # ключ — самостоятельный идентификатор арендатора
        if not hmac.compare_digest(str(row.get("hash") or ""), digest):
            continue
        if row.get("status") != "active":
            return {"ok": False, "code": "revoked", "reason": "ключ отозван"}
        scopes = row.get("scopes", [])
        if required and required not in scopes:
            return {"ok": False, "code": "scope", "reason": f"нет скоупа {required}",
                    "scopes": scopes}
        row["last_used_at"] = _now()
        KEYS.upsert_unscoped(row)     # запись в пространство самого ключа, а не в контекст
        return {"ok": True, "scopes": scopes, "name": row.get("name"),
                "id": row.get("id"), "workspace_id": row.get("workspace_id")}
    return {"ok": False, "code": "unknown", "reason": "ключ не найден"}


def describe() -> dict:
    return {"prefix": PREFIX, "scopes": list(SCOPES),
            "legacy_prefixes_rejected": list(LEGACY_PREFIXES),
            "format": f"{PREFIX}<32 символа base62>",
            "storage": "sha256 в entity_docs, наружу — только hint"}
