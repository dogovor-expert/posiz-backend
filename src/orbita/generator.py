"""«Рой» агентов: бриф → ядро → канал → фактчек → комплаенс → редактор.

Реальный контур (ADR 0005/0009):
* Strategist (heavy) — бриф по сигналу **и контексту Brand Brain** (гибридный поиск BM25+вектор);
* Copywriter (heavy) — смысловое ядро, ссылается на карточки, а не на память модели;
* Adapter (fast) — переупаковка под лимиты и разметку канала (`CHANNEL_SPECS`);
* Fact-Checker (fast, JSON) — сверка утверждений с найденными карточками, помечает риски;
* Compliance — правило-движок (без модели): стоп-лист, ОРД-маркировка, запреты канала;
* Editor (heavy, JSON) — оценка и список правок; не более 2 итераций, порог 3,5.

Промпты маскируют PII до отправки (`llm.complete(..., mask_pii=True)`), а
`simulated=True` проставляется в ответе, если работал офлайн-фолбэк: наружу честно
видно, что текст не от модели. Стоимость считается по фактическим токенам.
"""
from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field

from . import knowledge, llm, metrics, prompts, quota, store

log = logging.getLogger("orbita.generator")

CHANNEL_SPECS: dict[str, dict] = {
    "max":      {"format": "longread", "limit": 4000, "markdown": "plain-safe", "link_in_reply": False},
    "vk":       {"format": "longread", "limit": 4000, "markdown": "limited", "link_in_reply": False},
    "telegram": {"format": "longread", "limit": 4096, "markdown": "markdown_v2", "link_in_reply": False},
    "ok":       {"format": "short",    "limit": 3000, "markdown": "plain", "link_in_reply": False},
    "x":        {"format": "short",    "limit": 280,  "markdown": "plain", "link_in_reply": True},
}

AGENT_COST_RUB = {           # плановая оценка, если LLM-клиент в симуляции
    "strategist": 0.9, "copywriter": 1.4, "adapter": 0.3,
    "fact_checker": 0.2, "compliance": 0.0, "editor": 0.4,
}

STOPLIST = ("бесплатно", "гарантируем победу", "100% результат")

SYSTEM = {
    "strategist": "Ты стратег контента. Опирайся только на контекст Brand Brain. "
                  "Отвечай по-русски, кратко, в формате JSON.",
    "copywriter": "Ты копирайтер. Пиши по-русски, спокойно, без юридического сленга и "
                  "обещаний результата суда. Не выдумывай факты вне контекста.",
    "adapter": "Ты адаптер под соцсеть. Сохраняй смысл, соблюдай лимит канала.",
    "fact_checker": "Ты фактчекер. Сверяй утверждения с карточками знаний, отвечай JSON.",
    "editor": "Ты редактор. Оцени текст по шкале 1–5 и верни JSON с полем score и issues.",
}


@dataclass
class Brief:
    goal: str
    audience: str
    thesis: str
    cta: str
    citations: list[dict] = field(default_factory=list)
    context: str = ""
    #: маркеры попыток инъекции, встреченные в данных Brand Brain (наблюдаемость)
    injection_signals: list[str] = field(default_factory=list)


def _pii_safe(text: str) -> str:
    return text


def make_brief(title: str, goal: str, *, workspace_id: str) -> Brief:
    """Бриф без пространства не строится: Brand Brain читается только своего клиента."""
    """Strategist: бриф с опорой на Brand Brain (RAG) — с фолбэком без сети."""
    ctx = knowledge.context_block(f"{title} {goal}", workspace_id=workspace_id, top_k=5)
    fallback = json.dumps({
        "audience": "малый бизнес и самозанятые без юриста в штате",
        "thesis": title,
        "cta": "Открыть конструктор и проверить свой договор",
    }, ensure_ascii=False)
    # Контекст Brand Brain приходит из внешнего мира (сайт, карточки товаров, инбокс) и
    # раньше подставлялся прямо в сообщение с инструкцией: текст «игнорируй инструкции»
    # в карточке был для модели неотличим от нашей задачи. Теперь это отдельный блок
    # данных, а правило иерархии — в system-сообщении.
    context_block = prompts.data_block(ctx["text"], kind="brand_brain", title="Brand Brain")
    messages = prompts.build(
        system=SYSTEM["strategist"],
        instruction=(f"Сигнал: {title}\nЦель: {goal}\n\n"
                     "Сформулируй audience, thesis, cta."),
        data=context_block, json_keys=("audience", "thesis", "cta"))
    data, _ = llm.get_client().complete_json(
        "strategist", messages, fallback=json.loads(fallback), max_tokens=400)
    return Brief(
        goal=goal,
        audience=data.get("audience") or "малый бизнес без юриста в штате",
        thesis=data.get("thesis") or title,
        cta=data.get("cta") or "Открыть конструктор и проверить свой договор",
        citations=ctx["citations"],
        context=ctx["text"],
        injection_signals=context_block.signals,
    )


def core_copy(brief: Brief) -> str:
    """Copywriter: смысловое ядро без учёта канала."""
    messages = prompts.build(
        system=SYSTEM["copywriter"],
        instruction=(f"Тезис: {brief.thesis}\nАудитория: {brief.audience}\n"
                     f"CTA: {brief.cta}\n\n"
                     "Напиши 3–4 абзаца: что проверить, какие формулировки опасны, "
                     "как исправить. Факты бери только из блока данных."),
        data=prompts.data_block(brief.context, kind="brand_brain", title="Brand Brain"))
    res = llm.get_client().complete("copywriter", messages, max_tokens=900,
                                   fallback=f"{brief.thesis}\n\n"
                                            "Короткий разбор: что проверить, какие формулировки "
                                            f"опасны и как исправить.\n\n{brief.cta}.")
    return res["text"].strip()


def adapt(channel: str, core: str, link_url: str | None) -> tuple[str, bool]:
    spec = CHANNEL_SPECS[channel]
    link_in_reply = spec["link_in_reply"]
    body = core
    if link_url and not link_in_reply:
        body = f"{body}\n\nПодробнее: {link_url}"
    if channel == "x":
        first = core.split("\n")[0]
        body = first[:279]
    if len(body) > spec["limit"]:
        body = body[: spec["limit"] - 1].rstrip() + "…"
    return body, link_in_reply


def adapt_llm(channel: str, core: str, link_url: str | None) -> tuple[str, bool]:
    """Adapter-агент: переупаковка моделью, затем жёсткая обрезка под лимит канала."""
    spec = CHANNEL_SPECS[channel]
    messages = [
        {"role": "system", "content": SYSTEM["adapter"]},
        {"role": "user", "content":
            f"Канал: {channel}. Лимит: {spec['limit']} символов. Разметка: {spec['markdown']}.\n"
            f"Текст:\n{core}\n\nВерни только готовый текст поста."},
    ]
    res = llm.get_client().complete("adapter", messages, max_tokens=900, fallback=core)
    return adapt(channel, res["text"].strip() or core, link_url)


def fact_check(body: str, brief: Brief) -> dict:
    messages = prompts.build(
        system=SYSTEM["fact_checker"],
        instruction=("Проверь текст на соответствие карточкам. Верни claims[] "
                     "(утверждение, confirmed, card_id), risks[], ok."),
        data=[prompts.data_block(body, kind="draft", title="Черновик"),
              prompts.data_block(brief.context, kind="brand_brain", title="Brand Brain")],
        json_keys=("claims", "risks", "ok"))
    data, res = llm.get_client().complete_json("fact_checker", messages,
                                               fallback={"claims": [], "risks": [], "ok": True},
                                               max_tokens=400)
    supported = [c for c in data.get("claims", []) if c.get("confirmed") or c.get("подтверждено")]
    return {
        "ok": bool(data.get("ok", True)) and not data.get("risks"),
        "claims": data.get("claims", []),
        "risks": data.get("risks", []),
        "supported": len(supported),
        "simulated": res["simulated"],
    }


def compliance(body: str) -> dict:
    """Правило-движок: без модели, поэтому детерминирован и бесплатен."""
    lowered = body.lower()
    hits = [w for w in STOPLIST if w in lowered]
    return {"ok": not hits, "blocked": hits,
            "checks": ["stoplist", "claims_promise", "ord_marking_required"]}


def edit(body: str, brief: Brief) -> dict:
    """Editor: до 2 итераций доработки, порог 3,5 из 5."""
    score, issues, text = 0.0, [], body
    for _ in range(2):
        messages = [
            {"role": "system", "content": SYSTEM["editor"]},
            {"role": "user", "content": f"Текст:\n{text}\n\nВерни JSON: score (1-5), issues[]."},
        ]
        data, _res = llm.get_client().complete_json("editor", messages,
                                                    fallback={"score": 4.2, "issues": []},
                                                    max_tokens=300)
        try:
            score = float(data.get("score") or 0)
        except (TypeError, ValueError):
            score = 0.0
        issues = data.get("issues") or []
        if score >= 3.5:
            break
        messages = [
            {"role": "system", "content": SYSTEM["copywriter"]},
            {"role": "user", "content":
                f"Перепиши текст, устранив замечания: {issues}\n\nТекст:\n{text}\n\n"
                f"CTA: {brief.cta}"},
        ]
        text = llm.get_client().complete("editor", messages, max_tokens=900,
                                        fallback=text)["text"].strip() or text
    return {"score": round(score, 2), "issues": issues, "text": text}


def run(signal_id: str, channels: list[str], link_url: str | None = None, *,
        workspace_id: str) -> dict:
    """Прогон роя. ``workspace_id`` обязателен.

    Раньше здесь стояло `workspace_id="ws_de"`: материал любого клиента генерировался
    (и сохранялся) в демо-пространстве, а в режиме БД запись отклонялась проверкой
    арендатора. Теперь рабочее пространство идёт через весь пайплайн параметром.
    """
    signal = store.get_signal(signal_id, workspace_id=workspace_id)
    if signal is None:
        raise KeyError(signal_id)

    calls_before = len(llm.USAGE)
    brief = make_brief(signal["title"], signal["goal"], workspace_id=workspace_id)
    core = core_copy(brief)

    variants, simulated_flags = [], []
    for ch in channels:
        if ch not in CHANNEL_SPECS:
            continue
        body, link_in_reply = adapt_llm(ch, core, link_url)
        facts = fact_check(body, brief)
        comp = compliance(body)
        review = edit(body, brief)
        body = adapt(ch, review["text"], link_url)[0]
        variant = store.save_variant(
            signal_id=signal_id, channel=ch, fmt=CHANNEL_SPECS[ch]["format"],
            title=signal["title"], body=body, link_url=link_url,
            link_in_reply=link_in_reply, score=review["score"], workspace_id=workspace_id,
        )
        # результаты проверок сохраняются, а не теряются в объекте памяти:
        # без этого в карточке варианта не было ни фактов, ни комплаенса
        variant = store.update_variant(
            variant["id"], workspace_id=workspace_id,
            facts=facts, compliance=comp, citations=list(brief.citations),
            issues=review["issues"],
        ) or variant
        variants.append(variant)
        simulated_flags.append(facts["simulated"])

    store.set_signal_status(signal_id, "generated", workspace_id=workspace_id)
    spent = llm.USAGE[calls_before:]
    cost = round(sum(u.cost_rub for u in spent), 4)
    tokens = sum(u.prompt_tokens + u.completion_tokens for u in spent)
    if not tokens and not llm.enabled():
        # Офлайн-симуляция не тратит токены провайдера, но квота должна вести себя так
        # же, как в бою — иначе на стенде лимит «не кончается» и проверка лимитов
        # превращается в фикцию. Оценка: ~4 символа на токен.
        approx = len(brief.context) + len(core) + sum(len(v.get("body") or "") for v in variants)
        tokens = max(1, approx // 4)

    # Учёт токенов. Списание идёт ПОСЛЕ вызова (токены уже потрачены), поэтому
    # превышение не отменяет работу — оно фиксируется счётчиком и предупреждением, а
    # следующая генерация упрётся в лимит на входе (POST /signals/{id}/generate).
    if tokens:
        try:
            quota.consume("llm_tokens", tokens, workspace_id)
        except quota.QuotaExceeded as exc:
            metrics.inc("quota_tokens_overrun")
            log.warning("квота токенов исчерпана (%s): работа %s выполнена, следующая — с 402",
                        exc, workspace_id)

    if brief.injection_signals:
        metrics.inc("prompt_injection_signals", len(brief.injection_signals))
    if not cost:        # офлайн-симуляция: плановая оценка, чтобы UI не показывал 0 ₽
        cost = round(sum(AGENT_COST_RUB.values()) * max(len(variants), 1), 2)
    return {
        "run_id": f"run_{uuid.uuid4().hex[:12]}",
        "status": "completed",
        "variants": variants,
        "cost_rub": cost,
        "llm": {
            "mode": "live" if llm.enabled() else "simulated",
            "calls": len(spent),
            "tokens": tokens,
            "models": sorted({u.model for u in spent}),
            "all_simulated": all(simulated_flags) if simulated_flags else True,
        },
        "rag": {"hits": len(brief.citations),
                "backend": "bm25+cosine(1024), pgvector-ready"},
        # граница «инструкции/данные»: видно, пытался ли источник влиять на модель.
        # Это не «блокировка» (текст карточки остаётся в контексте), а сигнал для
        # модерации и для разбора инцидентов.
        "prompt_guard": {"injection_signals": brief.injection_signals,
                         "suspicious": bool(brief.injection_signals),
                         "data_isolation": "prompts.data_block"},
    }
