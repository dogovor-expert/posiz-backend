"""Комплаенс: маркировка попадает в текст, блокировки — только про рекламу.

Аудит (5): «вердикт block срабатывает до проверки ``is_ad``, а
``channel_in_blogger_registry`` по умолчанию ``True`` — то есть правило «канал не в
реестре» не может сработать». Здесь проверяются оба исправления и то, что маркировка
реально доезжает до канала.
"""
from __future__ import annotations

from orbita import compliance


def _ctx(**kwargs):
    """Контекст варианта; ``is_ad`` передаётся в ``check`` отдельно (флаг из БД)."""
    base = dict(channel="vk", body="Пост про договор аренды и залог", erid=None,
                audience_size=1200)
    base.update(kwargs)
    return compliance.VariantContext(**base)


def test_organic_post_is_not_blocked_on_forbidden_resource():
    """Информирование в канале с ограниченным доступом — не размещение рекламы."""
    report = compliance.check(_ctx(channel="instagram"), is_ad=False)
    assert report.verdict != "forbidden_resource"
    assert any(r.rule_code == "FORBIDDEN_RESOURCE_INFO" for r in report.reasons)


def test_ad_on_forbidden_resource_is_blocked():
    report = compliance.check(_ctx(channel="instagram"), is_ad=True)
    assert report.verdict == "forbidden_resource"


def test_registry_unknown_is_warning_not_confirmation():
    report = compliance.check(_ctx(audience_size=50_000, channel_in_blogger_registry=None),
                              is_ad=True)
    codes = {r.rule_code for r in report.reasons}
    assert "BLOGGER_REGISTRY_UNKNOWN" in codes
    assert "BLOGGER_REGISTRY_10K" not in codes


def test_registry_known_failure_blocks_ad():
    report = compliance.check(_ctx(audience_size=50_000, channel_in_blogger_registry=False),
                              is_ad=True)
    assert report.verdict == "ad_requires_marking"
    assert any(r.rule_code == "BLOGGER_REGISTRY_10K" for r in report.reasons)


def test_registry_confirmed_channel_passes():
    report = compliance.check(_ctx(audience_size=50_000, channel_in_blogger_registry=True),
                              is_ad=False)
    assert not any(r.rule_code.startswith("BLOGGER_REGISTRY") for r in report.reasons)


def test_marking_applied_to_text_once():
    ctx = _ctx(erid="erid: 2Vfnxw8abcde")
    text = compliance.apply_marking("Текст рекламного поста.", ctx)
    assert "Реклама" in text and "erid: 2Vfnxw8abcde" in text
    assert compliance.apply_marking(text, ctx) == text      # второй раз не дублируем


def test_marking_block_is_the_same_as_in_report():
    """Текст, ушедший в канал, содержит тот же блок, что показан в отчёте проверки."""
    ctx = _ctx(erid="erid: X1")
    report = compliance.check(ctx, is_ad=True)
    text = compliance.apply_marking("Пост.", ctx)
    assert report.marking_block.strip() in text


def test_empty_body_gets_only_marking():
    text = compliance.apply_marking("", _ctx(erid="erid: X1"))
    assert text.strip().startswith("Реклама")


def test_rules_have_sources():
    """Каждое правило ссылается на норму и дату — иначе это «мнение», а не комплаенс."""
    for rule in compliance.RULES:
        assert rule["source"] and rule["effective"]
        assert rule["severity"] in {"block", "warn"}
