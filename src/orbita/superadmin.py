"""Панель суперадминистратора: SaaS-метрики, AI FinOps, имперсонация, здоровье очередей,
редактор правил комплаенса без деплоя.

Доступ только владельцу платформы: IP-allowlist + 2FA (в каркасе — заголовок роли + флаг).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

TENANTS = [
    {"id": "t_1", "name": "Договор Эксперт", "plan": "business", "mrr": 2490, "seats": 3,
     "inn": "7701234567", "type": "legal", "channels": 4, "generations_used": 340, "health": "ok"},
    {"id": "t_2", "name": "Право и Сделки", "plan": "start", "mrr": 990, "seats": 1,
     "inn": None, "type": "individual", "channels": 2, "generations_used": 120, "health": "ok"},
    {"id": "t_3", "name": "Агентство «Контур»", "plan": "agency", "mrr": 4990, "seats": 6,
     "inn": "7809876543", "type": "legal", "channels": 9, "generations_used": 1480, "health": "anomaly"},
    {"id": "t_4", "name": "Клиника «Медиан»", "plan": "business", "mrr": 2490, "seats": 2,
     "inn": None, "type": "individual", "channels": 3, "generations_used": 210, "health": "ok"},
]

AI_SPEND = [
    {"provider": "deepseek", "model": "deepseek-v4-pro", "tokens_in": 1_240_000, "tokens_out": 380_000,
     "cost_rub": 1540.0, "share": 0.71},
    {"provider": "deepseek", "model": "deepseek-flash", "tokens_in": 3_900_000, "tokens_out": 620_000,
     "cost_rub": 340.0, "share": 0.16},
    {"provider": "gemini", "model": "gemini-3.8-flash", "tokens_in": 420_000, "tokens_out": 90_000,
     "cost_rub": 210.0, "share": 0.10},
    {"provider": "openai", "model": "gpt-6.1-sol", "tokens_in": 30_000, "tokens_out": 8_000,
     "cost_rub": 70.0, "share": 0.03},
]

IMPERSONATIONS: list[dict] = []
RULES: dict[str, dict] = {
    "AD_MARKING_REQUIRED": {"code": "AD_MARKING_REQUIRED", "severity": "block", "version": 202611,
                            "title": "Реклама без erid", "active": True},
    "FORBIDDEN_RESOURCE": {"code": "FORBIDDEN_RESOURCE", "severity": "block", "version": 202609,
                           "title": "Реклама на запрещённом ресурсе (IG/FB)", "active": True},
    "BLOGGER_REGISTRY_10K": {"code": "BLOGGER_REGISTRY_10K", "severity": "warn", "version": 202601,
                             "title": "Аудитория > 10 000 — реестр блогеров", "active": True},
    "TOBACCO_ALCOHOL_BAN": {"code": "TOBACCO_ALCOHOL_BAN", "severity": "block", "version": 202606,
                            "title": "Запрещённые категории товаров", "active": True},
}
OPS = {
    "global_pause": False,
    "queues": {"publish": {"pending": 3, "failed_1h": 1}, "inbox": {"pending": 12, "failed_1h": 0},
               "metrics": {"pending": 0, "failed_1h": 0}},
    "success_rate": {"max": 0.998, "vk": 0.996, "telegram": 0.962, "x": 0.991},
}


def metrics() -> dict:
    mrr = sum(t["mrr"] for t in TENANTS)
    legal = sum(t["mrr"] for t in TENANTS if t["type"] == "legal")
    return {
        "mrr": mrr, "arr": mrr * 12, "nrr": 1.14, "churn_monthly": 0.031,
        "trial_to_paid": 0.24, "ltv_rub": 41_800, "active_tenants": len(TENANTS),
        "revenue_split": {"legal": legal, "individual": mrr - legal,
                          "legal_share": round(legal / mrr, 3)},
        "cohorts": [
            {"month": "2026-08", "new": 18, "paid": 4, "retained_3m": 0.72},
            {"month": "2026-09", "new": 24, "paid": 7, "retained_3m": 0.78},
            {"month": "2026-10", "new": 31, "paid": 9, "retained_3m": None},
        ],
    }


def finops() -> dict:
    total = round(sum(r["cost_rub"] for r in AI_SPEND), 2)
    revenue = sum(t["mrr"] for t in TENANTS)
    return {
        "month_cost_rub": total, "per_tenant_rub": round(total / max(1, len(TENANTS)), 2),
        "gross_margin": round(1 - total / revenue, 3),
        "by_model": AI_SPEND,
        "anomalies": [{"tenant": t["name"], "reason": "себестоимость AI > 25% подписки",
                       "tenant_id": t["id"]} for t in TENANTS if t["health"] == "anomaly"],
        "routing": {"heavy": "deepseek-v4-pro", "fast": "deepseek-flash",
                    "vision_fallback": "gemini-3.8-flash", "premium": "gpt-6.1-sol"},
    }


def tenants() -> list[dict]:
    return TENANTS


def impersonate(tenant_id: str, reason: str, actor_role: str = "owner") -> dict:
    tenant = next((t for t in TENANTS if t["id"] == tenant_id), None)
    if not tenant:
        return {"error": "арендатор не найден"}
    if len(reason.strip()) < 5:
        return {"error": "укажите причину (не короче 5 символов) — она попадёт в audit_log"}
    session = {"id": f"imp_{uuid.uuid4().hex[:8]}", "tenant_id": tenant_id, "mode": "support_readonly",
               "reason": reason, "actor_role": actor_role,
               "created_at": datetime.now(timezone.utc), "ttl_minutes": 30}
    IMPERSONATIONS.append(session)
    from . import store

    store.audit(actor_role, "POST", "/admin/impersonate", 200,
                {"tenant_id": tenant_id, "reason": reason, "session": session["id"]})
    return session


def list_impersonations() -> list[dict]:
    return IMPERSONATIONS


def rules() -> list[dict]:
    return list(RULES.values())


def upsert_rule(payload: dict) -> dict:
    code = payload["code"]
    rule = {"code": code, "severity": payload.get("severity", "warn"),
            "version": int(payload.get("version", 202612)), "title": payload.get("title", code),
            "active": payload.get("active", True), "updated_at": datetime.now(timezone.utc)}
    RULES[code] = {**RULES.get(code, {}), **rule}
    return RULES[code]


def toggle_pause(paused: bool) -> dict:
    OPS["global_pause"] = paused
    return {"global_pause": paused,
            "note": "публикации приостановлены, посты автопереносятся на +2 часа"}
