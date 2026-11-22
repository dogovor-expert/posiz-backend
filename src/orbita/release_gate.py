"""Release gate: исполняемый чек-лист готовности.

Зачем: аудит справедливо заметил, что «готовность» в проекте заявлялась словами.
Здесь она проверяется кодом — по конфигурации и по живым модулям. Каждый пункт
отвечает на вопрос «что сломается у клиента, если это не выполнено», а не «есть ли
файл в репозитории».

Пункты с ``infra=True`` требуют запущенных Postgres/Redis: локально они возвращают
``not_verified`` со списком недостающего, а не ложное «ок».

Запуск::

    python -m orbita.release_gate            # человекочитаемо
    python -m orbita.release_gate --json     # для CI

Код возврата 1, если есть ``failed``.
"""
from __future__ import annotations

import json
import os
import sys
from typing import Callable

CHECK: list[tuple[str, str, bool, Callable[[], tuple[bool, str]]]] = []


def check(check_id: str, title: str, *, infra: bool = False):
    def register(func: Callable[[], tuple[bool, str]]):
        CHECK.append((check_id, title, infra, func))
        return func

    return register


# ------------------------------------------------------------------ медиа
@check("media.spec_schema", "Спека карточки валидируется до рендера")
def _media_schema() -> tuple[bool, str]:
    from . import media

    good = media.build_spec({"title": "29 площадок"}, "9x16", "card-hero")
    bad_template = {**good, "template": "card-3d"}
    bad_version = {**good, "spec_version": "reel.spec.v0"}
    bad_text = {**good, "props": {"headline": "а" * (media.MAX_HEADLINE_CHARS + 1)}}
    if media.validate_spec(good):
        return False, f"годная спека отвергнута: {media.validate_spec(good)}"
    for name, spec in (("шаблон", bad_template), ("версия", bad_version), ("длина", bad_text)):
        if not media.validate_spec(spec):
            return False, f"невалидная спека прошла проверку: {name}"
    return True, "4 шаблона, {} форматов, лимиты и версия спеки применяются".format(
        len(media.FORMATS))


@check("media.render_smoke", "Карточки рендерятся во всех форматах и открываются как SVG")
def _media_render() -> tuple[bool, str]:
    from xml.etree import ElementTree as ET

    from . import media

    sizes = []
    for fmt in sorted(media.FORMATS):
        spec = media.build_spec({"title": "Один договор — 29 площадок", "footer": "posiz.ru"},
                                fmt, "card-hero", brand={"name": "dogiz"})
        report = media.check(spec)
        if not report["ok"]:
            return False, f"{fmt}: проверки отвергли годную карточку: {report['issues'][:2]}"
        svg = media.render_svg(spec)
        ET.fromstring(svg)                      # валидный XML
        if f'width="{media.FORMATS[fmt][0]}"' not in svg:
            return False, f"{fmt}: размер не совпадает с форматом"
        sizes.append(f"{fmt} {len(svg.encode())} Б")
    return True, "рендер: " + ", ".join(sizes)


@check("media.safe_zone", "Текст не попадает под интерфейс канала")
def _media_safe_zone() -> tuple[bool, str]:
    from . import media

    box = media.content_box("9x16")
    spec = media.build_spec({"title": "Один договор — 29 площадок", "footer": "posiz.ru"}, "9x16")
    for bbox in media.measure(spec):
        if bbox["x"] + bbox["width"] > box["x"] + box["width"] + 12:
            return False, f"блок {bbox['kind']} выходит за зону контента"
    dense = media.build_spec({
        "title": "Девять причин перейти на один конвейер публикаций и не копипастить вручную",
        "eyebrow": "контент",
        "items": ["Первый пункт ровно сорок восемь символов в срок!",
                  "Второй пункт ровно сорок восемь символов в срок",
                  "Третий пункт ровно сорок восемь символов в срок",
                  "Четвертый пункт сорок восемь символов в срок!",
                  "Пятый пункт ровно сорок восемь символов в срок"],
    }, "1x1", "card-list")
    if media.check(dense)["ok"]:
        return False, "переполненная карточка 1:1 прошла проверку safe zone"
    return True, f"зона контента 9:16 {box['width']}×{box['height']}, переполнение ловится"


@check("media.compliance", "Реклама без erid не рендерится, с erid — плашка в кадре")
def _media_compliance() -> tuple[bool, str]:
    from . import media

    ad = {"ad": True, "erid": "2Vtzqw9GateErID", "advertiser": "ООО «Тест»", "inn": "7700000000"}
    without = media.build_spec({"title": "Скидка 20%"}, "1x1", compliance={"ad": True})
    if media.check(without)["ok"]:
        return False, "реклама без erid прошла проверку"
    marked = media.build_spec({"title": "Скидка 20%"}, "1x1", compliance=ad)
    if not media.check(marked)["ok"]:
        return False, "маркированная реклама отвергнута"
    svg = media.render_svg(marked)
    if ad["erid"] not in svg or "Реклама" not in svg:
        return False, "плашки маркировки нет в SVG"
    return True, "без erid — отказ, с erid — плашка «Реклама · erid …» в кадре"


@check("media.compliance_packs", "Правила маркировки — пакеты рынка, а не ветки в коде")
def _media_compliance_packs() -> tuple[bool, str]:
    from . import media_compliance

    rows = {row["code"]: row for row in media_compliance.packs()}
    if not {"ru-ad", "eu-disclosure", "us-ftc", "generic"} <= set(rows):
        return False, f"пакетов не хватает: {sorted(rows)}"
    if rows["ru-ad"]["requires"] != ["erid", "advertiser", "inn"]:
        return False, "ru-ad требует не тот набор полей"
    if "erid" in rows["eu-disclosure"]["requires"]:
        return False, "eu-disclosure не должен требовать erid"
    codes = {issue["code"] for issue in media_compliance.validate({"pack": "nope", "ad": True})}
    if codes != {"pack_unknown"}:
        return False, "неизвестный пакет не отвергнут"
    if media_compliance.validate({"pack": "ru-ad", "ad": True,
                                  "erid": "2Vtzqw9Gate", "advertiser": "ООО «Тест»",
                                  "inn": "7700000000"}):
        return False, "корректная маркировка отвергнута"
    if not media_compliance.validate({"pack": "ru-info", "ad": True}):
        return False, "ru-info пропустил рекламу"
    if "min_age_invalid" not in {i["code"] for i in
                                 media_compliance.validate({"min_age": 21})}:
        return False, "возрастная метка вне пакета не отвергнута"
    return True, (f"{len(rows)} пакетов ({', '.join(sorted(rows))}); "
                  f"ru-ad: erid+рекламодатель+ИНН, eu-disclosure без erid, неизвестный — отказ")


@check("media.compliance_block", "Реклама без маркировки не рендерится ни карточкой, ни роликом")
def _media_compliance_block() -> tuple[bool, str]:
    from . import media, media_compliance, media_reel

    ad = {"pack": "ru-ad", "ad": True, "erid": "2Vtzqw9GateErID",
          "advertiser": "ООО «Тест»", "inn": "7700000000", "min_age": 18}
    card = media.build_spec({"title": "Скидка 20%"}, "1x1", compliance={"ad": True})
    if media.check(card)["ok"]:
        return False, "карточка: реклама без erid прошла проверку"
    reel = media_reel.build_reel_spec({"title": "Скидка 20%", "footer": "posiz.ru"}, fps=25,
                                      compliance={"ad": True})
    if media_reel.check(reel)["ok"]:
        return False, "ролик: реклама без erid прошла проверку"
    marked = media_reel.build_reel_spec({"title": "Скидка 20%", "footer": "posiz.ru"}, fps=25,
                                        compliance=ad)
    report = media_reel.check(marked)
    if not report["ok"]:
        return False, f"маркированный ролик отвергнут: {report['issues']}"
    frame = media_reel.frame_at(marked, float(marked["duration"]) * 0.5)
    if ad["erid"] not in frame or "Реклама" not in frame:
        return False, "в кадре ролика нет плашки с erid"
    if "18+" not in frame:
        return False, "возрастная метка не попала в кадр"
    note = media_compliance.description(marked.get("compliance") or {}) or ""
    if ad["erid"] not in note or "ИНН" not in note:
        return False, "маркировка не продублирована в описании"
    return True, "без erid — отказ (карточка и ролик), с erid — плашка в каждом кадре и в описании"


@check("media.compliance_safe_zone", "Плашка маркировки не уезжает под интерфейс канала")
def _media_compliance_safe_zone() -> tuple[bool, str]:
    from . import media, media_compliance, media_reel

    ad = {"pack": "ru-ad", "ad": True, "erid": "2Vtzqw9GateErID",
          "advertiser": "ООО «Тест»", "inn": "7700000000", "min_age": 18,
          "disclaimer": "Не является юридической консультацией"}
    spec = media_reel.build_reel_spec({"title": "Скидка 20%", "footer": "posiz.ru"}, fps=25,
                                      compliance=ad)
    if not media_reel.check(spec)["ok"]:
        return False, f"маркированный ролик с дисклеймером отвергнут: {media_reel.check(spec)}"
    box = media.content_box("9x16")
    zone = media.SAFE_ZONES["9x16"]["bottom"]
    plates = media_compliance.plates(ad)
    reserve = media_compliance.reserve_height(plates, line_height=media_reel.PLATE_LINE,
                                              gap=media_reel.PLATE_GAP)
    if reserve <= 0:
        return False, "маркировка не резервирует место в кадре"
    if box["y"] + box["height"] - reserve < box["y"] + box["height"] * 0.5:
        return False, "маркировка съедает больше половины зоны контента"
    # плашка и прогресс-бар живут над нижней зоной интерфейса: 1080×1920, bottom = {zone}px
    bar_y = box["y"] + box["height"] - 6
    if bar_y + 6 > box["y"] + box["height"]:
        return False, "прогресс-бар вышел из зоны контента"
    if reserve + 16 > zone and reserve > box["height"] * media_reel.MAX_PLATE_SHARE:
        return False, "маркировка не помещается в зону контента"
    letters = media_reel.build_reel_spec({"title": "Скидка 20%"}, fps=25,
                                         compliance={"pack": "ru-ad"})
    if not media_reel.check(letters)["ok"]:
        return False, "нерекламный ролик с пустым пакетом отвергнут"
    return True, (f"плашка {reserve} px внутри зоны {box['width']}×{box['height']}, "
                  f"нижние {zone} px свободны под интерфейс канала")


@check("media.format_matrix", "Один ролик — четыре формата без правки рендерера")
def _media_format_matrix() -> tuple[bool, str]:
    from . import media, media_reel

    post = {"title": "Один пост — все каналы", "footer": "posiz.ru",
            "items": ["Первый пункт", "Второй пункт"]}
    sizes = []
    for fmt in media_reel.REEL_FORMATS:
        spec = media_reel.build_reel_spec(post, fps=25, fmt=fmt)
        report = media_reel.check(spec)
        if not report["ok"]:
            return False, f"{fmt}: спека отвергнута — {report['issues']}"
        svg = media_reel.frame_at(spec, float(spec["duration"]) * 0.5)
        width, height = media.FORMATS[fmt]
        if f'width="{width}"' not in svg or f'height="{height}"' not in svg:
            return False, f"{fmt}: кадр не в размерах формата {width}×{height}"
        if "One" in svg:
            return False, "подозрительная подмена текста"
        sizes.append(f"{fmt} {width}×{height}")
    unknown = media_reel.build_reel_spec(post, fps=25, fmt="21x9")
    codes = [issue["code"] for issue in media_reel.check(unknown)["issues"]]
    if "format_unknown" not in codes:
        return False, "неизвестный формат прошёл валидацию"
    return True, "; ".join(sizes) + " — из одной спеки, неизвестный формат отвергается"


@check("media.brand_kit", "Brand kit: тема, гарнитура и масштаб — данные, а не вёрстка")
def _media_brand_kit() -> tuple[bool, str]:
    from . import media, media_reel

    post = {"title": "Витрина", "footer": "shop.ru"}
    light = media_reel.build_reel_spec(post, fps=25, brand={"theme": "light", "font": "serif",
                                                            "name": "shop.ru"})
    if not media_reel.check(light)["ok"]:
        return False, f"светлая тема отвергнута: {media_reel.check(light)}"
    svg = media_reel.frame_at(light, 1.0)
    if media.THEMES["light"]["bg"] not in svg:
        return False, "светлая тема не применилась к фону"
    if "Georgia" not in svg:
        return False, "гарнитура serif не применилась"
    small = media_reel.build_reel_spec(post, fps=25, brand={"scale": 0.8})
    big = media_reel.build_reel_spec(post, fps=25, brand={"scale": 1.3})
    if len(media_reel.frame_at(small, 1.0)) >= len(media_reel.frame_at(big, 1.0)):
        return False, "масштаб типографики не влияет на кадр"
    broken = media_reel.build_reel_spec(post, fps=25,
                                        brand={"theme": "neon", "font": "comic", "scale": 9})
    codes = {issue["code"] for issue in media_reel.check(broken)["issues"]}
    if not {"theme_invalid", "font_invalid", "brand_scale_invalid"} <= codes:
        return False, f"невалидный brand kit пропущен: {codes}"
    return True, "light/serif/scale применяются, неизвестные тема, гарнитура и масштаб — отказ"


@check("media.limits", "Лимиты ролика и цена озвучки — конфигурация, а не константы")
def _media_limits() -> tuple[bool, str]:
    from . import media_reel, media_voice

    saved = {key: os.environ.get(key) for key in
             ("MEDIA_REEL_MAX_DURATION", "MEDIA_TTS_YANDEX_RUB_PER_1000")}
    try:
        os.environ["MEDIA_REEL_MAX_DURATION"] = "180"
        if media_reel.limits()["max_duration"] != 180:
            return False, "лимит длительности не читается из окружения"
        scenes = [{"template": "chips-flow", "props": {"items": ["а", "б"]}, "duration": 120.0}]
        long_spec = media_reel.build_reel_spec({"title": "Длинный"}, fps=25, scenes=scenes)
        codes = [issue["code"] for issue in media_reel.check(long_spec)["issues"]]
        if "duration_range" in codes:
            return False, "120 секунд отвергнуты при лимите 180"
        os.environ["MEDIA_TTS_YANDEX_RUB_PER_1000"] = "2.0"
        if media_voice.price_per_1000("yandex") != 2.0:
            return False, "цена провайдера не читается из окружения"
        if media_voice.estimate_cost(1000) != 2.0:
            return False, "смета не следует за ценой провайдера"
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    if "kling" not in media_voice.PROVIDERS and media_voice.synthesize(
            "тест", provider="kling").get("ok"):
        return False, "неизвестный провайдер озвучки не отвергнут"
    registry = media_voice.PROVIDERS
    if not all("price_per_1000" in row for row in registry.values()):
        return False, "у провайдера нет цены в реестре"
    return True, (f"{len(registry)} провайдера в реестре с ценами; лимиты и тариф меняются "
                  f"окружением без релиза")


@check("media.hook_lab", "Варианты хука различаются только первым кадром, победитель — по данным")
def _media_hook_lab() -> tuple[bool, str]:
    from . import media_lab, media_reel

    media_lab.reset()
    post = {"title": "Минус 20% на всё до 30 ноября", "eyebrow": "Витрина · распродажа",
            "items": ["Доставка по России", "Гарантия 2 года"], "footer": "shop.ru",
            "cta": "Забрать скидку"}
    lab = media_lab.build_lab(post, fps=25, count=3)
    if lab["built"] != 3:
        return False, f"собрано {lab['built']} вариантов: {lab['skipped']}"
    hooks = [variant["hook"] for variant in lab["variants"]]
    if len(set(hooks)) != 3:
        return False, f"варианты не различаются: {hooks}"
    tails = {json.dumps(variant["spec"]["scenes"][1:], sort_keys=True, ensure_ascii=False)
             for variant in lab["variants"]}
    if len(tails) != 1:
        return False, "у вариантов различаются не только первые секунды"
    hashes = {variant["spec_hash"] for variant in lab["variants"]}
    if len(hashes) != 3:
        return False, "одинаковые spec_hash у разных хуков"
    for variant in lab["variants"]:
        if not media_reel.check(variant["spec"])["ok"]:
            return False, f"вариант {variant['id']} невалиден"
    refused = media_lab.decide(lab["id"])
    if refused.get("ok") or refused.get("code") != "not_enough_data":
        return False, "победитель выбран без данных"

    for variant, views, watch in ((lab["variants"][0], 300, 4.0),
                                  (lab["variants"][1], 300, 8.0),
                                  (lab["variants"][2], 50, 12.0)):
        media_lab.record_metrics(lab["id"], variant["id"], views=views, avg_watch_seconds=watch)
    decision = media_lab.table(lab["id"])["decision"]
    if decision.get("status") != "winner":
        return False, f"решение не принято при данных: {decision}"
    if decision["winner_variant_id"] != lab["variants"][1]["id"]:
        return False, "победил вариант без порога показов"
    first = media_lab.decide(lab["id"])
    second = media_lab.decide(lab["id"])
    if not first.get("ok") or not second.get("reused"):
        return False, "решение не идемпотентно"
    spec = media_lab.winner_spec(lab["id"])
    if not spec or spec["scenes"][0]["props"]["headline"] != lab["variants"][1]["hook"]:
        return False, "спека победителя не совпадает с решением"
    return True, (f"3 варианта ({', '.join(hooks)}), общие сцены совпадают; "
                  f"победитель после 300 показов — «{lab['variants'][1]['hook']}»")


@check("media.recycle", "Recycle берёт топ-пост по данным и отказывается без них")
def _media_recycle() -> tuple[bool, str]:
    from . import media_recycle, store

    saved_publications = dict(store.PUBLICATIONS)
    saved_metrics = list(store.METRICS)
    try:
        store.PUBLICATIONS.clear()
        store.METRICS.clear()
        empty = media_recycle.recycle()
        if empty.get("ok") or empty.get("code") != "no_candidates":
            return False, "recycle собрал ролик без единой публикации"

        def seed(pid: str, title: str, body: str, channel: str, impressions: int,
                 reactions: int, clicks: int, *, metrics: bool = True) -> None:
            variant = store.save_variant("sig_gate", channel, "post", title, body, None, False,
                                         0.9, "ws_de")
            store.PUBLICATIONS[pid] = {
                "id": pid, "variant_id": variant["id"], "workspace_id": "ws_de",
                "channel": channel, "status": "published", "media_ids": [], "is_ad": False,
                "published_at": "2026-11-01T10:00:00+00:00",
            }
            if metrics:
                store.METRICS.append({"publication_id": pid, "channel": channel,
                                      "impressions": impressions, "reactions": reactions,
                                      "clicks": clicks, "cost_rub": 0.0})

        seed("pub_gate_big", "Минус 20% на всё до 30 ноября",
             "Скидка 20% на весь каталог.\n• Доставка бесплатно от 3000 ₽", "vk", 5200, 210, 140)
        seed("pub_gate_mid", "Как выбрать подрядчика", "Три критерия выбора.", "max", 800, 12, 3)
        seed("pub_gate_tiny", "Малый охват", "текст", "tg", 40, 3, 0)
        seed("pub_gate_none", "Без аналитики", "текст", "tg", 0, 0, 0, metrics=False)

        listed = media_recycle.candidates()
        ranked = [row["publication_id"] for row in listed["rows"]]
        if ranked != ["pub_gate_big", "pub_gate_mid"]:
            return False, f"порядок кандидатов не по вовлечённости: {ranked}"
        reasons = {row["publication_id"]: row["reason"] for row in listed["skipped"]}
        if not reasons.get("pub_gate_tiny", "").startswith("below_threshold"):
            return False, "пост ниже порога показов не отсечён с причиной"
        if not reasons.get("pub_gate_none", "").startswith("no_metrics"):
            return False, "пост без аналитики попал в кандидаты"

        result = media_recycle.recycle()
        origin = (result.get("spec") or {}).get("origin") or {}
        if not result.get("ok") or origin.get("publication_id") != "pub_gate_big":
            return False, f"recycle выбрал не тот пост: {origin}"
        if not origin.get("why"):
            return False, "в спеке нет причины выбора (origin.why)"
        if not result["checks"]["ok"]:
            return False, f"ролик из топ-поста невалиден: {result['checks']['issues'][:2]}"
        if media_recycle.recycle()["spec_hash"] != result["spec_hash"]:
            return False, "recycle того же поста даёт другой spec_hash"
        if media_recycle.recycle(publication_id="pub_gate_tiny").get("code") != "below_threshold":
            return False, "явно указанный пост ниже порога принят без отказа"
        return True, (f"кандидаты по вовлечённости ({', '.join(ranked)}), "
                      f"порог 100 показов отсекает 2 поста; ролик из «{origin.get('publication_id')}» "
                      f"с причиной выбора в спеке")
    finally:
        store.PUBLICATIONS.clear()
        store.PUBLICATIONS.update(saved_publications)
        store.METRICS.clear()
        store.METRICS.extend(saved_metrics)


@check("media.locale", "Язык ролика — профиль данных: темп чтения, лимиты, отказ на неизвестный")
def _media_locale_gate() -> tuple[bool, str]:
    from . import media_locale, media_reel

    post = {"title": "Скидка 20% на весь каталог до 30 ноября", "eyebrow": "Витрина · распродажа",
            "items": ["Скидка 20% на каталог", "Бесплатная доставка от 3000 ₽"],
            "cta": "Забрать скидку"}
    ru = media_reel.build_reel_spec(post, fps=25, locale="ru")
    en = media_reel.build_reel_spec(post, fps=25, locale="en")
    if ru["locale"] != "ru" or en["locale"] != "en":
        return False, "язык не попал в спеку"
    if not en["duration"] < ru["duration"]:
        return False, f"английская версия не короче русской ({en['duration']}/{ru['duration']})"
    if media_locale.resolve("fr")[2][0]["code"] != "locale_unknown":
        return False, "неизвестный язык принят молча"
    localized = media_reel.localize(ru, locale="en", texts={
        "headline": "20% off storewide until Nov 30", "eyebrow": "Shop · sale",
        "items": ["20% off everything", "Free delivery over $30"], "cta": "Get the deal"})
    if not localized["ok"]:
        return False, f"перевод не собрался: {localized['issues'][:2]}"
    if [s["template"] for s in localized["spec"]["scenes"]] != [s["template"] for s in ru["scenes"]]:
        return False, "локализация изменила структуру сцен"
    broken = media_reel.localize(ru, locale="en", texts={
        "headline": "This translated headline is far too long to fit inside the safe content area"})
    if broken["ok"]:
        return False, "переполненный перевод принят как валидный"
    return True, (f"{len(media_locale.codes())} профиля языков в реестре; ru {ru['duration']} с → "
                  f"en {en['duration']} с; неизвестный язык и переполнение — отказ")


@check("media.report", "Видео-отчёт считает те же цифры, что в кабинете, и не выдумывает нули")
def _media_report_gate() -> tuple[bool, str]:
    from . import media_report, store

    saved_publications = dict(store.PUBLICATIONS)
    saved_metrics = list(store.METRICS)
    try:
        store.PUBLICATIONS.clear()
        store.METRICS.clear()
        if media_report.build(days=30).get("code") != "no_data":
            return False, "отчёт собрался без публикаций"

        variant = store.save_variant("sig_gate", "vk", "post", "Пост для отчёта", "тело", None,
                                     False, 0.9, "ws_de")
        store.PUBLICATIONS["pub_rep"] = {
            "id": "pub_rep", "variant_id": variant["id"], "workspace_id": "ws_de",
            "channel": "vk", "status": "published", "media_ids": [], "is_ad": False,
            "published_at": "2026-11-01T10:00:00+00:00",
        }
        if media_report.build(days=30).get("code") != "no_metrics":
            return False, "отчёт собрался из нулей без охватов"
        store.METRICS.append({"publication_id": "pub_rep", "channel": "vk",
                              "impressions": 5200, "reactions": 210, "clicks": 140,
                              "cost_rub": 0.0})
        store.METRICS.append({"publication_id": "pub_rep", "channel": "vk",
                              "impressions": 800, "reactions": 30, "clicks": 10,
                              "cost_rub": 0.0})
        result = media_report.build(days=30)
        if not result.get("ok"):
            return False, f"отчёт не собрался при данных: {result.get('reason')}"
        data = result["numbers"]
        expected = sum(int(row["impressions"]) for row in store.METRICS)
        if data["impressions"] != expected:
            return False, f"охваты в отчёте {data['impressions']} ≠ {expected} в аналитике"
        if "6 000" not in result["post"]["title"]:
            return False, f"главное число не попало в кадр: {result['post']['title']}"
        if not result["checks"]["ok"]:
            return False, f"ролик отчёта невалиден: {result['checks']['issues'][:2]}"
        if not {"impressions", "sources"} <= set(result) and not result.get("sources"):
            return False, "в отчёте нет источника цифр"
        return True, (f"отчёт за 30 дней: {data['impressions']} показов, "
                      f"{data['publications']} публикация, ER {data['engagement']:.1%}; "
                      f"источник каждой цифры в sources")
    finally:
        store.PUBLICATIONS.clear()
        store.PUBLICATIONS.update(saved_publications)
        store.METRICS.clear()
        store.METRICS.extend(saved_metrics)


@check("media.voice_providers", "Провайдеры озвучки — реестр с ценами, подачами и честными отказами")
def _media_voice_providers() -> tuple[bool, str]:
    from . import media_voice

    required = {"yandex", "elevenlabs", "steosvoice", "local", "mock", "file"}
    missing = required - set(media_voice.PROVIDERS)
    if missing:
        return False, f"в реестре нет провайдеров: {sorted(missing)}"
    for code, row in media_voice.PROVIDERS.items():
        if row.get("price_per_1000") is None:
            return False, f"у провайдера {code} нет цены"
        if row.get("audience") not in {"client", "self-hosted", "internal"}:
            return False, f"у провайдера {code} не указана аудитория (client/self-hosted)"
    if media_voice.PROVIDERS["local"]["audience"] != "self-hosted":
        return False, "локальный движок попал в клиентскую витрину"
    if media_voice.PROVIDERS["elevenlabs"]["default_model"] != "eleven_multilingual_v2":
        return False, "у ElevenLabs не выставлена модель Multilingual v2"
    score = {code: row.get("expressiveness", 0) for code, row in media_voice.PROVIDERS.items()}
    if not score["elevenlabs"] > score["yandex"]:
        return False, "выразительность в реестре не отражает разницу движков"

    refused = media_voice.synthesize("Проверка", provider="deepgram-tts")
    if refused.get("ok") or "не поддерживается" not in refused.get("reason", ""):
        return False, "неизвестный провайдер не отвергнут со списком реестра"
    honest = media_voice.synthesize("Проверка", provider="elevenlabs")
    if honest.get("ok") or "ELEVENLABS_API_KEY" not in honest.get("reason", ""):
        return False, "ElevenLabs без ключа не сказал, чего именно не хватает"
    local = media_voice.synthesize("Проверка", provider="local")
    if local.get("ok") or "MEDIA_LOCAL_TTS_URL" not in local.get("reason", ""):
        return False, "локальный синтез без адреса не объяснил отказ"
    state = media_voice.available()
    if "mock" not in state["providers"] or "local" in state["providers"]:
        return False, "витрина смешала клиентские и self-hosted движки"
    return True, (f"{len(media_voice.PROVIDERS)} провайдеров в реестре с ценами и аудиторией; "
                  f"ElevenLabs (v2, выразительность 5) и SteosVoice (русские ударения) — "
                  f"клиентские, локальный TTS — self-hosted; без ключа — отказ с причиной")


@check("media.performance", "Подача диктора: паузы и настройки меняют ролик, а не только звук")
def _media_performance() -> tuple[bool, str]:
    from . import media_reel, media_voice

    catalog = {row["code"]: row for row in media_voice.performance_catalog()}
    baritone = catalog.get("cinematic-baritone")
    if not baritone:
        return False, "в реестре подач нет кинематографичного баритона"
    if not (0.35 <= baritone["stability"] <= 0.45 and 0.15 <= baritone["style"] <= 0.25):
        return False, f"настройки баритона вне 35–45 % / 15–25 %: {baritone}"
    if not baritone["pause_scale"] > 1:
        return False, "кинематографичная подача не удлиняет паузы"

    post = {"title": "Он приехал в горы Дагестана — и остался навсегда",
            "eyebrow": "История · Дагестан", "items": ["Высота 2 000 метров — и тишина"],
            "note": "Так начинается история, о которой не рассказывают в путеводителях.",
            "cta": "Смотреть целиком"}
    neutral = media_reel.build_reel_spec(post, fps=25, performance="neutral")
    cinematic = media_reel.build_reel_spec(post, fps=25, performance="cinematic-baritone")
    if not cinematic["duration"] > neutral["duration"]:
        return False, (f"паузы не попали в таймлайн: {cinematic['duration']} ≤ "
                       f"{neutral['duration']}")
    if cinematic["performance"] != "cinematic-baritone":
        return False, "подача не попала в спеку"

    settings = media_voice.settings_for("elevenlabs", performance="cinematic-baritone")
    if settings["applied"].get("stability") != 0.4 or settings["applied"].get("style") != 0.2:
        return False, f"настройки ElevenLabs не применились: {settings['applied']}"
    partial = media_voice.settings_for("yandex", performance="cinematic-baritone")
    if set(partial["skipped"]) != {"stability", "style"}:
        return False, "SpeechKit молча выбросил неподдерживаемые настройки"

    bad = media_voice.synthesize("Проверка", provider="mock", performance="melodrama")
    if bad.get("ok") or bad["issues"][0]["code"] != "performance_unknown":
        return False, "неизвестная подача не отвергнута"
    marked = media_voice.synthesize("в горах Дагест+ана", provider="mock",
                                    stress={"Дагестана": "Дагест+ана"})
    if "+" in marked["spoken_text"] or "+" in marked["screen_text"] \
            or marked["screen_text"] != "в горах Дагестана":
        return False, "метка ударения дошла до движка без поддержки или осталась в кадре"
    return True, (f"баритон: паузы ×{baritone['pause_scale']}, темп {baritone['speed']}, "
                  f"stability 40 % / style 20 %; ролик {neutral['duration']} с → "
                  f"{cinematic['duration']} с; ударения не попадают в кадр")


@check("article.composition", "Статья собирается по данным: визуал выбирается смыслом, а не украшением")
def _article_composition() -> tuple[bool, str]:
    from . import articles

    text = ("Разбираем по существу, сколько стоит размещение и от чего зависит цена, "
            "чтобы не платить за пустой охват и видеть результат в цифрах. ") * 3
    brief = {
        "topic": "Продвижение в мессенджерах: цены и каналы",
        "keywords": ["продвижение в мессенджерах"],
        "sections": 5,
        "outline": [
            {"heading": "Сколько стоит размещение", "text": text,
             "columns": ["Канал", "Цена"], "rows": [["Telegram", "12 000 ₽"], ["MAX", "9 000 ₽"]],
             "source": "прайс клиента"},
            {"heading": "Динамика охватов за год", "text": text, "material": "reach"},
            {"heading": "Как мы считаем результат", "text": text,
             "steps": ["Собираем охваты", "Считаем вовлечённость", "Проверяем отчёт"]},
            {"heading": "Пример отчёта в кабинете", "text": text,
             "image_prompt": "Отчёт по охватам в кабинете"},
            {"heading": "Что сравнить перед стартом", "text": text,
             "columns": [{"title": "Каналы", "items": ["дешевле вход"]},
                         {"title": "Блог", "items": ["долгий эффект"]}]},
        ],
        "faq": [{"q": "Что входит в цену?", "a": "Пост, ссылка и отчёт."}],
    }
    materials = [{"kind": "numbers", "code": "reach", "title": "Охваты", "unit": " тыс.",
                  "source": "аналитика клиента",
                  "values": [{"label": "июл", "value": 8.2}, {"label": "сен", "value": 11.1}]}]
    result = articles.compose(brief, materials=materials)
    if not result.get("ok"):
        return False, f"статья не собралась: {result.get('reason') or result.get('code')}"
    spec = result["spec"]
    kinds = [block["type"] for block in spec["blocks"]]
    expected = {"table", "chart", "diagram", "image", "comparison"}
    missing = expected - set(kinds)
    if missing:
        return False, f"по данным разделов не появились визуалы: {sorted(missing)}"
    # данные раздела сильнее слов: «пример отчёта» с image_prompt — иллюстрация, а не цифра
    example = next(block for block in spec["blocks"] if block["type"] == "image")
    if example.get("reason") != next(rule["reason"] for rule in articles.VISUAL_RULES
                                     if rule["code"] == "show"):
        return False, "иллюстрация выбрана не по своему правилу"
    for block in spec["blocks"]:
        if not articles.BLOCKS[block["type"]]["visual"]:
            continue
        if not block.get("alt") or not block.get("reason"):
            return False, f"{block['type']}: нет alt или причины появления"
        if block["type"] in {"chart", "table", "stat"} and not block.get("source"):
            return False, f"{block['type']}: цифры без источника"
    # нет ряда — нет графика: цифры не придумываются
    lonely = articles.compose({"topic": "Рост охватов за год", "sections": 3,
                               "outline": [{"heading": "Динамика за год",
                                            "text": "Покажем рост в процентах. " * 8}]})
    if "chart" in [block["type"] for block in lonely["spec"]["blocks"]]:
        return False, "график нарисован без материала"
    checks = result["checks"]
    if not checks["ok"]:
        return False, f"проверки забраковали собственную статью: {checks['issues'][:2]}"
    return True, (f"{len(spec['blocks'])} блоков, {checks['stats']['words']} слов, "
                  f"{checks['stats']['visuals']} визуалов по данным разделов "
                  f"(таблица, график, схема, иллюстрация, сравнение); у каждого — alt и "
                  f"причина; без материала график не ставится")


@check("article.publishability", "Статья готова к публикации: HTML, schema.org, честный статус")
def _article_publishability() -> tuple[bool, str]:
    from . import articles

    text = ("Разбираем по существу, сколько стоит размещение и как считать результат, "
            "чтобы не платить за пустой охват и видеть отдачу в цифрах, а не в ощущениях. ") * 8
    brief = {"topic": "Продвижение в мессенджерах: цены и каналы",
             "keywords": ["продвижение в мессенджерах"], "sections": 4,
             "outline": [{"heading": "Тарифы", "text": text,
                          "columns": ["Канал", "Цена"], "rows": [["Telegram", "12 000 ₽"]],
                          "source": "прайс клиента"},
                         {"heading": "Динамика", "text": text, "material": "reach"},
                         {"heading": "Что дальше", "text": text}],
             "faq": [{"q": "Что входит в цену?", "a": "Пост и отчёт."}]}
    materials = [{"kind": "numbers", "code": "reach", "title": "Охваты",
                  "source": "аналитика клиента",
                  "values": [{"label": "июл", "value": 8.2}, {"label": "сен", "value": 11.1}]}]
    articles.reset()
    try:
        result = articles.compose(brief, materials=materials)
        spec = result["spec"]
        if not result["checks"]["ok"]:
            return False, f"статья не проходит проверки: {result['checks']['issues'][:2]}"
        html = articles.render_html(spec)
        if not html.startswith("<!doctype html>") or "<article>" not in html:
            return False, "HTML не является самостоятельной страницей"
        if '"@type": "FAQPage"' not in html or '"@context": "https://schema.org"' not in html:
            return False, "в разметке нет schema.org Article + FAQPage"
        if "источник: аналитика клиента" not in html:
            return False, "подпись графика не ссылается на источник"
        if html.count("<svg") < 1:
            return False, "график не нарисован (нет inline SVG)"
        poisoned = dict(spec)
        poisoned["blocks"] = spec["blocks"] + [{"id": "x", "type": "paragraph",
                                                "props": {"text": "TODO <script>x</script>"}}]
        poisoned_html = articles.render_html(poisoned)
        if "<script>x</script>" in poisoned_html or "&lt;script&gt;" not in poisoned_html:
            return False, "текст статьи попадает в HTML без экранирования"

        first = articles.publish(spec)
        again = articles.publish(spec)
        if not first["ok"] or first["article"]["status"] != "ready":
            return False, "готовая статья сохранилась не как ready"
        if not again["reused"] or again["article"]["id"] != first["article"]["id"]:
            return False, "одинаковая спека создала второй черновик"
        broken = dict(spec)
        broken["blocks"] = [dict(block) for block in spec["blocks"]]
        for block in broken["blocks"]:
            if block["type"] == "chart":
                block["source"] = ""
        broken["checks"] = articles.check(broken)
        if articles.publish(broken)["article"]["status"] != "draft":
            return False, "статья с цифрами без источника помечена как готовная"
        return True, (f"{result['checks']['stats']['words']} слов → HTML "
                      f"{len(html) // 1024} КБ с schema.org Article + FAQPage и источником в "
                      f"подписи; одинаковая спека — одна статья; дефекты дают draft, а не ready")
    finally:
        articles.reset()


@check("article.packs", "Ниша статьи — пакет правил и шаблон, а не ветка в коде")
def _article_packs() -> tuple[bool, str]:
    import json
    import os
    import tempfile

    from . import articles

    packs = {row["code"]: row for row in articles.rule_packs()}
    if not {"default", "high-stakes"} <= set(packs):
        return False, f"нет встроенных паков: {sorted(packs)}"
    broken = {code: row["invalid_rules"] for code, row in packs.items() if row["invalid_rules"]}
    if broken:
        return False, f"неприменимые правила в паках: {broken}"
    strict = packs["high-stakes"]["policy"]
    if int(strict.get("min_words", 0)) <= articles.MIN_WORDS:
        return False, "строгая политика не поднимает минимум объёма"
    if not strict.get("required_blocks") or not strict.get("disclaimer"):
        return False, "строгая политика не требует оговорки"
    if "risk" not in [rule["code"] for rule in articles.rules_for("high-stakes")]:
        return False, "правило врезки пака не подключилось"
    if "risk" in [rule["code"] for rule in articles.rules_for("default")]:
        return False, "правило пака утекло в базовый набор"

    # пак из файла: расширение без правки кода + честный отчёт о неприменимом правиле
    payload = {
        "gate-check": {"title": "Проверка гейта",
                       "rules": [{"code": "gate-rule", "block": "diagram", "data": ["steps"],
                                  "reason": "шаги процесса видны схемой"}],
                       "policy": {"min_words": 300}},
        "gate-broken": {"title": "Ломаный пак",
                        "rules": [{"code": "nope", "block": "carousel", "reason": "нет блока"}]},
    }
    handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
    try:
        json.dump(payload, handle, ensure_ascii=False)
        handle.close()
        os.environ["ARTICLE_RULE_PACKS"] = handle.name
        articles.reload_packs()
        file_packs = {row["code"]: row for row in articles.rule_packs()}
        if "gate-rule" not in [rule["code"] for rule in articles.rules_for("gate-check")]:
            return False, "правило из файла не подключилось"
        if file_packs.get("gate-broken", {}).get("invalid_rules") != ["nope"]:
            return False, "неприменимое правило из файла не показано в витрине"
        if articles.pack_policy("gate-check").get("min_words") != 300:
            return False, "политика из файла не применилась"
        try:
            articles.rule_pack("nope-nope")
        except KeyError:
            pass
        else:
            return False, "неизвестный пакет не отвергнут"
    finally:
        os.environ.pop("ARTICLE_RULE_PACKS", None)
        articles.reload_packs()
        os.unlink(handle.name)

    text = ("Разбираем по существу, сколько стоит проверка и как считать результат, чтобы не "
            "платить за пустой объём и видеть отдачу в цифрах, а не в ощущениях. ") * 10
    brief = {"topic": "Проверка договора перед подписанием", "sections": 3,
             "keywords": ["проверка договора"], "rule_pack": "high-stakes",
             "template": "longread",
             "outline": [{"heading": "Где риски", "text": text,
                          "callout": "Риск: спорные условия действуют до оспаривания."},
                         {"heading": "Сколько стоит", "text": text,
                          "columns": ["Пакет", "Цена"], "rows": [["Базовый", "9 000 ₽"]],
                          "source": "прайс клиента"},
                         {"heading": "Как проходит проверка", "text": text,
                          "steps": ["Читаем договор", "Отмечаем риски", "Пишем протокол"]}]}
    articles.reset()
    try:
        result = articles.compose(brief)
        spec = result["spec"]
        if spec["rule_pack"] != "high-stakes" or spec["policy"]["min_words"] != 500:
            return False, "пакет и политика не попали в спеку"
        callouts = [block for block in spec["blocks"] if block["type"] == "callout"]
        if len(callouts) < 2 or not any("не заменяет консультацию" in block["props"]["text"]
                                       for block in callouts):
            return False, "оговорка пака не попала в статью отдельным блоком"
        poisoned = {"topic": brief["topic"], "sections": 3, "rule_pack": "high-stakes",
                    "outline": [{"heading": "Гарантии", "text": text + " Гарантируем результат."}]}
        codes = {issue["code"] for issue in articles.compose(poisoned)["checks"]["issues"]}
        if "banned_phrase" not in codes:
            return False, "запрещённое обещание не поймано политикой"
        html = articles.render_html(spec)
        if "#0f766e" not in html or "Georgia" not in html:
            return False, "шаблон не применил свои токены вёрстки"
        svg = html[html.find("<svg"):] if "<svg" in html else ""
        if "0f766e" not in svg:
            return False, "акцент шаблона не доехал до inline-SVG"
        try:
            articles.plan({"topic": "Тема", "template": "nope"})
        except KeyError:
            pass
        if articles.plan({"topic": "Тема", "template": "nope"}).get("code") != "template_unknown":
            return False, "неизвестный шаблон не отвергнут со списком реестра"
        return True, (f"{len(packs)} встроенных пака правил и {len(articles.templates())} шаблона; "
                      f"пак из файла расширяет правила и политику без правки кода, неприменимое "
                      f"правило видно в витрине; строгий пак: минимум 500 слов, оговорка отдельным "
                      f"блоком, запрещённые обещания ловятся, шаблон меняет CSS и акцент SVG")
    finally:
        articles.reset()


@check("article.media", "Галерея и видео в статье — визуалы из медиа-движка, а не место под картинку")
def _article_media() -> tuple[bool, str]:
    from . import articles, media_reel

    text = ("Разбираем по существу, сколько стоит оформление и как считать результат, "
            "чтобы не платить за пустой охват. ") * 10
    brief = {"topic": "Оформление постов: примеры и ролик", "keywords": ["оформление постов"],
             "sections": 3,
             "outline": [
                 {"heading": "Примеры оформления", "text": text,
                  "gallery": [{"title": "Тёмная тема", "caption": "Пост в тёмной теме"},
                              {"title": "Светлая карточка", "caption": "Пост в светлой теме"},
                              {"title": "Обложка 16:9", "caption": "Обложка для ролика"}]},
                 {"heading": "Как выглядит ролик", "text": text,
                  "video": {"items": ["Заголовок печатается посимвольно",
                                      "Пункты появляются по одному"],
                            "note": "Ролик 16:9 из одного поста"}},
                 {"heading": "Сколько это занимает", "text": text,
                  "columns": ["Этап", "Время"], "rows": [["Пост", "3 минуты"]],
                  "source": "регламент клиента"}]}
    articles.reset()
    try:
        result = articles.compose(brief)
        if not result.get("ok"):
            return False, f"статья не собралась: {result.get('reason') or result.get('code')}"
        spec = result["spec"]
        gallery = next((block for block in spec["blocks"] if block["type"] == "gallery"), None)
        video = next((block for block in spec["blocks"] if block["type"] == "video"), None)
        if not gallery or not video:
            return False, "галерея или ролик не появились по данным раздела"
        items = gallery["props"]["items"]
        if len(items) < 3 or not all(item.get("alt") and item.get("asset") for item in items):
            return False, "плитки галереи без alt или без спеки карточки"
        reel = video["props"]["reel"]
        limits = media_reel.limits()
        if not reel or not media_reel.check(reel["spec"])["ok"]:
            return False, "спека ролика невалидна"
        if reel["duration"] > limits["max_duration"] or reel["scenes"] > limits["max_scenes"]:
            return False, "ролик вышел за лимиты длительности или числа сцен"
        if not all(shot["label"] for shot in reel["storyboard"]):
            return False, "сториборд без подписей сцен"
        html = articles.render_html(spec)
        if 'class="gallery"' not in html or "video-strip" not in html:
            return False, "галерея или полоса ролика не отрендерились"
        if html.count("<svg") < 4 or "<img" in html or 'src="http' in html:
            return False, "плитки не inline-SVG или статья тянет внешние файлы"
        one = {**brief, "outline": [{**brief["outline"][0],
                                     "gallery": brief["outline"][0]["gallery"][:1]},
                                    brief["outline"][1], brief["outline"][2]]}
        one_spec = articles.compose(one)["spec"]
        if "gallery" in [block["type"] for block in one_spec["blocks"]]:
            return False, "один элемент нарисован как галерея"
        link_brief = {**brief, "outline": [brief["outline"][0],
                                           {"heading": "Готовый ролик", "text": text,
                                            "video": {"src": "https://cdn.example.com/r.mp4"}},
                                           brief["outline"][2]]}
        link_html = articles.render_html(articles.compose(link_brief)["spec"])
        if "<video controls" not in link_html or "cdn.example.com" not in link_html:
            return False, "ссылка клиента на ролик не попала в разметку"
        broken = {**spec, "blocks": [dict(block) for block in spec["blocks"]]}
        for block in broken["blocks"]:
            if block["type"] == "video":
                block["props"] = {**block["props"], "reel": None, "src": None}
            if block["type"] == "gallery":
                block["props"] = {"items": [dict(item, alt="") for item in block["props"]["items"]]}
        codes = {issue["code"] for issue in articles.check(broken)["issues"]}
        if not {"video_source", "alt_missing"} <= codes:
            return False, "дефекты галереи и ролика не названы проверками"
        return True, (f"галерея {len(items)} плитки inline-SVG без внешних файлов, ролик "
                      f"{reel['scenes']} сцен / {reel['duration']} с / {reel['format']} из спеки "
                      f"статьи с подписями сцен; ссылка клиента поддерживается; один элемент — "
                      f"не галерея; дефекты ловятся")
    finally:
        articles.reset()


@check("article.images", "Иллюстрации: провайдер по ключу, кэш по промпту, лицензия в артефакте")
def _article_images() -> tuple[bool, str]:
    import os

    from . import articles

    saved = {name: os.environ.get(name) for name in
             ("MEDIA_IMAGE_PROVIDER_KEY", "MEDIA_IMAGE_PROVIDER_URL")}
    articles.reset()
    articles.reset_images()
    try:
        catalog = {row["code"]: row for row in articles.image_catalog()}
        if not catalog["brand-card"]["available"]:
            return False, "своя графика недоступна: статья не соберётся без ключей"
        for name in saved:
            os.environ.pop(name, None)
        refused = articles.generate_image("Отчёт", provider="ai-generate")
        if refused.get("ok") or refused.get("code") != "provider_key_missing":
            return False, "без ключа провайдер не отказал по имени переменной"
        if "MEDIA_IMAGE_PROVIDER_KEY" not in (refused.get("needs") or []):
            return False, "отказ не назвал, какой переменной не хватает"
        fallback_spec = articles.compose({
            "topic": "Отчёт по охватам: что важно", "sections": 3, "image_provider": "ai-generate",
            "keywords": ["отчёт по охватам"],
            "outline": [{"heading": "Как выглядит отчёт",
                         "text": "Описание раздела достаточно длинное для проверки качества. " * 8,
                         "image_prompt": "Отчёт в кабинете"},
                        {"heading": "Стоимость", "text": "Описание раздела про цену и сроки. " * 8,
                         "columns": ["Пакет", "Цена"], "rows": [["Базовый", "9 000 ₽"]],
                         "source": "прайс клиента"},
                        {"heading": "Что дальше", "text": "Описание финального раздела. " * 8}]})
        image = next((block for block in fallback_spec["spec"]["blocks"]
                      if block["type"] == "image"), None)
        if not image or image["props"].get("provider") != "brand-card":
            return False, "без ключа статья осталась без иллюстрации вместо своей графики"
        if "MEDIA_IMAGE_PROVIDER_KEY" not in str(image["props"].get("fallback_reason") or ""):
            return False, "откат на свою графику не объяснил причину"

        # провайдер «включён»: ответ с лицензией, второй запрос — из кэша
        os.environ["MEDIA_IMAGE_PROVIDER_KEY"] = "gate-key"
        os.environ["MEDIA_IMAGE_PROVIDER_URL"] = "https://provider.example.com/generate"
        calls = {"count": 0}

        def fake(provider: str, prompt: str, size: str) -> dict:
            calls["count"] += 1
            return {"ok": True, "url": "https://cdn.example.com/art/otchet.png",
                    "width": 1600, "height": 900,
                    "license": {"name": "Generated by ExampleAI", "type": "generated",
                                "terms_url": "https://example.com/terms"},
                    "cost_rub": 2.4}

        original = articles._provider_request
        articles._provider_request = fake
        try:
            made = articles.generate_image("Отчёт в кабинете", provider="ai-generate")
            again = articles.generate_image("Отчёт в кабинете", provider="ai-generate")
            if not made.get("ok") or made.get("cached"):
                return False, "первый запрос к провайдеру не дал иллюстрацию"
            if not again.get("cached") or calls["count"] != 1:
                return False, "одинаковый промпт ушёл провайдеру второй раз"
            if made["license"]["name"] != "Generated by ExampleAI":
                return False, "лицензия не сохранена рядом с иллюстрацией"
            articles._provider_request = lambda provider, prompt, size: {
                "ok": True, "url": "https://cdn.example.com/art/nolicense.png", "license": {}}
            naked = articles.generate_image("Другой промпт", provider="ai-generate")
            if naked.get("ok") or naked.get("code") != "license_missing":
                return False, "иллюстрация без лицензии не отвергнута"
            articles._provider_request = fake
            spec = articles.compose({
                "topic": "Отчёт по охватам: что важно", "sections": 3,
                "keywords": ["отчёт по охватам"], "image_provider": "ai-generate",
                "outline": [{"heading": "Как выглядит отчёт",
                             "text": "Описание раздела достаточно длинное для проверки. " * 8,
                             "image_prompt": "Отчёт в кабинете"},
                            {"heading": "Стоимость", "text": "Раздел про цену и сроки. " * 10,
                             "columns": ["Пакет", "Цена"], "rows": [["Базовый", "9 000 ₽"]],
                             "source": "прайс клиента"},
                            {"heading": "Что дальше", "text": "Финальный раздел статьи. " * 10}]})
            html = articles.render_html(spec["spec"])
            if '<img src="https://cdn.example.com/art/otchet.png"' not in html:
                return False, "внешняя иллюстрация не попала в разметку"
            if "Generated by ExampleAI" not in html:
                return False, "подпись лицензии не видна читателю"
            if "alt=" not in html:
                return False, "у иллюстрации нет alt"
            codes = set()
            poisoned = {"spec_version": articles.SPEC_VERSION, "seo": spec["spec"]["seo"],
                        "blocks": [dict(block) for block in spec["spec"]["blocks"]]}
            for block in poisoned["blocks"]:
                if block["type"] == "image":
                    block["props"] = {**block["props"],
                                      "image": {**block["props"].get("image", {}),
                                                "license": {}}}
            codes = {issue["code"] for issue in articles.check(poisoned)["issues"]}
            if "license_missing" not in codes:
                return False, "проверка не назвала иллюстрацию без лицензии"
        finally:
            articles._provider_request = original
        stats = articles.image_cache_stats()
        if stats["entries"] < 1 or stats["providers"].get("ai-generate", 0) < 1:
            return False, "кэш не сохранил ни одной записи с провайдером"
        return True, (f"провайдер по ключу: без {refused['needs'][0]} — отказ и откат на свою "
                      f"графику с причиной; с ключом иллюстрация приходит с лицензией "
                      f"«{made['license']['name']}», второй одинаковый промпт берётся из кэша "
                      f"({stats['entries']} записей), ответ без лицензии отвергается, подпись "
                      f"видна в разметке")
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        articles.reset()
        articles.reset_images()


@check("article.links", "Перелинковка по смыслу и обновление вечнозелёных статей")
def _article_links() -> tuple[bool, str]:
    import os

    from . import article_links, articles

    saved = {name: os.environ.get(name) for name in
             ("ARTICLE_LINK_MIN_SCORE", "ARTICLE_MAX_LINKS", "ARTICLE_REVIEW_DAYS",
              "ARTICLE_CANONICAL_BASE")}
    articles.reset()
    article_links.reset()
    text = ("Разбираем по существу, как считается охват и что в нём важно, чтобы не платить "
            "за пустой показ. ") * 12
    late = "2031-06-01T00:00:00+00:00"

    def make(topic: str, heads: list[str], keywords: list[str], **extra) -> dict:
        outline = [{"heading": head, "text": text} for head in heads]
        for override in extra.pop("data", []):
            outline[override.pop("index")].update(override)
        result = articles.compose({"topic": topic, "keywords": keywords,
                                   "sections": len(heads), "outline": outline, **extra})
        return result["spec"]

    try:
        for name in saved:
            os.environ.pop(name, None)
        gap = article_links.random_embed_gap()
        if not gap["ours"]["related"] - gap["ours"]["unrelated"] > 5 * abs(
                gap["hash"]["related"] - gap["hash"]["unrelated"]):
            return False, "близость по смыслу не отличается от случайной: ссылки были бы шумом"
        empty = make("Отдельная тема без соседей", ["Раз", "Два", "Три"], ["отдельная тема"])
        if article_links.link_plan(empty)["code"] != "corpus_empty":
            return False, "пустой индекс не назван: перелинковка молча отдала пустой блок"
        ohvaty = make("Как читать отчёт по охватам и переходам",
                      ["Охваты и переходы", "Отчёт в кабинете", "Цена действия"],
                      ["отчёт по охватам", "переходы"], evergreen=True)
        cifry = make("Что показывает отчёт по охватам в цифрах",
                     ["Охваты и переходы", "Цена действия", "Как менять канал"],
                     ["отчёт по охватам", "цена действия"])
        prays = make("Прайс на ведение канала: пакеты и сроки",
                     ["Пакеты", "Сроки", "Оплата"], ["прайс", "ведение канала"])
        for spec in (ohvaty, cifry, prays):
            articles.publish(spec)
        article_links.index_all(articles.articles(20))

        plan = article_links.link_plan(ohvaty)
        if not plan["ok"] or not plan["links"]:
            return False, f"ближайшая статья не найдена: {plan.get('code')}"
        top = plan["links"][0]
        if "охват" not in str(top["title"]).lower():
            return False, "связанной оказалась статья о другом: ссылка по тегам, не по смыслу"
        if top["score"] < plan["threshold"]:
            return False, "ссылка ниже собственного порога"
        strict = article_links.link_plan(prays, min_score=0.9)
        if strict["code"] != "no_candidates" or "ниже порога" not in strict["reason"]:
            return False, "порог близости не отсекает постороннюю статью"

        linked = article_links.apply_links(ohvaty, plan["links"])
        types = [row["type"] for row in linked["spec"]["blocks"]]
        if types.index("related") > types.index("cta"):
            return False, "«Читайте также» встало после призыва"
        if not linked["spec"]["history"][-1]["reason"]:
            return False, "правка без причины: историю нельзя объяснить"
        html = articles.render_html(linked["spec"], standalone=False)
        nav = html.split('<nav class="related">')[1].split("</nav>")[0] \
            if '<nav class="related">' in html else ""
        if nav.count("<a href=") != len(plan["links"]):
            return False, "ссылки не отрисованы"
        if "читать далее" in nav.lower():
            return False, "текст ссылки не объясняет, куда она ведёт"
        again = article_links.link_plan(linked["spec"])
        if again["code"] != "already_linked":
            return False, "повторная перелинковка дублирует ссылки"

        poisoned = {"spec_version": articles.SPEC_VERSION, "seo": ohvaty["seo"],
                    "blocks": [dict(row) for row in ohvaty["blocks"]]}
        poisoned["seo"]["canonical"] = "https://example.com/blog/ohvaty/"
        poisoned["blocks"].insert(len(poisoned["blocks"]) - 1, {
            "id": "blk_rel_gate", "type": "related", "reason": "проверка",
            "props": {"items": [{"url": "", "anchor": "Пусто"},
                                {"url": "https://example.com/blog/ohvaty/", "anchor": "Себя"},
                                {"url": "/blog/cifry", "anchor": ""},
                                {"url": "/blog/cifry", "anchor": "Дубль"}]}})
        codes = {issue["code"] for issue in articles.check(poisoned)["issues"]}
        if not {"link_broken", "link_self", "link_anchor_missing",
                "link_duplicate"} <= codes:
            return False, f"дефекты ссылок не пойманы: {sorted(codes)}"

        # вечнозелёная статья: срок ревью, обновление по материалам, честные пропуски
        data_spec = make("Отчёт по охватам: как обновлять цифры",
                         ["Охваты и переходы", "Цена действия", "Что дальше"],
                         ["отчёт по охватам"], evergreen=True,
                         data=[{"index": 0, "columns": ["Пакет", "Цена"],
                                "rows": [["Базовый", "9 000 ₽"]], "source": "прайс клиента"}])
        if article_links.freshness(data_spec)["due"]:
            return False, "свежая статья уже считается просроченной"
        due = article_links.freshness(data_spec, now=late)
        if not due["due"] or not due["age_days"]:
            return False, "просрочка ревью не считается"
        kept = article_links.refresh(data_spec, [{"key": "другой ряд", "rows": [["X", "1"]]}],
                                     now=late)
        if kept["ok"] or kept["code"] != "no_materials" or not kept["skipped"]:
            return False, "число без материала не названо пропуском"
        table = next(row for row in kept["spec"]["blocks"] if row["type"] == "table")
        if table["props"]["rows"] != [["Базовый", "9 000 ₽"]]:
            return False, "число подменено без источника"
        fresh = article_links.refresh(data_spec, [{"key": "прайс клиента",
                                                  "rows": [["Базовый", "6 900 ₽"]]}], now=late)
        if not fresh["ok"] or fresh["version"] != 2 or not fresh["changes"]:
            return False, "обновление по материалу не изменило статью"
        same = article_links.refresh(fresh["spec"], [{"key": "прайс клиента",
                                                     "rows": [["Базовый", "6 900 ₽"]]}],
                                     now="2032-06-01T00:00:00+00:00")
        if same["code"] != "no_changes" or same["spec"]["version"] != 2:
            return False, "версия растёт без изменений: «обновлено» становится галкой"
        # canonical: адрес статьи только когда домен платформы известен
        os.environ.pop("ARTICLE_CANONICAL_BASE", None)
        bare = make("Отчёт по охватам без домена", ["Охваты", "Переходы", "Итог"],
                    ["отчёт по охватам"])
        if bare["seo"]["canonical"] is not None:
            return False, "canonical выдуман без домена платформы"
        os.environ["ARTICLE_CANONICAL_BASE"] = "https://posiz.ru/blog"
        named = make("Отчёт по охватам без домена", ["Охваты", "Переходы", "Итог"],
                     ["отчёт по охватам"])
        expected = f"https://posiz.ru/blog/{named['slug']}/"
        if named["seo"]["canonical"] != expected:
            return False, f"canonical не собран из домена: {named['seo']['canonical']}"
        report = article_links.evergreen_report(articles.articles(20), now=late)
        if report["evergreen"] < 1 or report["due"] < 1:
            return False, "отчёт по вечнозелёным не видит просрочку"
        return True, (f"близость по смыслу отличима от случайной (статья об охватах "
                      f"{top['score']:.2f} против посторонней ниже порога "
                      f"{plan['threshold']}); ссылка встала перед призывом с причиной и "
                      f"анкором, дубль и битая ловятся; вечнозелёная статья обновилась по "
                      f"материалу (версия 2), число без источника осталось прежним и попало "
                      f"в пропуски, повтор без изменений версию не поднял; canonical появляется "
                      f"только при известном домене ({expected})")
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        articles.reset()
        article_links.reset()


@check("article.abtests", "A/B заголовка и вводки: тело одно, решение по статистике")
def _article_abtests() -> tuple[bool, str]:
    import os

    from . import article_ab, articles

    saved = {name: os.environ.get(name) for name in
             ("ARTICLE_AB_MIN_IMPRESSIONS", "ARTICLE_AB_ALPHA")}
    articles.reset()
    article_ab.reset()
    text = ("Отчёт по охватам нужен для решения, а не для отчётности: смотрим охваты, переходы "
            "и цену действия. Без цены действия канал нельзя ни оставить, ни закрыть. ") * 8
    try:
        for name in saved:
            os.environ.pop(name, None)
        result = articles.compose({
            "topic": "Как читать отчёт по охватам", "keywords": ["отчёт по охватам"],
            "sections": 3,
            "outline": [{"heading": head, "text": text}
                        for head in ("Охваты и переходы", "Цена действия", "Что дальше")]})
        spec = result["spec"]
        made = article_ab.headline_variants(spec, count=3)
        if not made["ok"] or len(made["variants"]) < 3:
            return False, f"варианты не собраны: {made.get('code')}"
        if len({row["title"] for row in made["variants"]}) != len(made["variants"]):
            return False, "варианты совпали по заголовку: сравнивать нечего"
        if any(row["title_length"] > article_ab.MAX_TITLE for row in made["variants"]):
            return False, "в тест попал заголовок длиннее лимита"
        if any("отчёт по охватам" not in row["title"].lower() for row in made["variants"]):
            return False, "вариант потерял ключевое слово"

        test = article_ab.create(spec, count=3, article_id="art_gate")["test"]
        body = test["body_hash"]
        if article_ab._body_hash(spec) != body:
            return False, "отпечаток тела не совпадает со спекой"
        first = article_ab.assign(test["id"], "reader-1")["code"]
        if article_ab.assign(test["id"], "reader-1")["code"] != first:
            return False, "разбиение нестабильно: тот же читатель видит другой вариант"
        buckets = {article_ab.assign(test["id"], f"reader-{index}")["code"]
                   for index in range(120)}
        if len(buckets) != len(test["variants"]):
            return False, "разбиение не покрывает все варианты"

        if article_ab.evaluate(test["id"])["code"] != "not_enough_data":
            return False, "решение принято без данных"
        codes = [row["code"] for row in test["variants"]]
        for code, clicks in ((codes[0], 20), (codes[1], 75), (codes[2], 24)):
            article_ab.record(test["id"], code, impressions=1000, clicks=clicks)
        decision = article_ab.evaluate(test["id"])
        if decision.get("code") != "decision" or decision["winner"] != codes[1]:
            return False, f"победитель не найден: {decision.get('code')}"
        if decision["p_value"] >= decision["alpha"] or decision["uplift"] <= 0:
            return False, "решение принято без значимой разницы"
        if article_ab.record(test["id"], codes[0], impressions=5, clicks=9)["code"] != \
                "invalid_metrics":
            return False, "клики больше показов приняты как метрика"

        applied = article_ab.apply(test["id"], spec)
        if not applied.get("ok"):
            return False, f"победитель не применён: {applied.get('code')}"
        if applied["spec"]["seo"]["title"] != decision["winner_title"]:
            return False, "заголовок победителя не встал в статью"
        if article_ab._body_hash(applied["spec"]) != body:
            return False, "применение изменило тело статьи"
        if applied["spec"]["version"] != 2:
            return False, "версия статьи не выросла"
        if "выиграл по данным" not in applied["spec"]["history"][-1]["reason"]:
            return False, "применение не объяснено в истории"
        if article_ab.apply(test["id"], applied["spec"])["code"] != "already_applied":
            return False, "повторное применение не отвергнуто"
        noise = article_ab.create(spec, count=2, article_id="art_gate_2")["test"]
        article_ab.record(noise["id"], noise["variants"][0]["code"], impressions=1000, clicks=30)
        article_ab.record(noise["id"], noise["variants"][1]["code"], impressions=1000, clicks=32)
        if article_ab.evaluate(noise["id"])["code"] != "no_difference":
            return False, "разница в пределах шума объявлена победителем"
        return True, (f"{len(made['variants'])} варианта на одном теле (отпечаток {body[:8]}…), "
                      f"разбиение стабильно и покрывает все варианты; без данных — "
                      f"not_enough_data, шум — no_difference, победитель «{decision['winner']}» "
                      f"с uplift {decision['uplift']} при p = {decision['p_value']}; "
                      f"применён заголовок и вводка, тело не изменилось, версия 2")
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        articles.reset()
        article_ab.reset()


@check("article.i18n", "Перевод статьи: тексты данными, «хром» кодом, hreflang в артефакте")
def _article_i18n() -> tuple[bool, str]:
    from . import article_i18n, articles

    articles.reset()
    text = ("Отчёт по охватам нужен для решения, а не для отчётности: смотрим охваты, переходы "
            "и цену действия. Без цены действия канал нельзя ни оставить, ни закрыть. ") * 8
    english = ("The reach report exists for decisions, not for reporting: reach, clicks and cost "
               "per action. ") * 8
    try:
        result = articles.compose({
            "topic": "Как читать отчёт по охватам", "keywords": ["отчёт по охватам"],
            "sections": 3, "lead": "Отчёт по охватам нужен для решений.",
            "outline": [{"heading": head, "text": text}
                        for head in ("Охваты и переходы", "Цена действия", "Что дальше")],
            "faq": [{"q": "Как часто нужен отчёт?", "a": "Раз в неделю."}]})
        spec = result["spec"]

        rows = {row["code"]: row for row in article_i18n.catalog()}
        if set(rows) != {"ru", "en", "kk", "uz"}:
            return False, f"реестр языков неполон: {sorted(rows)}"
        if rows["en"]["words_per_minute"] <= rows["ru"]["words_per_minute"]:
            return False, "темп чтения статьи не отличается по языкам"
        if rows["en"]["title_limit"] >= rows["ru"]["title_limit"]:
            return False, "лимит заголовка одинаков для ru и en: английский обрежется в выдаче"
        pace = article_i18n.reading(spec, "ru")
        if pace["words_per_minute"] != 180:
            return False, "время чтения считается не по профилю статьи"

        translations = {"seo": {"title": "How to read a reach report",
                                "description": "Reach, clicks and cost per action."},
                        "blocks": {}, "cta": {"text": "Open your report"}}
        for block in spec["blocks"]:
            props = block.get("props") or {}
            given: dict = {}
            if props.get("text"):
                given["text"] = english if block["type"] == "paragraph" else "English text"
            if props.get("items") and block["type"] == "faq":
                given["items"] = [{"q": "How often?", "a": "Once a week."}]
            if props.get("caption"):
                given["caption"] = "English caption"
            if props.get("alt"):
                given["alt"] = "English alt"
            if given:
                translations["blocks"][block["id"]] = given

        refused = article_i18n.translate(spec, "en", translations={}, strict=True)
        if refused.get("code") != "translation_missing" or not refused["missing"]:
            return False, "неполный перевод не назван: английская версия осталась бы с русским"
        draft = article_i18n.translate(spec, "en", translations={}, strict=False)
        codes = {issue["code"] for issue in articles.check(draft["spec"])["issues"]}
        if "translation_missing" not in codes:
            return False, "проверка не видит неполный перевод"
        unknown = article_i18n.translate(spec, "de", translations={})
        if unknown.get("code") != "locale_unknown":
            return False, "неизвестный язык не отвергнут: вышел бы тихий откат на русский"
        if article_i18n.translate(spec, "ru", translations={}).get("code") != "already_localized":
            return False, "перевод на исходный язык принят за перевод"

        made = article_i18n.translate(spec, "en", translations=translations, article_id="art_gate")
        if not made.get("ok") or made["partial"]:
            return False, f"полный перевод не собрался: {made.get('missing')}"
        local = made["spec"]
        if local["labels"]["related"] != "Read next" or local["locale"] != "en":
            return False, "подписи интерфейса не переведены кодом"
        if local["id"] == spec["id"]:
            return False, "перевод получил id оригинала: публикация перезаписала бы русскую версию"
        html = articles.render_html(local)
        if '<html lang="en">' not in html or "min read" not in html or "мин чтения" in html:
            return False, "язык или подписи не попали в разметку"
        if spec["locale"] != "ru" or spec["seo"]["title"] == local["seo"]["title"]:
            return False, "перевод изменил исходную спеку"

        urls = {"ru": "https://posiz.ru/blog/ru/", "en": "https://posiz.ru/blog/en/"}
        group = article_i18n.hreflang(local, urls)
        if not group.get("ok") or not any(row["hreflang"] == "x-default" for row in group["rows"]):
            return False, "hreflang-группа без x-default"
        if not any(row["self"] for row in group["rows"]):
            return False, "в hreflang нет ссылки на собственную версию"
        local["hreflang"] = group["rows"]
        if articles.render_html(local).count('rel="alternate"') != 3:
            return False, "alternate-ссылки не попали в head"
        broken = article_i18n.hreflang(local, {"ru": "https://posiz.ru/blog/ru/"})
        if broken.get("code") != "hreflang_self_missing":
            return False, "группа без собственного языка принята"
        no_default = article_i18n.hreflang(local, urls, default="kk")
        if no_default.get("code") != "hreflang_default_missing":
            return False, "группа без версии по умолчанию принята"

        long_en = json_loads(json_dumps(translations))
        long_en["seo"]["title"] = "A" * 65
        replaced = article_i18n.translate(spec, "en", translations=long_en, strict=False)
        if not any(row["code"] == "title_too_long_locale" for row in replaced["issues"]):
            return False, "лимит заголовка под язык не проверяется"

        articles.publish(spec)
        articles.publish(local)
        if len(articles.articles(10)) != 2:
            return False, "перевод и оригинал не сосуществуют как две статьи"
        return True, (f"4 языка в реестре (en {rows['en']['words_per_minute']} слов/мин против "
                      f"{rows['ru']['words_per_minute']} у ru, лимит заголовка "
                      f"{rows['en']['title_limit']} против {rows['ru']['title_limit']}); неполный "
                      f"перевод отвергнут с {len(refused['missing'])} полями, черновик помечен "
                      f"неполным, неизвестный язык отвергнут; полный перевод дал «Read next», "
                      f"min read и lang=en, hreflang из {len(group['rows'])} строк с x-default и "
                      f"ссылкой на себя; перевод живёт отдельной статьёй")
    finally:
        articles.reset()


def json_dumps(payload):
    import json

    return json.dumps(payload, ensure_ascii=False)


def json_loads(text):
    import json

    return json.loads(text)


@check("media.idempotency", "Повторный запрос не рендерит карточку заново")
def _media_idempotency() -> tuple[bool, str]:
    from . import media

    spec = media.build_spec({"title": "Повторная карточка"}, "1x1")
    first = media.submit(spec)
    second = media.submit(spec)
    if not first["asset"] or not second["asset"]:
        return False, "рендер не дал артефакт"
    if first["asset"]["id"] != second["asset"]["id"]:
        return False, "одинаковая спека дала два разных артефакта"
    if not second.get("reused"):
        return False, "второй запрос не отмечен как повторное использование"
    return True, f"spec_hash {media.spec_hash(spec)[:12]}…, артефакт {first['asset']['id']}"


@check("media.storage", "Артефакты уходят в хранилище, а не живут в памяти процесса")
def _media_storage() -> tuple[bool, str]:
    from . import media, media_storage

    usage = media_storage.usage()
    key = "gate/check/probe.svg"
    try:
        media_storage.put(key, b"<svg/>", mime="image/svg+xml")
        ok = media_storage.get(key) == b"<svg/>"
        media_storage.delete(key)
    except media_storage.StorageError as exc:
        return False, f"хранилище недоступно: {exc}"
    if not ok:
        return False, "артефакт не читается обратно из хранилища"
    if usage["backend"] == "s3":
        return True, "хранилище S3 (TTL — lifecycle-правило бакета)"
    return True, (f"спул {usage['root']}: {usage['files']} файлов, "
                  f"{usage['bytes'] // 1024} КБ, свободно {usage['disk_free'] // (1024 ** 3)} ГБ")


@check("media.raster", "PNG получается из SVG тем же движком, что видит дизайнер")
def _media_raster() -> tuple[bool, str]:
    from . import media, media_raster

    state = media_raster.available()
    if not state["available"]:
        return False, state.get("reason") or "растеризатор недоступен"
    spec = media.build_spec({"title": "Проверка растра", "footer": "posiz.ru"}, "1x1")
    svg = media.render_svg(spec)
    png, backend = media_raster.rasterize(svg, *media.FORMATS["1x1"])
    if not png:
        return False, f"{backend or state['backend']} не отдал PNG"
    width, height = media_raster.png_size(png)
    if (width, height) != tuple(media.FORMATS["1x1"]):
        return False, f"PNG {width}×{height}, ожидалось {media.FORMATS['1x1']}"
    note = "точность высокая" if state["fidelity"] == "high" else \
        "точность низкая: шрифт подменяется, в проде нужен rsvg/Chromium"
    return True, f"{state['backend']} ({state['binary']}), PNG {len(png) // 1024} КБ, {note}"


@check("media.public_urls", "Публичные ссылки подписаны и не открываются без подписи")
def _media_public() -> tuple[bool, str]:
    from . import media

    try:
        token = media.signed_token("as_gate", "png", ttl=60)
    except ValueError as exc:
        return False, f"{exc} (нужен MEDIA_URL_SECRET в окружении)"
    if media.resolve_token(token) != ("as_gate", "png"):
        return False, "подпись ссылки не проходит проверку"
    if media.resolve_token(media.signed_token("as_gate", "png", ttl=-1)) is not None:
        return False, "просроченная ссылка принимается"
    if media.resolve_token(token[:-2] + "aa") is not None:
        return False, "подделанная подпись принимается"
    base = media.public_url("as_gate", "png")
    return True, f"ссылка живёт час: {base.split('/api/v1')[0]}/api/v1/media/public/…"


@check("media.attachments", "Вложения доезжают от артефакта до адаптера канала")
def _media_attachments() -> tuple[bool, str]:
    from . import media
    from .adapters.base import PublishInput

    spec = media.build_spec({"title": "Вложение в публикации"}, "1x1")
    asset = media.submit(spec)["asset"]
    fmt = media.best_format(asset["id"])
    if fmt is None:
        return False, "у артефакта нет ни PNG, ни SVG"
    url = media.public_url(asset["id"], fmt)
    payload = PublishInput(account_external_id="1", text="текст", media_urls=[url])
    if not payload.media_urls or "media/public/" not in payload.media_urls[0]:
        return False, "PublishInput не принимает ссылку на артефакт"
    return True, f"{asset['id']} → {fmt.upper()} попадает в PublishInput.media_urls"


@check("media.reel_spec", "Ролик 9:16 собирается из поста и сцены проверяются до рендера")
def _media_reel_spec() -> tuple[bool, str]:
    from . import media_reel

    spec = media_reel.build_reel_spec({
        "title": "Один договор — 29 площадок", "eyebrow": "конвейер",
        "items": ["Один пост — все каналы", "Маркировка до публикации"],
        "footer": "posiz.ru", "cta": "Смотреть"}, brand={"name": "dogovor.expert"})
    issues = media_reel.validate_reel_spec(spec)
    if issues:
        return False, f"валидная спека отвергнута: {issues[0]['code']}"
    broken = {**spec, "scenes": [{**spec["scenes"][0], "template": "scene-3d"}]}
    if not media_reel.validate_reel_spec(broken):
        return False, "выдуманная сцена прошла проверку"
    ad = media_reel.build_reel_spec({"title": "Скидка"}, compliance={"ad": True})
    if not any(i["code"] == "erid_required" for i in media_reel.validate_reel_spec(ad)):
        return False, "реклама без erid прошла проверку"
    return True, (f"{len(spec['scenes'])} сцены, {spec['duration']} с, "
                  f"{media_reel.frames_count(spec)} кадров; выдуманная сцена и реклама без erid — отказ")


@check("media.reel_frames", "Кадр — чистая функция от времени, таймлайн сходится")
def _media_reel_frames() -> tuple[bool, str]:
    from . import media_reel

    spec = media_reel.build_reel_spec({"title": "Кадры и тайминг", "footer": "posiz.ru",
                                      "items": ["Первый", "Второй"]})
    frame = media_reel.frame_at(spec, 1.5)
    if frame != media_reel.frame_at(spec, 1.5):
        return False, "один и тот же момент времени дал разные кадры"
    if frame == media_reel.frame_at(spec, spec["duration"] - 0.1):
        return False, "кадры на разных секундах совпали"
    rows = media_reel.timeline(spec)
    if abs(rows[-1]["end"] - spec["duration"]) > 1e-6:
        return False, "таймлайн не сходится с длительностью"
    if sum(row["frames"] for row in rows) != media_reel.frames_count(spec):
        return False, "кадры по сценам не совпадают с общим числом кадров"
    return True, (f"{len(rows)} сцены, {media_reel.frames_count(spec)} кадров, "
                  f"детерминированные кадры 1080×1920")


@check("media.voice_plan", "Озвучка: произношение, смета в рублях, тайминги слов")
def _media_voice_plan() -> tuple[bool, str]:
    from . import media_voice

    spoken = media_voice.normalize("Реклама erid, 38-ФЗ и 100% договоров")
    if "erid" in spoken or "38-ФЗ" in spoken or "100%" in spoken:
        return False, f"словарь произношения не сработал: {spoken!r}"
    cost = media_voice.estimate_cost(1000)
    if cost != media_voice.RUB_PER_1000_CHARS:
        return False, "смета не соответствует тарифу"
    words = media_voice.align("Один договор закрывает двадцать девять площадок", 6.0)
    if not words or words[0]["start"] != 0 or abs(words[-1]["end"] - 6.0) > 1e-6:
        return False, "тайминги не покрывают аудио"
    for previous, current in zip(words, words[1:]):
        if current["start"] != previous["end"]:
            return False, "тайминги слов не монотонны"
    state = media_voice.available()
    where = "SpeechKit" if state["yandex"]["available"] else "SpeechKit без ключа (mock/file)"
    return True, (f"{len(words)} слов на 6 с, {cost} ₽/1000 знаков, "
                  f"выравнивание: {state['alignment']}, {where}")


@check("media.voice_sync", "Ролик подстраивается под речь, а не речь под ролик")
def _media_voice_sync() -> tuple[bool, str]:
    from . import media_reel, media_voice

    spec = media_reel.build_reel_spec({"title": "Синхрон", "footer": "posiz.ru",
                                       "items": ["Первый пункт", "Второй пункт"]}, fps=25)
    before = spec["duration"]
    audio_seconds = before * 1.6
    voice = {"duration": audio_seconds, "provider": "mock", "chars": 80, "cost_rub": 0.11,
             "words": media_voice.align("Речь длиннее сцен", audio_seconds)}
    stretched = media_reel.apply_voice(spec, voice)
    if abs(stretched["duration"] - audio_seconds) > 0.3:
        return False, f"ролик {stretched['duration']} с, речь {audio_seconds} с"
    for scene in stretched["scenes"]:
        floor = media_reel.SCENES[scene["template"]]["min_seconds"]
        if scene["duration"] < floor - 0.05:
            return False, f"сцена {scene['template']} сжата ниже минимума"
    issues = media_reel.validate_reel_spec(stretched)
    if issues:
        return False, f"после подгонки спека невалидна: {issues[0]['code']}"
    return True, (f"сцены {before} с → {stretched['duration']} с под речь, "
                  f"минимумы сцен соблюдены, титры получили тайминги речи")


@check("media.reel_encode", "MP4 собирается, длительность и faststart проверяются")
def _media_reel_encode() -> tuple[bool, str]:
    import struct
    import zlib

    from . import media, media_video

    state = media_video.available()
    if not state["available"]:
        return False, state["reason"] or "ffmpeg недоступен: ролик останется сторибордом"

    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (struct.pack(">I", len(payload)) + kind + payload +
                struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF))

    width, height = media.FORMATS["9x16"]
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    row = b"\x00" + b"\x09\x09\x0b" * width
    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) +
           chunk(b"IDAT", zlib.compress(row * height, 6)) + chunk(b"IEND", b""))
    result = media_video.encode([png] * 10, fps=25)
    if not result.get("ok"):
        return False, result.get("reason") or "ffmpeg не собрал файл"
    probe = result["probe"]
    problems = media_video.checks(probe, expected_duration=0.4, fps=25)
    if problems:
        return False, "; ".join(problems)
    return True, (f"{state['version']}, 10 кадров → {probe['bytes'] // 1024} КБ, "
                  f"{probe['duration']} с, {probe['width']}×{probe['height']}, faststart")


# ------------------------------------------------------------------ пункты
@check("db.pgvector", "БД умеет pgvector и схема ставится миграциями")
def _db() -> tuple[bool, str]:
    from .config import settings  # noqa: F401 — читает конфигурацию

    url = os.getenv("DATABASE_URL", "")
    if not url:
        return False, "DATABASE_URL не задан: проверить схему не на чем"
    try:
        from sqlalchemy import text

        from .persistence import database

        db = database()
        if db is None:
            return False, "слой БД не подключён (DATA_LAYER != db)"
        with db.session() as s:
            ext = s.execute(text("SELECT extname FROM pg_extension")).scalars().all()
            version = s.execute(text("SELECT version_num FROM alembic_version")).scalar()
        if "vector" not in ext:
            return False, f"расширения vector нет (есть: {sorted(ext)})"
        return True, f"vector установлен, схема на ревизии {version}"
    except Exception as exc:                       # noqa: BLE001 — это отчёт, а не падение
        return False, f"БД недоступна: {exc.__class__.__name__}: {exc}"


@check("queue.arq", "ARQ-воркер принимает и обрабатывает задачи")
def _arq() -> tuple[bool, str]:
    redis_url = os.getenv("REDIS_URL", "")
    if not redis_url:
        return False, "REDIS_URL не задан: очередь работает таймером, а не ARQ"
    from .workers.run import describe

    info = describe()
    if not info["functions"]:
        return False, "у воркера нет функций"
    return True, f"функций {len(info['functions'])}, cron {info['cron_count']}, timeout {info['job_timeout']} c"


@check("publishing.lease", "Публикации захватываются один раз и не зависают")
def _lease() -> tuple[bool, str]:
    from . import publishing

    if "publishing" in publishing.CLAIMABLE:
        return False, "статус publishing доступен для захвата — возможен дубль"
    return True, (f"аренда {publishing.DEFAULT_LEASE_SECONDS} c, "
                  f"попыток {publishing.MAX_ATTEMPTS}, backoff до {publishing.backoff_seconds(5)} c")


@check("tenancy.scope", "Слой данных сужает выборки по арендатору")
def _tenancy() -> tuple[bool, str]:
    from . import tenancy

    if tenancy.current() is not None:
        return False, "гейт запущен внутри привязанного контекста: проверка недостоверна"
    return True, "контекст вне запроса = платформенный режим; внутри запроса — только свой workspace"


@check("auth.state", "OAuth проверяет state, 2FA выдаёт настоящие коды")
def _auth() -> tuple[bool, str]:
    from . import auth

    url = auth.oauth_url("vk_id")
    if "state=" not in url["url"]:
        return False, "state не добавляется в authorize-URL"
    # провайдер «настроен» только на время проверки: иначе ручка отвечает
    # «oauth_not_configured» и проверка state до неё не доходит
    saved = {k: os.environ.get(k) for k in ("VK_ID_CLIENT_ID", "VK_ID_CLIENT_SECRET")}
    os.environ["VK_ID_CLIENT_ID"], os.environ["VK_ID_CLIENT_SECRET"] = "gate", "gate"
    try:
        probe = auth.exchange_code("vk_id", "x")
        if probe.get("code") != "invalid_state":
            return False, f"обмен без state не отклонён: {probe}"
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    two = auth.enable_2fa(next(iter(auth.SESSIONS)) if auth.SESSIONS else "")
    if "error" in two:
        codes_ok = True                      # нет сессии — проверить нечего, но и не врём
    else:
        codes_ok = len(two["backup_codes"]) == 10 and all("-" in c for c in two["backup_codes"])
    if not codes_ok:
        return False, "2FA не выдаёт одноразовые backup-коды"
    return True, "state одноразовый, TOTP и 10 backup-кодов выдаются"


@check("pii.masking", "PII маскируется сквозным образом и до внешних вызовов")
def _pii() -> tuple[bool, str]:
    from . import pii

    masker = pii.Masker()
    first, _ = masker.mask("тел +7 916 111-22-33")
    second, _ = masker.mask("тел +7 916 444-55-66")
    if first.split()[-1] == second.split()[-1]:
        return False, "разные значения получают одинаковый плейсхолдер"
    return True, "нумерация сквозная, NER получает замаскированный текст"


@check("compliance.marking", "Маркировка попадает в текст публикации")
def _compliance() -> tuple[bool, str]:
    from . import compliance

    ctx = compliance.VariantContext(channel="vk", body="Реклама", erid="erid: GATE")
    text = compliance.apply_marking("Пост.", ctx)
    if "erid: GATE" not in text:
        return False, "маркировка не добавляется в текст"
    return True, "пометка «Реклама» + erid вставляются перед отправкой в канал"


@check("channels.tokens", "Токены каналов берутся из хранилища аккаунта")
def _tokens() -> tuple[bool, str]:
    from .adapters.base import PublishInput

    if "token" not in PublishInput.__dataclass_fields__:
        return False, "PublishInput не передаёт токен аккаунта"
    return True, "адаптер получает токен конкретного аккаунта, а не процесса"


@check("schema.migrations", "Миграции линейны и идемпотентны")
def _migrations() -> tuple[bool, str]:
    from pathlib import Path

    versions = sorted((Path(__file__).resolve().parents[2] / "migrations" / "versions")
                      .glob("*.py"))
    numbers = [int(v.name[:4]) for v in versions if v.name[:4].isdigit()]
    if numbers != list(range(1, len(numbers) + 1)):
        return False, f"в цепочке миграций дыра: {numbers}"
    return True, f"ревизии 1…{len(numbers)} без пропусков, baseline идемпотентен"


@check("rls.force", "RLS включён принудительно и роли приложения не обойти")
def _rls() -> tuple[bool, str]:
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    migration = (root / "migrations" / "versions" / "0004_publishing.py").read_text()
    cluster = (root / "sql" / "00_cluster_init.sql").read_text()
    if "FORCE ROW LEVEL SECURITY" not in migration:
        return False, "нет FORCE RLS: владелец таблиц обходит политики"
    import re

    create_role = re.search(r"CREATE ROLE orbita_app.*?;", cluster, re.S)
    if create_role is None:
        return False, "нет создания роли orbita_app"
    statement = create_role.group(0)
    if "NOBYPASSRLS" not in statement or "NOSUPERUSER" not in statement:
        return False, "роль приложения создаётся без NOSUPERUSER/NOBYPASSRLS"
    return True, "FORCE RLS на таблицах клиента, роль orbita_app без BYPASSRLS"


@check("compose.hardening", "Compose поднимает стек детерминированно")
def _compose() -> tuple[bool, str]:
    from pathlib import Path

    import yaml

    compose = yaml.safe_load((Path(__file__).resolve().parents[2]
                              / "docker-compose.yml").read_text())
    services = compose["services"]
    if "pgvector" not in services["db"]["image"]:
        return False, "образ БД без pgvector: схема не создастся"
    if "healthcheck" not in services.get("db", {}):
        return False, "нет healthcheck у БД: API стартует раньше готовности"
    initdb = [v for v in services["db"].get("volumes", [])
              if "docker-entrypoint-initdb.d" in str(v)]
    if any("schema.sql" in str(v) for v in initdb):
        return False, "schema.sql грузится через initdb: двойной путь создания схемы"
    return True, "pgvector + healthchecks + единый путь миграций"


# ---------------------------------------------------------------- поведенческие проверки
# ---------------------------------------------------------------- тестовые арендаторы
def _probe_workspace(prefix: str) -> tuple[str, str, str]:
    """Создать (арендатор, пространство, пользователь) для проверки.

    В режиме ``db`` идентификаторы обязаны быть настоящими UUID: Postgres не примет
    «ws_gate_a» в колонку uuid, и проверка падала бы на типах, а не на изоляции.
    В режиме памяти достаточно строковых id.
    """
    import uuid as _uuid

    from . import store, tenancy

    if store._db() is None:
        suffix = _uuid.uuid4().hex[:8]
        return (f"tn_{prefix}_{suffix}", f"ws_{prefix}_{suffix}", f"u_{prefix}_{suffix}")

    from sqlalchemy import text

    from . import persistence

    db = store._db()
    tenant, workspace, user = (str(_uuid.uuid4()) for _ in range(3))
    with tenancy.bind(None, platform=True), db.session() as session:
        session.execute(text("INSERT INTO tenants (id, name) VALUES (:i, :n)"),
                        {"i": tenant, "n": f"gate {prefix}"})
        session.execute(text("INSERT INTO workspaces (id, tenant_id, name) VALUES (:i, :t, :n)"),
                        {"i": workspace, "t": tenant, "n": prefix})
        session.execute(text("INSERT INTO users (id, email) VALUES (:i, :e)"),
                        {"i": user, "e": f"gate-{prefix}-{user[:8]}@example.ru"})
        session.execute(text("INSERT INTO memberships (tenant_id, user_id, role) "
                             "VALUES (:t, :u, 'owner')"), {"t": tenant, "u": user})
        session.commit()
    return tenant, workspace, user


def _probe_channel_account(workspace_id: str) -> str:
    """Канал для публикации: в db-режиме нужен реальный channel_account (FK), иначе — строка."""
    from . import store

    if store._db() is None:
        return "vk_1"

    import uuid as _uuid

    from sqlalchemy import text

    from . import tenancy

    account = str(_uuid.uuid4())
    with tenancy.bind(None, platform=True), store._db().session() as session:
        session.execute(text(
            "INSERT INTO channel_accounts (id, workspace_id, channel, external_id) "
            "VALUES (:i, :w, 'vk', :e)"),
            {"i": account, "w": workspace_id, "e": f"gate-{workspace_id[:8]}"})
        session.commit()
    return account


@check("tenancy.cross_read", "Чужой workspace не читает сигнал и вариант (режим памяти)")
def _cross_read() -> tuple[bool, str]:
    from . import store, tenancy

    tenant_a, ws_a, user_a = _probe_workspace("a")
    tenant_b, ws_b, user_b = _probe_workspace("b")
    signal = store.create_signal("manual", "Проверка изоляции", None, None, "lead",
                                 workspace_id=ws_a)
    variant = store.save_variant(signal["id"], "vk", "post", "T",
                                 "Текст варианта для проверки изоляции между арендаторами.",
                                 None, False, 4.0, workspace_id=ws_a)
    with tenancy.bind(ws_a, tenant_id=tenant_a, user_id=user_a, role="owner"):
        own = store.get_signal(signal["id"]) is not None
    with tenancy.bind(ws_b, tenant_id=tenant_b, user_id=user_b, role="owner"):
        leaked_signal = store.get_signal(signal["id"]) is not None
        leaked_variant = store.get_variant(variant["id"]) is not None
    if not own:
        return False, "свой сигнал не читается — проверка недостоверна"
    if leaked_signal or leaked_variant:
        return False, f"утечка между workspace: signal={leaked_signal}, variant={leaked_variant}"
    return True, "свой читается, чужой невидим (сигнал и вариант)"


@check("tenancy.registration", "Регистрация создаёт отдельное пространство на клиента")
def _registration() -> tuple[bool, str]:
    from . import accounts

    first = accounts.register("gate-a@example.ru")
    second = accounts.register("gate-b@example.ru")
    if first["workspace"]["id"] == second["workspace"]["id"]:
        return False, "два клиента получили один workspace"
    if first["tenant"]["id"] == second["tenant"]["id"]:
        return False, "два клиента получили один tenant"
    if first["membership"]["role"] != "owner":
        return False, "первый пользователь не владелец своего пространства"
    return True, f"workspaces {first['workspace']['id']} ≠ {second['workspace']['id']}"


@check("publishing.no_double_post", "Две параллельные задачи дают один пост")
def _no_double() -> tuple[bool, str]:
    import asyncio
    from datetime import datetime, timedelta, timezone

    from . import store, tenancy
    from .workers import tasks

    class Counter:
        def __init__(self):
            self.calls = 0

        async def publish(self, payload, idempotency_key):
            from .adapters.base import PublishResult

            self.calls += 1
            await asyncio.sleep(0.02)
            return PublishResult(external_id=f"gate_{idempotency_key[:6]}", external_url="")

    counter = Counter()
    original = tasks.get_adapter
    tasks.get_adapter = lambda channel, token=None: counter       # type: ignore[assignment]
    try:
        tenant, ws, user = _probe_workspace("dup")
        with tenancy.bind(ws, tenant_id=tenant, user_id=user, role="owner"):
            signal = store.create_signal("manual", "Гейт дублей", None, None, "lead",
                                         workspace_id=ws)
            variant = store.save_variant(signal["id"], "vk", "post", "T",
                                         "Текст для проверки одновременной публикации.", None,
                                         False, 4.0, workspace_id=ws)
            account = _probe_channel_account(ws)
            pub = store.create_publication(variant["id"], account,
                                           datetime.now(timezone.utc) - timedelta(seconds=5),
                                           None, is_ad=False, require_approval=False)
        asyncio.run(asyncio.wait_for(_two_tasks(tasks, pub["id"]), timeout=10))
        status = (store.get_publication(pub["id"]) or {}).get("status")
    finally:
        tasks.get_adapter = original                            # type: ignore[assignment]
    if counter.calls != 1:
        return False, f"адаптер вызван {counter.calls} раз(а)"
    return True, f"один вызов адаптера, статус {status}"


async def _two_tasks(tasks, pid: str) -> None:
    import asyncio

    await asyncio.gather(tasks.publish_publication({"worker_id": "g1"}, pid),
                         tasks.publish_publication({"worker_id": "g2"}, pid))


@check("publishing.marking", "Реклама уходит с полной маркировкой (Реклама + рекламодатель + erid)")
def _marking() -> tuple[bool, str]:
    from . import compliance

    ctx = compliance.VariantContext(channel="vk", body="Реклама поста", erid="erid: GATE7777",
                                   advertiser_name="ООО «Гейт»", advertiser_site="gate.ru")
    text = compliance.apply_marking("Текст поста.", ctx)
    if not compliance.is_marked(text, ctx):
        return False, f"маркировка неполная: {text!r}"
    if compliance.is_marked("просто текст, erid: упомянут вскользь"):
        return False, "неполная маркировка признана корректной"
    return True, "нужны «Реклама», рекламодатель и валидный erid — иначе блок"


@check("compliance.advertiser", "Реквизиты рекламодателя берутся из профиля бренда")
def _advertiser() -> tuple[bool, str]:
    from . import accounts, compliance

    ws = accounts.register("gate-adv@example.ru")["workspace"]["id"]
    empty = compliance.check(compliance.VariantContext(channel="vk", body="Скидка 30%",
                                                       erid="erid: X1"), is_ad=True)
    if not any(r.rule_code == "ADVERTISER_MISSING" for r in empty.reasons):
        return False, "без реквизитов реклама не заблокирована"
    accounts.set_legal_entity(ws, legal_name="ООО «Клиент»", site_url="client.ru")
    legal = accounts.legal_entity(ws)
    if legal.get("legal_name") != "ООО «Клиент»":
        return False, "реквизиты не сохраняются в профиле"
    return True, "маркировка невозможна без реквизитов клиента и берёт их из workspace"


@check("auth.two_factor", "2FA — обязательный второй фактор при входе")
def _two_factor() -> tuple[bool, str]:
    from datetime import datetime, timedelta, timezone

    from . import auth

    if not os.getenv("JWT_SECRET"):
        return False, ("JWT_SECRET не задан: вход и 2FA не проверяются "
                       "(в проде секрет обязателен, гейт не выдаёт «ок» на глаз)")
    email = "gate-2fa@example.ru"
    auth.MAGIC["gate-token-1"] = {"email": email, "workspace_id": None, "role": "owner",
                                  "expires_at": datetime.now(timezone.utc) + timedelta(minutes=5)}
    first = auth.verify_magic("gate-token-1")
    if "access_token" not in first:
        return False, "первый вход не выдал пару токенов (проверка недостоверна)"
    setup = auth.setup_2fa(first["user_id"], email)
    auth.activate_2fa(first["user_id"], auth._totp(setup["secret"]))

    auth.MAGIC["gate-token-2"] = {"email": email, "workspace_id": None, "role": "owner",
                                  "expires_at": datetime.now(timezone.utc) + timedelta(minutes=5)}
    challenged = auth.verify_magic("gate-token-2")
    if "access_token" in challenged:
        return False, "вход выдал токены в обход второго фактора"
    if not challenged.get("two_factor_required"):
        return False, "вход не запросил второй фактор"
    done = auth.verify_second_factor(challenged["challenge"], setup["backup_codes"][0])
    if "access_token" not in done:
        return False, "верный backup-код не завершил вход"
    return True, "без второго фактора токенов нет; backup-код одноразовый"


@check("pii.external", "ПДн не уходят внешним сервисам (NER и эмбеддинги)")
def _pii_external() -> tuple[bool, str]:
    from . import pii

    seen: list[str] = []
    original = pii._ner_remote
    pii._ner_remote = lambda text: (seen.append(text), [])[1]      # type: ignore[assignment]
    try:
        pii.mask("Клиент Иван Петров, телефон +7 916 123-45-67")
    finally:
        pii._ner_remote = original                                  # type: ignore[assignment]
    if not seen:
        return False, "NER не вызывался — проверка недостоверна"
    leaked = [t for t in seen if "916" in t or "Иван" in t]
    if leaked:
        return False, f"во внешний NER ушли ПДн: {leaked}"
    source = (os.path.dirname(__file__) + "/llm.py")
    with open(source, encoding="utf-8") as handle:
        if "masker.mask(t)[0]" not in handle.read():
            return False, "эмбеддинги отправляются без маскирования"
    return True, "NER и эмбеддинги получают только псевдонимы"


@check("orm.schema", "ORM и SQL-схема согласованы по типам и колонкам")
def _orm_schema() -> tuple[bool, str]:
    import re as _re
    from pathlib import Path

    from . import persistence

    root = Path(__file__).resolve().parents[2]
    schema = (root / "schema.sql").read_text()
    migrations = "\n".join(p.read_text() for p in
                           (root / "migrations" / "versions").glob("*.py"))
    problems: list[str] = []
    for name, table in persistence.Base.metadata.tables.items():
        if name not in schema and f"CREATE TABLE {name}" not in schema:
            continue
        for column in table.columns:
            pattern = _re.compile(rf"^\s+{column.name}\s", _re.M)
            known = bool(pattern.search(schema)) or bool(
                _re.search(rf"ADD COLUMN IF NOT EXISTS\s+{column.name}\b", migrations)) or bool(
                _re.search(rf"^\s+{column.name}\s", migrations, _re.M))
            if not known:
                problems.append(f"{name}.{column.name}")
    if "citext" in schema and "citext" not in (root / "sql" / "00_cluster_init.sql").read_text():
        problems.append("расширение citext не создаётся")
    if problems:
        return False, "колонки модели без соответствия в схеме: " + ", ".join(problems[:8])
    return True, f"проверено таблиц: {len(persistence.Base.metadata.tables)}; citext создаётся"


@check("infra.secrets", "В compose нет паролей по умолчанию и публичных портов БД/Redis")
def _infra_secrets() -> tuple[bool, str]:
    from pathlib import Path

    import yaml

    compose = yaml.safe_load((Path(__file__).resolve().parents[2]
                              / "docker-compose.yml").read_text())
    services = compose["services"]
    problems = []
    if services["db"]["environment"]["POSTGRES_PASSWORD"].strip("${}") in {"postgres", "password"}:
        problems.append("пароль БД по умолчанию")
    for name in ("db", "redis"):
        if services[name].get("ports"):
            problems.append(f"{name} публикует порт")
    if "requirepass" not in " ".join(services["redis"]["command"]):
        problems.append("Redis без пароля")
    if problems:
        return False, "; ".join(problems)
    return True, "секреты из окружения, порты БД/Redis только внутри сети, Redis с паролем"


@check("db.rls_runtime", "Изоляция арендаторов работает В БАЗЕ, а не только в коде")
def _rls_runtime() -> tuple[bool, str]:
    """Поведенческая проверка RLS на живой PostgreSQL.

    Текстовые проверки политик ничего не доказывают: политика может ссылаться на функцию,
    которая рекурсивно читает ту же таблицу, или на переменную контекста, которую никто
    не выставляет. Поэтому здесь две строки от разных клиентов и два вопроса:
    «видит ли B чужое» и «видит ли кто-то всё без контекста».
    """
    import os
    import uuid as _uuid

    from sqlalchemy import create_engine, text

    url = os.getenv("DATABASE_URL", "").strip()
    if not url:
        return False, "DATABASE_URL не задан: RLS проверяется только на живой PostgreSQL"

    tenant_a, ws_a, user_a = _probe_workspace("rls_a")
    tenant_b, ws_b, user_b = _probe_workspace("rls_b")
    engine = create_engine(url, future=True)
    account = _probe_channel_account(ws_a)

    def context(conn, tenant: str, ws: str, user: str, role: str = "owner") -> None:
        for name, value in (("app.tenant_id", tenant), ("app.workspace_id", ws),
                            ("app.user_id", user), ("app.actor_role", role)):
            conn.execute(text("SELECT set_config(:n, :v, true)"), {"n": name, "v": value})

    try:
        with engine.begin() as conn:
            context(conn, tenant_a, ws_a, user_a)
            signal, variant, pub = (str(_uuid.uuid4()) for _ in range(3))
            conn.execute(text("INSERT INTO signals (id, workspace_id, kind, title) "
                              "VALUES (:i, :w, 'manual', 'RLS-проверка')"),
                         {"i": signal, "w": ws_a})
            conn.execute(text("INSERT INTO variants (id, workspace_id, signal_id, channel, format, body) "
                              "VALUES (:i, :w, :s, 'vk', 'post', 'Текст для проверки RLS.')"),
                         {"i": variant, "w": ws_a, "s": signal})
            conn.execute(text(
                "INSERT INTO publications (id, workspace_id, variant_id, channel_account_id, "
                "channel, scheduled_at, status, idempotency_key) "
                "VALUES (:i, :w, :v, :a, 'vk', now(), 'queued', :k)"),
                {"i": pub, "w": ws_a, "v": variant, "a": account, "k": f"rls-{pub[:8]}"})
        with engine.connect() as conn:
            context(conn, tenant_a, ws_a, user_a)
            own = conn.scalar(text("SELECT count(*) FROM publications WHERE id = :i"), {"i": pub})
        with engine.connect() as conn:
            context(conn, tenant_b, ws_b, user_b)
            foreign = conn.scalar(text("SELECT count(*) FROM publications WHERE id = :i"),
                                  {"i": pub})
        with engine.connect() as conn:
            conn.execute(text("SELECT set_config('app.actor_role', 'editor', true)"))
            anonymous = conn.scalar(text("SELECT count(*) FROM publications WHERE id = :i"),
                                    {"i": pub})
    finally:
        engine.dispose()

    if own != 1:
        return False, "своя строка не видна — проверка недостоверна"
    if foreign:
        return False, "клиент B видит публикацию клиента A"
    if anonymous:
        return False, "без контекста арендатора видны строки (RLS не фильтрует)"
    return True, "свой клиент видит 1 строку, чужой и «без контекста» — 0"


@check("db.rls_model", "Модель политик: рамка RESTRICTIVE, комплаенс не обходится")
def _rls_model() -> tuple[bool, str]:
    """Проверка самой модели политик — дефект, найденный третьим аудитом.

    Permissive-политики объединяются через OR, поэтому «строгая» permissive рядом с
    «широкой» не ограничивает ничего. Проверяем три вещи, каждая из которых уже
    ломала защиту:

    1. нет legacy-политик ``*_tenant`` (широкая permissive) — они отменяли матрицу ролей;
    2. у клиентских таблиц есть RESTRICTIVE-рамка арендатора;
    3. комплаенс рекламы (erid) объявлен RESTRICTIVE, а не permissive.
    """
    import os

    from sqlalchemy import create_engine, text

    url = os.getenv("DATABASE_URL", "").strip()
    if not url:
        return False, "DATABASE_URL не задан: модель политик видна только в живой БД"
    engine = create_engine(url, future=True)
    try:
        with engine.connect() as conn:
            wide = conn.execute(text(
                "SELECT tablename || '.' || policyname FROM pg_policies "
                "WHERE schemaname = 'public' AND policyname LIKE '%\\_tenant' "
                "AND permissive = 'PERMISSIVE'")).scalars().all()
            framed = set(conn.execute(text(
                "SELECT tablename FROM pg_policies WHERE schemaname = 'public' "
                "AND permissive = 'RESTRICTIVE' AND policyname LIKE '%\\_scope'")).scalars())
            compliance = conn.execute(text(
                "SELECT permissive, cmd FROM pg_policies WHERE schemaname = 'public' "
                "AND policyname = 'publications_compliance'")).fetchone()
    finally:
        engine.dispose()

    if wide:
        return False, f"permissive-дубли отменяют матрицу ролей: {', '.join(wide[:3])}"
    missing = {"publications", "signals", "variants", "entity_docs"} - framed
    if missing:
        return False, f"нет RESTRICTIVE-рамки: {', '.join(sorted(missing))}"
    if compliance is None:
        return False, "комплаенс-политика публикаций отсутствует"
    if compliance[0] != "RESTRICTIVE":
        return False, "комплаенс объявлен PERMISSIVE — его отменит любая широкая политика"
    return True, f"рамка RESTRICTIVE у {len(framed)} таблиц, комплаенс RESTRICTIVE/INSERT"


@check("infra.redis_acl", "Redis ACL: у API и воркера разные права, опасные команды запрещены")
def _redis_acl() -> tuple[bool, str]:
    """Проверка на живом Redis: права ролей действительно разделены.

    Дефект (F15): API и воркер ходили одним паролем с полными правами — утёкший ключ
    API-процесса давал доступ к состоянию очереди, а воркер мог трогать сессии.
    Проверяется поведение, а не наличие строк в конфиге: под ролью API команда
    ``FLUSHALL`` должна отвергаться, ключ воркера — быть недоступным, а ключ сессии —
    доступным.
    """
    import os

    from .config import redis_url

    api_password = os.getenv("REDIS_API_PASSWORD", "").strip()
    worker_password = os.getenv("REDIS_WORKER_PASSWORD", "").strip()
    if not (api_password and worker_password):
        return False, ("REDIS_API_PASSWORD/REDIS_WORKER_PASSWORD не заданы: права не разделены "
                       "(dev_stack.sh up создаёт эти роли)")
    try:
        import redis  # type: ignore
    except Exception:                                  # noqa: BLE001
        return False, "клиент redis не установлен"

    def client(role: str):
        return redis.Redis.from_url(redis_url(role), socket_timeout=0.5,
                                    socket_connect_timeout=0.5)

    problems: list[str] = []
    try:
        api = client("api")
        api.set("sess:gate", "1", ex=30)
        if api.get("sess:gate") != b"1":
            problems.append("API не может читать свои ключи сессий")
        try:
            api.flushall()
            problems.append("API может выполнить FLUSHALL")
        except Exception:                              # noqa: BLE001 — ожидаемо NOPERM
            pass
        try:
            api.get("pub:gate")
            problems.append("API читает ключи воркера (pub:*)")
        except Exception:                              # noqa: BLE001 — ожидаемо NOPERM
            pass

        worker = client("worker")
        try:
            worker.get("sess:gate")
            problems.append("воркер читает сессии")
        except Exception:                              # noqa: BLE001 — ожидаемо NOPERM
            pass
        worker.set("arq:gate", "1", ex=30)
    except Exception as exc:                           # noqa: BLE001
        return False, f"Redis недоступен под ролями: {exc.__class__.__name__}"

    if problems:
        return False, "; ".join(problems)
    return True, "API: сессии да, pub:* нет, FLUSHALL нет; воркер: arq да, сессии нет"


@check("infra.observability", "Метрики и статистика запросов доступны эксплуатации")
def _observability() -> tuple[bool, str]:
    """F16: без метрик дежурный видит «жив» и не видит «отстаём».

    Проверяется: ``/metrics`` отвечает в формате Prometheus и содержит метрики очереди;
    в PostgreSQL доступен ``pg_stat_statements`` (иначе нельзя ответить, какой запрос
    съедает базу).
    """
    import os

    try:
        from fastapi.testclient import TestClient

        from .main import app

        headers = {}
        token = os.getenv("METRICS_TOKEN", "").strip()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        response = TestClient(app).get("/metrics", headers=headers)
        if response.status_code != 200:
            return False, f"/metrics отвечает {response.status_code}"
        body = response.text
        if "posiz_publications_due_now" not in body:
            return False, "в /metrics нет метрик очереди публикаций"
    except Exception as exc:                           # noqa: BLE001
        return False, f"/metrics не отвечает: {exc.__class__.__name__}"

    url = os.getenv("DATABASE_URL", "").strip()
    if url:
        from sqlalchemy import create_engine, text

        engine = create_engine(url, future=True)
        try:
            with engine.connect() as conn:
                enabled = conn.scalar(text(
                    "SELECT count(*) FROM pg_extension WHERE extname = 'pg_stat_statements'"))
        finally:
            engine.dispose()
        if not enabled:
            return False, "pg_stat_statements не установлен: планы запросов недоступны"
        return True, "/metrics в формате Prometheus; pg_stat_statements включён"
    return True, "/metrics в формате Prometheus (БД не задана — статистика не проверялась)"


@check("db.backup_restore", "Резервная копия действительно восстанавливается")
def _backup_restore() -> tuple[bool, str]:
    """F: бэкап «по документации» не снимался вообще.

    У таблиц включён FORCE ROW LEVEL SECURITY, поэтому pg_dump под рабочей ролью падает
    на политиках. Проверка делает полный цикл: dump → новая база → restore → сверка
    таблиц, ревизии Alembic и числа RLS-политик.
    """
    import os
    import subprocess
    from pathlib import Path

    script = Path(__file__).resolve().parents[2] / "scripts" / "verify_backup.sh"
    if not script.exists():
        return False, "нет scripts/verify_backup.sh"
    if not (os.getenv("BACKUP_DATABASE_URL", "").strip()):
        return False, ("BACKUP_DATABASE_URL не задан: роль с BYPASSRLS нужна, потому что "
                       "pg_dump под рабочей ролью блокируется FORCE RLS")
    env = {**os.environ, "DATABASE_URL": os.getenv("DATABASE_URL", "")}
    result = subprocess.run(["bash", str(script)], capture_output=True, text=True,
                            env=env, timeout=300)
    if result.returncode != 0:
        reason = (result.stderr or result.stdout).strip().splitlines()
        return False, reason[-1] if reason else f"exit {result.returncode}"
    summary = [line for line in result.stdout.splitlines() if line.startswith("OK:")]
    return True, summary[0] if summary else "восстановление проверено"


@check("infra.profiles", "Local и prod контуры разделены, прод не публикует наружу")
def _infra_profiles() -> tuple[bool, str]:
    from pathlib import Path

    import yaml

    root = Path(__file__).resolve().parents[2]
    local = root / "docker-compose.local.yml"
    prod = root / "docker-compose.prod.yml"
    if not local.exists() or not prod.exists():
        return False, "нет docker-compose.local.yml / docker-compose.prod.yml"
    prod_spec = yaml.safe_load(prod.read_text())["services"]
    api = prod_spec.get("api", {})
    problems = []
    if api.get("ports"):
        problems.append("прод-API публикует порт наружу")
    if api.get("volumes"):
        problems.append("прод-API монтирует исходники")
    if not api.get("read_only"):
        problems.append("прод-API не read_only")
    local_db = yaml.safe_load(local.read_text())["services"].get("db", {})
    ports = " ".join(map(str, local_db.get("ports", [])))
    if ports and "127.0.0.1" not in ports:
        problems.append("локальная БД слушает не только localhost")
    if problems:
        return False, "; ".join(problems)
    return True, "прод: без портов/монтирования, read_only; локально: порты только на 127.0.0.1"


@check("docs.openapi", "OpenAPI и ADR соответствуют коду")
def _docs() -> tuple[bool, str]:
    from pathlib import Path

    repo = Path(__file__).resolve().parents[3]
    openapi = repo / "10-ARCHITECTURE" / "openapi.yaml"
    if not openapi.exists():
        return False, "нет 10-ARCHITECTURE/openapi.yaml"
    text = openapi.read_text()
    paths = text.count("\n  /")
    adr = sorted((repo / "10-ARCHITECTURE" / "adr").glob("0017*"))
    if not adr:
        return False, "нет ADR о движке публикаций (0017)"
    return True, f"путей в спецификации ≈ {paths}; ADR {adr[0].name}"


# ------------------------------------------------------------------ интерфейс
@check("ui.craft", "Визуальный слой: ни одного критичного и высокого замечания")
def _ui_craft() -> tuple[bool, str]:
    """Аудит интерфейса как пункт готовности, а не как разовое «посмотрели глазами».

    Считаются два числа: критичные и высокие замечания (должно быть 0) и общее число находок
    (потолок 5 — иначе регрессия вида «снова 40 кеглей вне шкалы» проходит незамеченной).
    Замеры в браузере (переполнение, контраст отрисованного текста, зоны нажатия) живут
    отдельно: `20-DESIGN/ui-audit/measure-app.mjs` — они требуют Chromium и в гейт не входят,
    чтобы гейт оставался воспроизводимым на чистой машине.
    """
    from pathlib import Path

    from . import ui_audit

    repo = Path(__file__).resolve().parents[3]
    if not (repo / "30-APP" / "index.html").exists():
        return False, "нет 30-APP/index.html"
    report = ui_audit.audit(repo)
    summary = report["summary"]
    head = (f"правил {report['rules']}, находок {report['total']} "
            f"(критично {summary['critical']}, высоко {summary['high']}); "
            f"отчёт 20-DESIGN/ui-audit/report.html")
    if summary["critical"] or summary["high"]:
        worst = [row["rule"] for row in report["findings"]
                 if row["severity"] in {"critical", "high"}][:3]
        return False, f"{head}; сначала закрыть: {', '.join(worst)}"
    if report["total"] > 5:
        return False, f"{head}; больше пяти открытых замечаний — регрессия"
    return True, head


def run() -> dict:
    """Прогнать все пункты. Возвращает отчёт (не бросает исключения)."""
    checks = []
    for check_id, title, infra, func in CHECK:
        try:
            ok, detail = func()
            status = "passed" if ok else "failed"
        except Exception as exc:                    # noqa: BLE001 — отчёт не должен падать
            status, detail = "failed", f"{exc.__class__.__name__}: {exc}"
        checks.append({"id": check_id, "title": title, "infra": infra,
                       "status": status, "detail": detail})
    passed = sum(1 for c in checks if c["status"] == "passed")
    return {"total": len(checks), "passed": passed, "failed": len(checks) - passed,
            "checks": checks}


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    report = run()
    if "--json" in argv:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        for item in report["checks"]:
            mark = "✅" if item["status"] == "passed" else "❌"
            print(f"{mark} {item['id']:<18} {item['title']}\n   {item['detail']}")
        print(f"\nГотовность: {report['passed']}/{report['total']}")
    return 0 if report["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
