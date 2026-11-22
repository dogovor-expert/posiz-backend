"""Редакционные статьи: структура, визуалы по смыслу, уникальность, публикация (этап I).

Что здесь проверяется — и почему именно это:

1. **Структуру решает система, а не шаблон с пустыми местами.** `plan()` отдаёт заголовки,
   интент раздела и — отдельно — *причину*, по которой в разделе нужен визуал. План можно
   показать человеку до генерации текста: правка плана дешевле правки статьи.
2. **Данные раздела сильнее слов в тексте.** Раздел со строками таблицы получает таблицу,
   даже если в тексте мелькнуло слово «отчёт» (оно тянет правило `proof` → цифру). Иначе
   «пример отчёта» превращался бы в плашку с числом, которого никто не просил.
3. **Визуал без материала не рисуется.** Обещали график, а ряда нет — раздел остаётся
   текстом, а не графиком из воздуха: цифра без источника в статье — это выдумка.
4. **HTML собирает код.** Вёрстка, schema.org и подписи с источниками появляются из спеки,
   поэтому правка дизайна не требует перегенерации статей, а разметка одинакова у всех.
5. **Публикация без ручной работы.** Одинаковая спека не создаёт второй черновик, статья с
   ошибками проверок сохраняется только явным `strict=false` — и тогда помечается `draft`.
"""
from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from orbita import articles
from orbita.main import app

client = TestClient(app)
H = {"X-Actor-Role": "owner"}

TOPIC = "Продвижение в мессенджерах: цены и каналы 2026"
WORDS = (
    "Разбираем по существу: сколько стоит размещение, от чего зависит цена и как считать "
    "результат, чтобы не платить за пустой охват. Данные берём из материалов клиента, "
    "выводы делаем из данных, а не из общих слов про важность присутствия в каналах. "
    "Читателю нужен ответ на конкретный вопрос: сколько это стоит и что он получит за эти "
    "деньги, поэтому каждый раздел заканчивается выводом, который можно применить сегодня. "
)
MATERIALS = [
    {"kind": "numbers", "code": "reach", "title": "Охваты по месяцам", "unit": " тыс.",
     "source": "аналитика клиента, ноябрь 2026",
     "values": [{"label": "июл", "value": 8.2}, {"label": "авг", "value": 9.4},
                {"label": "сен", "value": 11.1}, {"label": "окт", "value": 12.4}]},
]


def _brief(**overrides) -> dict:
    """Бриф на 400+ слов: пять разделов, у каждого — свой визуал из данных."""
    brief = {
        "topic": TOPIC,
        "keywords": ["продвижение в мессенджерах", "стоимость рекламы"],
        "sections": 5,
        "lead": ("Продвижение в мессенджерах: считаем стоимость по каналам, показываем "
                 "динамику охватов и объясняем, как проверить результат."),
        "outline": [
            {"heading": "Сколько стоит размещение", "text": WORDS,
             "columns": ["Канал", "Цена за пост", "Охват"],
             "rows": [["Telegram", "12 000 ₽", "18 000"], ["MAX", "9 000 ₽", "12 400"]],
             "source": "прайс клиента, ноябрь 2026"},
            {"heading": "Динамика охватов за год", "text": WORDS, "material": "reach"},
            {"heading": "Как мы считаем результат", "text": WORDS,
             "steps": ["Собираем охваты", "Считаем вовлечённость", "Проверяем отчёт"]},
            {"heading": "Как выглядит отчёт", "text": WORDS,
             "image_prompt": "Отчёт по охватам в кабинете"},
            {"heading": "Что сравнить перед стартом", "text": WORDS,
             "columns": [{"title": "Каналы", "items": ["дешевле вход", "быстрее охват"]},
                         {"title": "Блог", "items": ["долгий эффект", "нужен контент"]}]},
        ],
        "summary": ["Стоимость зависит от канала и формата", "Охваты растут вдвое за полгода",
                    "Считайте вовлечённость, а не цену за пост"],
        "faq": [{"q": "Что входит в цену?", "a": "Пост, ссылка и отчёт по охватам."}],
    }
    brief.update(overrides)
    return brief


def _compose(**overrides) -> dict:
    result = articles.compose(_brief(**overrides), materials=MATERIALS)
    assert result.get("ok"), result
    return result


def _types(spec: dict) -> list[str]:
    return [block["type"] for block in spec["blocks"]]


@pytest.fixture(autouse=True)
def _clean_store():
    articles.reset()
    yield
    articles.reset()


# ---------------------------------------------------------------- реестр блоков
def test_registry_is_closed_and_visuals_carry_contract():
    assert len(articles.BLOCKS) == 17
    assert set(articles.INLINE_VISUALS) <= set(articles.BLOCKS)
    for code, meta in articles.BLOCKS.items():
        assert meta.get("needs") is not None, code
        assert isinstance(meta.get("visual"), bool), code
    assert articles.render_html({"seo": {}, "blocks": []}).count("<article>") == 1


def test_visual_rules_are_data_and_every_rule_explains_itself():
    assert len(articles.VISUAL_RULES) >= 6
    for rule in articles.VISUAL_RULES:
        assert rule["block"] in articles.INLINE_VISUALS, rule["code"]
        assert rule["reason"] and rule["signals"], rule["code"]


# ---------------------------------------------------------------- план
def test_plan_builds_structure_with_reasons():
    planned = articles.plan(_brief())
    assert planned["ok"] and len(planned["sections"]) == 5
    for section in planned["sections"]:
        assert section["heading"] and section["intent"]
        if section["visual"]:
            assert section["visual"]["reason"], section["heading"]
    assert planned["stats"]["visuals"] == 5


def test_plan_clamps_section_count_and_requires_topic():
    assert len(articles.plan(_brief(), sections=2)["sections"]) == 3
    assert len(articles.plan(_brief(), sections=50)["sections"]) == 9
    assert articles.plan({"topic": ""})["code"] == "topic_required"


def test_plan_without_outline_invents_headings_not_visuals():
    planned = articles.plan({"topic": "Как выбрать тариф", "sections": 4})
    headings = [section["heading"] for section in planned["sections"]]
    assert len(set(headings)) == 4
    assert "визуал" not in planned["sections"][0] or planned["sections"][0]["visual"] is None


# ---------------------------------------------------------------- выбор визуала
def test_section_data_outranks_keywords_in_text():
    """Слова «пример/отчёт» в тексте не должны перебивать явный image_prompt."""
    section = {"heading": "Пример отчёта", "text": "Данные и отчёт по охватам.",
               "data": {"image_prompt": "Отчёт в кабинете"}}
    ranked = articles._rank_rules(section)
    assert ranked[0]["block"] == "image"


def test_each_kind_of_data_gets_its_own_visual():
    spec = _compose()["spec"]
    types = _types(spec)
    assert types.count("table") == 1
    assert types.count("chart") == 1
    assert types.count("diagram") == 1
    assert types.count("image") == 1
    assert types.count("comparison") == 1


def test_visuals_have_alt_reason_and_source():
    for block in _compose()["spec"]["blocks"]:
        meta = articles.BLOCKS[block["type"]]
        if not meta["visual"]:
            continue
        assert block["alt"], block["type"]
        assert block["reason"], block["type"]
        if block["type"] in {"chart", "stat", "table"}:
            assert block["source"], block["type"]


def test_chart_without_material_is_not_drawn():
    """Обещали график, ряда нет — блока нет: цифры не придумываем."""
    result = articles.compose({"topic": "Рост охватов за год", "sections": 3,
                               "outline": [{"heading": "Динамика за год",
                                            "text": "Покажем рост охватов в процентах." * 6}]})
    assert "chart" not in _types(result["spec"])


def test_fallback_to_next_candidate_when_data_missing():
    """`material` указан, но материала нет — берём следующий кандидат, а не пустоту."""
    result = articles.compose(
        {"topic": "Динамика и шаги", "sections": 3,
         "outline": [{"heading": "Рост за год и процесс", "text": "Динамика по шагам." * 8,
                      "material": "missing", "steps": ["Сбор", "Расчёт", "Отчёт"]}]})
    types = _types(result["spec"])
    assert "diagram" in types and "chart" not in types


# ---------------------------------------------------------------- проверки
def test_good_article_passes_checks():
    checks = _compose()["checks"]
    assert checks["ok"], checks["issues"]
    assert checks["stats"]["words"] >= articles.MIN_WORDS
    assert checks["stats"]["visuals"] == 5


def test_checks_catch_visuals_without_alt_reason_source():
    spec = _compose()["spec"]
    for block in spec["blocks"]:
        if block["type"] == "chart":
            block["alt"] = ""
            block["reason"] = ""
            block["source"] = ""
    codes = {issue["code"] for issue in articles.check(spec)["issues"]}
    assert {"alt_missing", "reason_missing", "source_missing"} <= codes


def test_checks_catch_structure_problems():
    spec = _compose()["spec"]
    spec["blocks"].append(articles._block("heading", {"text": "Слишком глубоко", "level": 4}))
    spec["blocks"].append(articles._block("paragraph", {"text": "TODO дописать"}))
    codes = {issue["code"] for issue in articles.check(spec)["issues"]}
    assert "heading_jump" in codes
    assert "placeholder" in codes


def test_checks_catch_short_note_and_missing_visuals():
    spec = {
        "spec_version": articles.SPEC_VERSION,
        "seo": {"title": "Заметка", "description": "Короткая заметка",
                "keywords": ["заметка"]},
        "blocks": [articles._block("heading", {"text": "Раздел", "level": 2}),
                   articles._block("paragraph", {"text": "Совсем короткий абзац."}),
                   articles._block("paragraph", {"text": "Ещё один абзац про то же самое."}),
                   articles._block("paragraph", {"text": "И третий абзац ни о чём."}),
                   articles._block("paragraph", {"text": "Четвёртый, для объёма."}),
                   articles._block("paragraph", {"text": "Пятый, чтобы блоков стало много."}),
                   articles._block("paragraph", {"text": "Шестой, всё ещё без визуала."}),
                   articles._block("paragraph", {"text": "Седьмой абзац подряд."}),
                   articles._block("paragraph", {"text": "Восьмой, без визуала и с водой."})],
    }
    codes = {issue["code"] for issue in articles.check(spec)["issues"]}
    assert {"too_few_words", "no_visuals", "too_short"} <= codes


def test_checks_reject_unknown_block_and_wrong_version():
    spec = _compose()["spec"]
    spec["blocks"].append({"id": "x", "type": "carousel", "props": {"items": ["a"]}})
    assert "block_unknown" in {issue["code"] for issue in articles.check(spec)["issues"]}
    assert articles.check({"spec_version": "article.spec.v0"})["issues"][0]["code"] == (
        "spec_version")


def test_keyword_coverage_counts_title_first_paragraph_and_headings():
    spec = _compose()["spec"]
    coverage = articles.keyword_coverage(spec)
    assert coverage["keywords"] == 2
    assert coverage["in_title"] == 1
    assert coverage["in_first_paragraph"] == 1
    assert coverage["title"] in TOPIC.lower()


# ---------------------------------------------------------------- уникальность
def test_uniqueness_flags_copy_of_corpus():
    spec = _compose()["spec"]
    own = articles._plain_text(spec)
    fresh = articles.uniqueness(spec, ["Совершенно другая тема про логистику и склады."])
    copy = articles.uniqueness(spec, [own])
    assert fresh["ok"] and fresh["duplicate_ratio"] < 0.05
    assert not copy["ok"] and copy["duplicate_ratio"] > articles.MAX_DUPLICATE_RATIO
    assert copy["examples"] and copy["limit"] == articles.MAX_DUPLICATE_RATIO


def test_uniqueness_uses_five_word_shingles():
    text = "один два три четыре пять шесть семь восемь девять десять"
    assert len(articles.shuffles(text)) == 6
    assert articles.SHINGLE == 5


# ---------------------------------------------------------------- HTML
def test_html_is_a_publishable_page():
    spec = _compose()["spec"]
    html = articles.render_html(spec)
    assert html.startswith("<!doctype html>")
    assert f"<title>{spec['seo']['title']}</title>" in html
    assert 'name="description"' in html
    assert html.count("<svg") == 3          # график, схема и иллюстрация — без внешних файлов
    assert "<img" not in html               # ни одного внешнего изображения
    assert "· источник: аналитика клиента, ноябрь 2026" in html
    assert '"@context": "https://schema.org"' in html
    assert '"@type": "FAQPage"' in html


def test_html_escapes_text_and_skips_canonical_by_default():
    spec = _compose()["spec"]
    spec["blocks"].append(articles._block("paragraph", {
        "text": "Осторожно: <script>alert(1)</script> в тексте не должен стать тегом."}))
    html = articles.render_html(spec)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html
    assert 'rel="canonical"' not in html
    spec["seo"]["canonical"] = "https://example.com/blog/post/"
    assert 'rel="canonical"' in articles.render_html(spec)


def test_html_fragment_mode_and_chart_kinds():
    spec = _compose()["spec"]
    fragment = articles.render_html(spec, standalone=False)
    assert fragment.startswith("<article>") and "<style>" not in fragment
    for block in spec["blocks"]:
        if block["type"] == "chart":
            block["props"]["kind"] = "bar"
    html = articles.render_html(spec)
    assert "12 тыс." in html or "12" in html
    diagram = next(block for block in spec["blocks"] if block["type"] == "diagram")
    assert "Шаг 1" in articles.render_html(spec) and diagram["props"]["steps"]


def test_html_lists_internal_links_only_when_given():
    result = articles.compose(_brief(), materials=MATERIALS, links=["prodvizhenie-v-kanalah"])
    html = articles.render_html(result["spec"])
    assert "Читать дальше" in html
    assert "/blog/prodvizhenie-v-kanalah/" in html


# ---------------------------------------------------------------- хранилище
def test_publish_is_idempotent_and_marks_status():
    spec = _compose()["spec"]
    first = articles.publish(spec)
    second = articles.publish(spec)
    assert first["ok"] and not first["reused"]
    assert second["reused"] and second["article"]["id"] == first["article"]["id"]
    assert first["article"]["status"] == "ready"
    assert articles.article(first["article"]["id"])["spec_hash"] == articles.spec_hash(spec)
    assert [row["id"] for row in articles.articles()] == [first["article"]["id"]]


def test_publish_marks_failed_checks_as_draft():
    spec = _compose()["spec"]
    spec["seo"]["keywords"] = []
    spec["checks"] = articles.check(spec)
    doc = articles.publish(spec)["article"]
    assert doc["status"] == "draft" and doc["issues"]


def test_summary_lists_limits_rules_and_image_providers():
    data = articles.summary()
    assert data["limits"]["min_words"] == articles.MIN_WORDS
    assert set(data["visual_rules"]) == {rule["code"] for rule in articles.VISUAL_RULES}
    codes = {row["code"] for row in data["images"]}
    assert codes == set(articles.IMAGES)
    assert next(row for row in data["images"] if row["code"] == "brand-card")["available"] is True


# ---------------------------------------------------------------- API
def test_api_summary_and_quick_plan():
    summary = client.get("/api/v1/articles/summary", headers=H)
    assert summary.status_code == 200
    assert summary.json()["spec_version"] == articles.SPEC_VERSION
    plan = client.get("/api/v1/articles/plan", headers=H,
                      params={"topic": TOPIC, "sections": 4})
    assert plan.status_code == 200 and len(plan.json()["sections"]) == 4
    bad = client.get("/api/v1/articles/plan", headers=H, params={"topic": "ok"})
    assert bad.status_code == 422


def test_api_full_plan_reports_material_usage():
    payload = _brief(materials=MATERIALS + [
        {"kind": "numbers", "code": "spare", "title": "Лишний ряд", "values": [1, 2]}])
    response = client.post("/api/v1/articles/plan", headers=H, json=payload)
    assert response.status_code == 200
    body = response.json()
    assert body["materials"] == ["reach", "spare"]
    assert body["unused_materials"] == ["spare"]


def test_api_creates_ready_article_and_serves_html():
    payload = _brief(materials=MATERIALS)
    created = client.post("/api/v1/articles", headers=H, json=payload)
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["status"] == "ready" and body["spec"]["checks"]["ok"]
    article_id = body["article_id"]
    page = client.get(f"/api/v1/articles/{article_id}.html", headers=H)
    assert page.status_code == 200 and page.headers["content-type"].startswith("text/html")
    assert "<article>" in page.text
    preview = client.get(f"/api/v1/articles/{article_id}/preview", headers=H)
    assert preview.status_code == 200 and "<style>" in preview.text
    single = client.get(f"/api/v1/articles/{article_id}", headers=H).json()
    assert single["article"]["id"] == article_id and single["html_bytes"] > 3000
    listing = client.get("/api/v1/articles", headers=H).json()
    assert listing["items"][0]["id"] == article_id
    assert listing["summary"]["articles"] == 1


def test_api_rejects_article_with_issues_unless_draft_allowed():
    payload = {"topic": "Слишком короткая тема статьи", "sections": 3, "keywords": ["тема"]}
    rejected = client.post("/api/v1/articles", headers=H, json=payload)
    assert rejected.status_code == 422
    detail = rejected.json()["detail"]
    assert detail["kind"] == "article_rejected" and detail["issues"]
    assert detail["stats"]["words"] < articles.MIN_WORDS
    draft = client.post("/api/v1/articles", headers=H, json={**payload, "strict": False})
    assert draft.status_code == 201 and draft.json()["status"] == "draft"


def test_api_returns_404_and_reuses_spec():
    assert client.get("/api/v1/articles/art_missing", headers=H).status_code == 404
    payload = _brief(materials=MATERIALS)
    first = client.post("/api/v1/articles", headers=H, json=payload).json()
    second = client.post("/api/v1/articles", headers=H, json=payload).json()
    assert second["reused"] and second["article_id"] == first["article_id"]


def test_api_accepts_edited_plan():
    payload = _brief(materials=MATERIALS)
    planned = articles.plan(payload)
    planned["sections"][1]["heading"] = "Правка плана человеком"
    body = client.post("/api/v1/articles", headers=H,
                       json={**payload, "plan": planned}).json()
    assert "Правка плана человеком" in [
        block["props"].get("text") for block in body["spec"]["blocks"]]
    assert len(re.findall(r"<h2>", articles.render_html(body["spec"]))) >= 5
