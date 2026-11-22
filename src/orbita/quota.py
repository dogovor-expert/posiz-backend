"""Quota Engine: подписка + докупаемые пакеты.

Счётчики живут в таблице ``quota_counters`` (период = календарный месяц): при
``DATA_LAYER=db`` списание идёт атомарным UPDATE, поэтому два воркера не «перерасходуют»
пакет. В памяти остаётся демо-режим для каркаса и тестов. Списание — только за принятые
результаты: черновики и отклонённые правки бесплатны.
"""
from __future__ import annotations

from . import tenancy
from .persistence import database

#: Токены считаются отдельно от генераций: одна генерация может стоить 600 токенов, а
#: может 20 000 — «штуки» этого не показывают. Лимит — страховка от «дорогого» клиента,
#: а не тариф: пакеты продаются в генерациях.
DEFAULT = {"generations": 600, "publications": 300, "channels": 8, "brands": 1,
           "storage_gb": 10, "llm_tokens": 2_000_000}
PACKS = {"generations_300": {"key": "generations", "amount": 300, "price_rub": 690},
         "publications_200": {"key": "publications", "amount": 200, "price_rub": 490},
         "storage_10": {"key": "storage_gb", "amount": 10, "price_rub": 390}}


class QuotaExceeded(RuntimeError):
    def __init__(self, key: str, used: float, limit: float):
        super().__init__(f"квота {key} исчерпана: {used}/{limit}")
        self.key, self.used, self.limit = key, used, limit


QUOTAS: dict[str, dict[str, dict]] = {
    "ws_de": {"generations": {"used": 340, "limit": 600},
              "publications": {"used": 58, "limit": 300},
              "channels": {"used": 4, "limit": 8},
              "brands": {"used": 1, "limit": 1},
              "storage_gb": {"used": 1.2, "limit": 10},
              "llm_tokens": {"used": 0, "limit": 2_000_000}},
}


def _db():
    return database()


def _db_counters(workspace_id: str) -> dict | None:
    """Счётчики из БД; отсутствующие ключи создаются с лимитами по умолчанию."""
    db = _db()
    if db is None:
        return None
    for key, limit in DEFAULT.items():
        db.quota_ensure(workspace_id, key, limit)
    return db.quota_rows(workspace_id)


def _scope(workspace_id: str | None) -> str:
    """Пространство квоты: явное значение → контекст запроса → демо-пространство.

    Раньше значения по умолчанию были ``"ws_de"``: клиент A видел и тратил квоту
    демо-пространства, а списание для клиента B уменьшало ту же общую цифру. То есть
    лимиты существовали, но не для клиента.
    """
    return workspace_id or tenancy.scope_workspace() or "ws_de"


def get(workspace_id: str | None = None) -> dict:
    workspace_id = _scope(workspace_id)
    if (from_db := _db_counters(workspace_id)) is not None:
        return {"workspace_id": workspace_id, "counters": from_db,
                "percent": {k: round(c["used"] / c["limit"] * 100, 1) if c["limit"] else 0
                            for k, c in from_db.items()}}
    counters = QUOTAS.setdefault(workspace_id, {k: {"used": 0, "limit": v} for k, v in DEFAULT.items()})
    return {"workspace_id": workspace_id, "counters": counters,
            "percent": {k: round(c["used"] / c["limit"] * 100, 1) if c["limit"] else 0
                        for k, c in counters.items()}}


def consume(key: str, amount: float = 1, workspace_id: str | None = None) -> dict:
    workspace_id = _scope(workspace_id)
    if (db := _db()) is not None:
        counters = _db_counters(workspace_id) or {}
        current = counters.get(key, {"used": 0, "limit": DEFAULT.get(key, 0)})
        used, limit = db.quota_consume(workspace_id, key, amount)
        if used + amount > limit:                     # списание не прошло — лимит исчерпан
            raise QuotaExceeded(key, used, limit)
        return {"key": key, "used": used, "limit": limit}
    counters = QUOTAS.setdefault(workspace_id, {k: {"used": 0, "limit": v} for k, v in DEFAULT.items()})
    counter = counters[key]
    if counter["used"] + amount > counter["limit"]:
        raise QuotaExceeded(key, counter["used"], counter["limit"])
    counter["used"] += amount
    return {"key": key, **counter}


def buy_pack(pack_code: str, workspace_id: str | None = None) -> dict:
    workspace_id = _scope(workspace_id)
    pack = PACKS.get(pack_code)
    if not pack:
        return {"error": f"пакет {pack_code} не найден", "available": list(PACKS)}
    if (db := _db()) is not None:
        _db_counters(workspace_id)
        used, limit = db.quota_add_limit(workspace_id, pack["key"], pack["amount"])
        return {"pack": pack_code, "price_rub": pack["price_rub"],
                "counter": {"used": used, "limit": limit},
                "note": "пакет активирован, действует до конца периода"}
    counters = QUOTAS.setdefault(workspace_id, {k: {"used": 0, "limit": v} for k, v in DEFAULT.items()})
    counters[pack["key"]]["limit"] += pack["amount"]
    return {"pack": pack_code, "price_rub": pack["price_rub"],
            "counter": counters[pack["key"]], "note": "пакет активирован, действует до конца периода"}


def catalog() -> dict:
    return {"packs": PACKS, "plans": {
        "free": {"price_rub": 0, "generations": 30},
        "start": {"price_rub": 990, "generations": 150},
        "business": {"price_rub": 2490, "generations": 600},
        "agency": {"price_rub": 4990, "generations": 1500},
        "enterprise": {"price_rub": 12900, "generations": 6000},
    }}
