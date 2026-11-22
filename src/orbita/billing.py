"""Биллинг: ЮKassa + СБП, счета и закрывающие документы для юрлиц.

Ключевые требования B2B-клиентов: счёт по ИНН, акт/УПД для бухгалтерии, автопродление
и отмена в два клика. Чеки 54-ФЗ формирует ЮKassa.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

PAYMENTS: list[dict] = []
SUBSCRIPTIONS: dict[str, dict] = {
    "ws_de": {"plan": "business", "price_rub": 2490, "status": "active",
              "renews_at": "2026-12-01", "payment_method": "card+sbp", "autopay": True},
}


def create_invoice(amount_rub: float, plan: str = "business", payer: str = "ООО «Договор Эксперт»",
                   inn: str | None = None, workspace_id: str = "ws_de") -> dict:
    pid = f"pay_{uuid.uuid4().hex[:10]}"
    is_legal = bool(inn)
    payment = {
        "payment_id": pid, "workspace_id": workspace_id, "amount_rub": amount_rub, "plan": plan,
        "payer": payer, "inn": inn, "is_legal_entity": is_legal, "status": "pending",
        "created_at": datetime.now(timezone.utc),
        "confirmation_url": f"https://yoomoney.ru/checkout/payments/v2/contract?orderId={pid}",
        "invoice_pdf": f"https://api.posiz.ru/v1/billing/{pid}/invoice.pdf" if is_legal else None,
        "receipt_url": None, "upd_url": None,
    }
    PAYMENTS.append(payment)
    return payment


def webhook(payment_id: str, event: str = "payment.succeeded") -> dict:
    """Обработать событие платёжного провайдера.

    Идемпотентно: провайдеры повторяют уведомления (и шлют несколько событий по одному
    платежу), поэтому повторный ``payment.succeeded`` не должен заново продлевать
    подписку и пересчитывать период.
    """
    payment = next((p for p in PAYMENTS if p["payment_id"] == payment_id), None)
    if not payment:
        return {"error": "платёж не найден"}
    if event != "payment.succeeded":
        if payment.get("status") == "succeeded":       # позднее «failed» не отменяет оплату
            return {**payment, "ignored": "платёж уже проведён"}
        payment["status"] = "failed"
        return payment
    if payment.get("status") == "succeeded":
        return {**payment, "idempotent": True}
    payment["status"] = "succeeded"
    payment["paid_at"] = datetime.now(timezone.utc)
    payment["receipt_url"] = f"https://receipt.example/{payment_id}"          # 54-ФЗ
    if payment["is_legal_entity"]:
        payment["upd_url"] = f"https://diadoc.example/upd/{payment_id}"      # ЭДО Диадок/СБИС
    sub = SUBSCRIPTIONS.setdefault(payment["workspace_id"], {})
    sub.update(plan=payment["plan"], price_rub=payment["amount_rub"], status="active",
               renews_at=(datetime.now(timezone.utc) + timedelta(days=30)).date().isoformat())
    return payment


def list_payments(workspace_id: str = "ws_de") -> list[dict]:
    return [p for p in reversed(PAYMENTS) if p["workspace_id"] == workspace_id]


def subscription(workspace_id: str = "ws_de") -> dict:
    return SUBSCRIPTIONS.get(workspace_id, {"plan": "free", "status": "active", "price_rub": 0})


def set_autopay(enabled: bool, workspace_id: str = "ws_de") -> dict:
    sub = SUBSCRIPTIONS.setdefault(workspace_id, {})
    sub["autopay"] = enabled
    return sub
