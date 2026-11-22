"""Compliance-движок: детерминированные правила 38-ФЗ / ЕРИР / реестр блогеров.

Правила версионируются; LLM здесь не используется — только проверяемые признаки.
ADR 0004: rules-as-code.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .schemas import ComplianceReason, ComplianceReport

RULES_VERSION = 202611

# Маркеры рекламы (упрощённый линтер; в проде — расширяемый набор + разметка юристами)
AD_PRICE = re.compile(r"(\d[\d\s]{2,}\s*(₽|руб|рублей)|скидк|акци|−\d+%|-\d+%|цена|стоимость|тариф)", re.I)
AD_CTA = re.compile(r"(купи|закажи|запишись|оформ|переходи по ссылке|жми|подпишись|оставь заявк|звони)", re.I)
PROMISE = re.compile(r"(гарантиру(ем|ет) результат|100\s*%|лучш(ий|ая) в россии|навсегда избавит)", re.I)

FORBIDDEN_RESOURCES = {"instagram", "facebook"}          # ч. 10.7 ст. 5 ФЗ «О рекламе»
RESTRICTED_RESOURCES = {"telegram", "youtube", "whatsapp"}  # переходный период ФАС до 31.12.2026


@dataclass(frozen=True)
class VariantContext:
    """Контекст проверки.

    ``advertiser_name``/``advertiser_site`` больше НЕ имеют значений по умолчанию и НЕ
    указывают на юрлицо разработчика: маркировка по ст. 18.1 требует реального
    рекламодателя, и подстановка чужой компании в рекламу клиента — нарушение.
    Реквизиты приходят из профиля workspace (``accounts.workspace(...)['legal_entity']``).
    """

    channel: str
    body: str
    advertiser_name: str | None = None
    advertiser_site: str | None = None
    erid: str | None = None
    # None — «неизвестно»: отсутствие данных о реестре не должно выглядеть как подтверждение.
    # True ставится только после проверки реестра РКН (``registry.verify``), False — по отказу.
    channel_in_blogger_registry: bool | None = None
    audience_size: int = 0


def marking_block(ctx: VariantContext) -> str:
    """Блок маркировки. Пустые реквизиты остаются прочерками — их видно в отчёте."""
    name = ctx.advertiser_name or "________ (реквизиты не заполнены)"
    site = ctx.advertiser_site or "________"
    erid = ctx.erid or "erid: ________________"
    return f"Реклама. {name}, {site}\nerid: {erid}"


ERID_RE = re.compile(r"erid:\s*\S{4,}", re.I)


def is_marked(body: str, ctx: VariantContext | None = None) -> bool:
    """Полная ли маркировка в тексте.

    Раньше проверялось лишь наличие подстроки «erid:» — текст с любым упоминанием
    считался размеченным, даже без слова «Реклама», рекламодателя и реального erid.
    Теперь нужны все три элемента, и erid должен быть валидным токеном, а не прочерком.
    """
    if not body:
        return False
    low = body.lower()
    if "реклама" not in low:
        return False
    match = ERID_RE.search(body)
    if match is None or "____" in match.group(0):
        return False
    if ctx is not None and ctx.advertiser_name and ctx.advertiser_name.lower() not in low:
        return False
    return True


def apply_marking(body: str, ctx: VariantContext) -> str:
    """Добавить блок маркировки в итоговый текст публикации.

    Раньше маркировка существовала только в отчёте проверки: ``check()`` возвращал
    ``marking_block``, но текст, уходящий в канал, оставался немаркированным.
    Теперь вставка — отдельная проверяемая функция, её вызывает воркер перед публикацией.
    """
    block = marking_block(ctx)
    if not body:
        return block
    if is_marked(body, ctx):
        return body                      # полностью размечено — второй раз не дублируем
    return f"{body.rstrip()}\n\n{block}"


#: Правила с источниками и датой действия — для аудита и для страницы «что проверяем».
RULES = (
    {"code": "ADVERTISER_MISSING", "severity": "block", "source": "38-ФЗ ст. 18.1",
     "effective": "2022-09-01", "note": "наименование и сайт рекламодателя в маркировке"},
    {"code": "AD_MARKING_REQUIRED", "severity": "block", "source": "38-ФЗ ст. 18.1",
     "effective": "2022-09-01", "note": "пометка «Реклама», рекламодатель и erid"},
    {"code": "FORBIDDEN_RESOURCE", "severity": "block", "source": "38-ФЗ ч. 10.7 ст. 5",
     "effective": "2025-09-01", "note": "реклама на ресурсах с ограниченным доступом"},
    {"code": "RESTRICTED_RESOURCE_WARN", "severity": "warn", "source": "переходный период ФАС",
     "effective": "2026-02-01", "until": "2026-12-31", "note": "реклама на ограниченных ресурсах — на риск"},
    {"code": "BLOGGER_REGISTRY_10K", "severity": "block", "source": "реестр блогеров РКН",
     "effective": "2024-11-01", "note": "канал 10 000+ вне реестра не размещает рекламу"},
    {"code": "BLOGGER_REGISTRY_UNKNOWN", "severity": "warn", "source": "реестр блогеров РКН",
     "effective": "2024-11-01", "note": "нет подтверждения регистрации канала"},
    {"code": "CLAIM_STOPLIST", "severity": "warn", "source": "38-ФЗ ст. 5 ч. 3",
     "effective": "2006-07-13", "note": "недостоверные обещания результата"},
)


def check(ctx: VariantContext, is_ad: bool | None = None) -> ComplianceReport:
    """Вердикт и причины. ``block`` — публикация запрещена.

    ``is_ad`` можно передать явно: в базе у публикации есть флаг и erid, и это
    авторитетнее линтера по словам. Линтер остаётся запасным путём, когда флага нет
    (например, проверка варианта до создания публикации).
    """
    reasons: list[ComplianceReason] = []

    if is_ad is None:
        has_price = bool(AD_PRICE.search(ctx.body))
        has_cta = bool(AD_CTA.search(ctx.body))
        is_ad = has_price or has_cta

    # 1. Запрещённый ресурс. Блокируется **реклама**, а не любой пост:
    # информирование в таком канале не является размещением рекламы.
    if ctx.channel in FORBIDDEN_RESOURCES:
        if is_ad:
            reasons.append(ComplianceReason(
                rule_code="FORBIDDEN_RESOURCE", severity="block",
                message="Ресурс с ограниченным доступом: размещение рекламы запрещено (ч. 10.7 ст. 5).",
            ))
            return ComplianceReport(verdict="forbidden_resource", reasons=reasons,
                                    rules_version=RULES_VERSION)
        reasons.append(ComplianceReason(
            rule_code="FORBIDDEN_RESOURCE_INFO", severity="warn",
            message="Канал с ограниченным доступом: реклама здесь запрещена, информирование допустимо.",
        ))

    # 2. Ограниченный ресурс (переходный период)
    if ctx.channel in RESTRICTED_RESOURCES and is_ad:
        reasons.append(ComplianceReason(
            rule_code="RESTRICTED_RESOURCE_WARN", severity="warn",
            message="Ресурс с ограниченным доступом: переходный период ФАС до 31.12.2026, реклама — на риск клиента.",
        ))

    # 3. Признаки рекламы → маркировка
    if is_ad:
        if not (ctx.advertiser_name and ctx.advertiser_site):
            reasons.append(ComplianceReason(
                rule_code="ADVERTISER_MISSING", severity="block",
                message=("В профиле бренда не заполнены реквизиты рекламодателя: "
                         "маркировка по ст. 18.1 невозможна. Заполните название и сайт юрлица."),
            ))
        if not ctx.erid:
            reasons.append(ComplianceReason(
                rule_code="AD_MARKING_REQUIRED", severity="block",
                message="Обнаружены признаки рекламы (цена/призыв). Требуется пометка «Реклама», рекламодатель и erid.",
            ))
        else:
            reasons.append(ComplianceReason(
                rule_code="AD_MARKING_REQUIRED", severity="info",
                message="Маркировка заполнена корректно.",
            ))
        # 4. Реестр блогеров: подтверждение, отказ и «неизвестно» — три разных вердикта
        if ctx.audience_size >= 10_000:
            if ctx.channel_in_blogger_registry is False:
                reasons.append(ComplianceReason(
                    rule_code="BLOGGER_REGISTRY_10K", severity="block",
                    message="Канал 10 000+ подписчиков отсутствует в реестре РКН: размещать рекламу запрещено.",
                ))
            elif ctx.channel_in_blogger_registry is None:
                reasons.append(ComplianceReason(
                    rule_code="BLOGGER_REGISTRY_UNKNOWN", severity="warn",
                    message="Реестр РКН не проверен: подтвердите регистрацию канала 10 000+ до размещения.",
                ))
    else:
        reasons.append(ComplianceReason(
            rule_code="AD_MARKING_REQUIRED", severity="info",
            message="Признаков рекламы не найдено: маркировка не требуется (информирование).",
        ))

    # 5. Стоп-лист обещаний
    if PROMISE.search(ctx.body):
        reasons.append(ComplianceReason(
            rule_code="CLAIM_STOPLIST", severity="warn",
            message="Формулировка из стоп-листа: обещание гарантированного результата.",
        ))

    blocked = any(r.severity == "block" for r in reasons)
    verdict = "ad_requires_marking" if is_ad else "informational"
    if blocked and is_ad:
        verdict = "ad_requires_marking"
    if blocked and not is_ad:
        verdict = "blocked"            # стоп-лист сработал, но это не реклама
    return ComplianceReport(
        verdict=verdict, reasons=reasons,
        marking_block=marking_block(ctx) if is_ad else None,
        rules_version=RULES_VERSION,
    )
