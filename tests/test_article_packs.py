"""Пакеты правил и шаблоны публикации: ниша — данные, а не ветки кода (этап I2).

Что здесь проверяется — и почему именно это:

1. **Пак расширяет движок, не меняя его.** «Темы с высокой ценой ошибки» добавляют оговорку,
   требуют источники у сравнений и запрещают обещания результата — это записи в реестре, а не
   `if topic == ...`. Новый рынок подключается файлом `ARTICLE_RULE_PACKS`.
2. **Политика проверяется, а не декларируется.** `min_words`, `required_blocks`,
   `banned_phrases`, `require_sources`, `disclaimer` попадают в спеку и в `check()`; порог
   уникальности берётся из пака, а не из константы.
3. **Шаблон — токены вёрстки.** Цвета, гарнитура, ширина и радиусы приходят из данных: второй
   рендерер не нужен, а шаблоны не расходятся по вёрстке.
4. **Неизвестный код отвергается.** Ни пак, ни шаблон не подменяются «значением по умолчанию»:
   молча другая политика публикации опаснее, чем 422 с перечнем реестра.
5. **Правило, которое нельзя применить, не применяется.** Правило пака с блоком не из реестра
   отбрасывается и видно в витрине (`invalid_rules`), а не падает в рантайме.
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from orbita import articles
from orbita.main import app

client = TestClient(app)
H = {"X-Actor-Role": "owner"}

#: длинный текст раздела: строгая политика пака по объёму не рубит нормальную статью
LONG = ("Разбираем по существу, сколько стоит проверка и как считать результат, чтобы не "
        "платить за пустой объём и видеть отдачу в цифрах, а не в ощущениях. ") * 10
#: 130 слов в разделе: проходит базовый минимум (350), но не строгий (500)
MEDIUM = "Проверка договора занимает два дня и заканчивается протоколом с рисками. " * 12

PLAN_FILE = {
    "kz-edu": {
        "title": "Образовательные темы",
        "description": "Курсы и обучение: программа, стоимость, формат",
        "rules": [{"code": "program", "block": "diagram", "data": ["steps"],
                   "signals": ("программ", "модул"),
                   "reason": "программа курса — это последовательность модулей, схема читается сразу"}],
        "policy": {"min_words": 400, "required_blocks": ["diagram"]},
    },
    "broken": {
        "title": "Пакет с неприменимым правилом",
        "rules": [{"code": "carousel", "block": "carousel", "reason": "нет такого блока"}],
        "policy": {},
    },
}


def _brief(**overrides) -> dict:
    brief = {
        "topic": "Проверка договора перед подписанием",
        "keywords": ["проверка договора"],
        "sections": 4,
        "lead": ("Проверка договора: где риски, сколько стоит и что смотреть в первую очередь."),
        "outline": [
            {"heading": "Где риски в договоре", "text": LONG,
             "callout": "Риск: спорные условия действуют, пока их не оспорили."},
            {"heading": "Сколько стоит проверка", "text": LONG,
             "columns": ["Пакет", "Цена"],
             "rows": [["Базовый", "9 000 ₽"], ["Полный", "24 000 ₽"]],
             "source": "прайс клиента, ноябрь 2026"},
            {"heading": "Как проходит проверка", "text": LONG,
             "steps": ["Читаем договор", "Отмечаем риски", "Пишем протокол"]},
            {"heading": "Что остаётся у клиента", "text": LONG},
        ],
        "summary": ["Риски важнее формулировок", "Проверка занимает два дня"],
        "faq": [{"q": "Нужен ли оригинал?", "a": "Достаточно скана всех страниц."}],
    }
    brief.update(overrides)
    return brief


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for name in ("ARTICLE_RULE_PACKS", "ARTICLE_TEMPLATES"):
        monkeypatch.delenv(name, raising=False)
    articles.reload_packs()
    articles.reset()
    yield
    articles.reload_packs()
    articles.reset()


# ---------------------------------------------------------------- реестр паков
def test_builtin_packs_and_templates_are_valid():
    packs = {row["code"]: row for row in articles.rule_packs()}
    assert {"default", "high-stakes"} <= set(packs)
    assert all(not row["invalid_rules"] for row in packs.values())
    strict = packs["high-stakes"]["policy"]
    assert strict["min_words"] > articles.MIN_WORDS
    assert strict["max_duplicate_ratio"] < articles.MAX_DUPLICATE_RATIO
    assert strict["required_blocks"] and strict["disclaimer"]
    templates = {row["code"]: row for row in articles.templates()}
    assert {"editorial", "longread"} <= set(templates)
    assert templates["longread"]["tokens"]["font"].startswith("Georgia")


def test_unknown_pack_and_template_are_not_silently_replaced():
    with pytest.raises(KeyError):
        articles.rule_pack("no-such-pack")
    with pytest.raises(KeyError):
        articles.template("no-such-template")
    assert articles.plan({"topic": "Тест", "rule_pack": "no-such-pack"})["code"] == "pack_unknown"
    assert articles.plan({"topic": "Тест", "template": "no-such"})["code"] == "template_unknown"
    with pytest.raises(KeyError):
        articles.template_tokens("no-such")   # токены — только через известный код


def test_pack_rules_extend_visual_rules_and_can_be_disabled():
    assert "risk" in [rule["code"] for rule in articles.rules_for("high-stakes")]
    assert "risk" not in [rule["code"] for rule in articles.rules_for("default")]
    disabled = {"default": {"title": "Без цифр", "rules": [], "policy": {},
                            "disable": ["proof", "prices"]}}
    import os
    import tempfile

    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                     encoding="utf-8") as handle:
        json.dump(disabled, handle, ensure_ascii=False)
        path = handle.name
    try:
        os.environ["ARTICLE_RULE_PACKS"] = path
        articles.reload_packs()
        codes = [rule["code"] for rule in articles.rules_for("default")]
        assert "proof" not in codes and "prices" not in codes
        assert "show" in codes
    finally:
        os.environ.pop("ARTICLE_RULE_PACKS", None)
        articles.reload_packs()


def test_unusable_pack_rule_is_reported_not_crashed(monkeypatch, tmp_path):
    path = tmp_path / "packs.json"
    path.write_text(json.dumps(PLAN_FILE, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("ARTICLE_RULE_PACKS", str(path))
    articles.reload_packs()
    packs = {row["code"]: row for row in articles.rule_packs()}
    assert packs["broken"]["invalid_rules"] == ["carousel"]
    assert "carousel" not in [rule["code"] for rule in articles.rules_for("broken")]
    assert packs["kz-edu"]["invalid_rules"] == []
    assert "program" in [rule["code"] for rule in articles.rules_for("kz-edu")]


# ---------------------------------------------------------------- политика
def test_pack_policy_lands_in_spec_and_checks():
    result = articles.compose(_brief(rule_pack="high-stakes", template="longread"))
    spec = result["spec"]
    assert spec["rule_pack"] == "high-stakes" and spec["template"] == "longread"
    assert spec["policy"]["min_words"] == 500
    assert result["checks"]["ok"], result["checks"]["issues"]
    # оговорка и врезка раздела — разные блоки
    callouts = [block for block in spec["blocks"] if block["type"] == "callout"]
    assert len(callouts) == 2
    assert any("не заменяет консультацию" in block["props"]["text"] for block in callouts)


def test_policy_min_words_overrides_default_threshold():
    short = _brief(sections=3, outline=[
        {"heading": "Где риски", "text": MEDIUM},
        {"heading": "Сколько стоит", "text": MEDIUM, "columns": ["Пакет", "Цена"],
         "rows": [["Базовый", "9 000 ₽"]], "source": "прайс клиента"},
        {"heading": "Что дальше", "text": MEDIUM},
    ])
    default = articles.compose(short)
    strict = articles.compose({**short, "rule_pack": "high-stakes"})
    assert default["checks"]["ok"]
    codes = {issue["code"] for issue in strict["checks"]["issues"]}
    assert "too_few_words" in codes
    message = next(issue["message"] for issue in strict["checks"]["issues"]
                   if issue["code"] == "too_few_words")
    assert "500" in message


def test_policy_bans_promises_and_requires_sources():
    brief = _brief(rule_pack="high-stakes")
    brief["outline"][2]["text"] = LONG + " Гарантируем результат в суде."
    # сравнение без источника: у таблицы источник проверяется и без политики, у сравнения — ею
    brief["outline"][3] = {"heading": "Что сравнить", "text": LONG,
                           "columns": [{"title": "Пакет", "items": ["два дня"]},
                                       {"title": "Своими силами", "items": ["дольше"]}]}
    result = articles.compose(brief)
    codes = {issue["code"] for issue in result["checks"]["issues"]}
    assert "banned_phrase" in codes
    assert "source_missing" in codes


def test_policy_requires_block_only_when_missing():
    spec = articles.compose(_brief(rule_pack="high-stakes"))["spec"]
    assert "block_required" not in {issue["code"] for issue in articles.check(spec)["issues"]}
    without = {**spec, "blocks": [block for block in spec["blocks"]
                                  if block["type"] != "callout"]}
    assert "block_required" in {issue["code"] for issue in articles.check(without)["issues"]}


def test_policy_changes_uniqueness_limit():
    spec = articles.compose(_brief(rule_pack="high-stakes"))["spec"]
    loose = articles.compose(_brief())["spec"]
    assert articles.uniqueness(spec, [])["limit"] == 0.2
    assert articles.uniqueness(loose, [])["limit"] == articles.MAX_DUPLICATE_RATIO


def test_pack_rule_points_to_callout_only_with_section_data():
    brief = _brief(rule_pack="high-stakes")
    brief["outline"][3] = {"heading": "Что остаётся клиента", "text": LONG,
                           "callout": "Срок хранения протокола — три года."}
    spec = articles.compose(brief)["spec"]
    section_four = [block for block in spec["blocks"]
                    if block["type"] == "callout" and block.get("section") == 4]
    assert section_four and section_four[0]["props"]["tone"] == "warning"
    assert section_four[0]["reason"]


# ---------------------------------------------------------------- шаблоны
def test_template_changes_css_tokens_and_keeps_markup():
    editorial = articles.render_html(articles.compose(_brief())["spec"])
    longread = articles.render_html(articles.compose(_brief(template="longread"))["spec"])
    assert "#7c5cff" in editorial and "#0f766e" in longread
    assert "Georgia" in longread and 'max-width:720px' in longread
    assert editorial.count("<article>") == longread.count("<article>") == 1
    assert articles.style_for("editorial") != articles.style_for("longread")


def test_template_partial_tokens_fall_back_to_editorial(monkeypatch, tmp_path):
    path = tmp_path / "templates.json"
    path.write_text(json.dumps({"mint": {"title": "Мята", "tokens": {"accent": "#0e9f6e"}}},
                               ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("ARTICLE_TEMPLATES", str(path))
    articles.reload_packs()
    tokens = articles.template_tokens("mint")
    assert tokens["accent"] == "#0e9f6e"
    assert tokens["font"] == articles.TEMPLATES["editorial"]["tokens"]["font"]
    assert "#0e9f6e" in articles.style_for("mint")


# ---------------------------------------------------------------- API
def test_api_packs_catalog():
    body = client.get("/api/v1/articles/packs", headers=H).json()
    assert {row["code"] for row in body["rule_packs"]} >= {"default", "high-stakes"}
    assert {row["code"] for row in body["templates"]} >= {"editorial", "longread"}
    assert body["default_pack"] == articles.DEFAULT_PACK
    assert body["env"]["rule_packs"] == "ARTICLE_RULE_PACKS"
    assert "disclaimer" in body["policy_keys"]


def test_api_rejects_unknown_pack_and_serves_strict_article():
    rejected = client.post("/api/v1/articles", headers=H,
                           json={"topic": "Тема с чужим пакетом", "sections": 3,
                                 "rule_pack": "no-such-pack"})
    assert rejected.status_code == 422 and rejected.json()["detail"]["kind"] == "pack_unknown"
    created = client.post("/api/v1/articles", headers=H,
                          json=_brief(rule_pack="high-stakes", template="longread",
                                      materials=[]))
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["spec"]["rule_pack"] == "high-stakes"
    page = client.get(body["urls"]["preview"], headers=H).text
    assert "#0f766e" in page and "не заменяет консультацию" in page


def test_api_summary_lists_packs_templates_and_limits():
    body = client.get("/api/v1/articles/summary", headers=H).json()
    assert body["default_pack"] == "default" and body["default_template"] == "editorial"
    assert {row["code"] for row in body["rule_packs"]} >= {"default", "high-stakes"}
    assert {row["code"] for row in body["templates"]} >= {"editorial", "longread"}
