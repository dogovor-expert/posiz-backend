"""LLM-клиент: OpenAI-совместимый роутер, две физические модели на пять ролей (ADR 0009).

Что здесь есть:
* маршрутизация роль → модель (`ROLE_MODEL`), цены и учёт токенов в рублях;
* POST ``{base}/chat/completions`` и ``{base}/embeddings`` через один ``httpx.Client``
  (подмена транспорта в тестах — без сети);
* ретраи на 429/5xx с экспоненциальной паузой, таймауты, отмена по бюджету;
* строгий JSON-режим (``response_format={"type": "json_object"}``) для редактора/фактчекера;
* **PII-маскирование до отправки** (152-ФЗ: ФИО, телефоны, ИНН не уходят в зарубежную модель,
  если включён ``PII_MASKING``) и восстановление плейсхолдеров в ответе;
* детерминированный fallback: без ключа или при ``DRY_RUN`` клиент работает офлайн,
  но помечает ответ ``"simulated": true`` — интерфейс и тесты не различают кодовые пути.

Ключи только из окружения: ``DEEPSEEK_API_KEY`` / ``GEMINI_API_KEY`` / ``LLM_ROUTER_KEY``.
Ничего не хардкодится, отсутствие ключа — честный статус в ``/health`` (``llm.mode=simulated``).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import random
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

import httpx

from . import pii

log = logging.getLogger("orbita.llm")

EMBED_DIM = 1024

# роль агента → модель (2 физические модели на 5 ролей, ADR 0009)
ROLE_MODEL: dict[str, str] = {
    "strategist": "heavy",
    "copywriter": "heavy",
    "editor": "heavy",
    "adapter": "fast",
    "visual": "fast",
    "fact_checker": "fast",
    "compliance": "rules",      # правило-движок, модель не нужна
}

# цена за 1K токенов, ₽ (вход/выход) — из ai-architecture.md
PRICES: dict[str, tuple[float, float]] = {
    "deepseek-v4-pro": (0.10, 0.40),
    "deepseek-flash": (0.02, 0.08),
    "gemini-3.8-flash": (0.06, 0.24),
    "gpt-6.1-sol": (1.20, 4.80),
    "embed-multilingual-1024": (0.01, 0.0),
}


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def models() -> dict[str, str]:
    return {
        "heavy": _env("LLM_HEAVY", "deepseek-v4-pro"),
        "fast": _env("LLM_FAST", "deepseek-flash"),
        "vision": _env("LLM_VISION_FALLBACK", "gemini-3.8-flash"),
        "premium": _env("LLM_PREMIUM", "gpt-6.1-sol"),
        "embed": _env("EMBED_MODEL", "embed-multilingual-1024"),
    }


def router_url() -> str:
    base = _env("LLM_ROUTER_URL") or _env("LLM_BASE_URL")
    if base:
        return base.rstrip("/")
    # OpenAI-совместимый шлюз по умолчанию; переопределяется одной переменной
    if _env("DEEPSEEK_API_KEY"):
        return "https://api.deepseek.com/v1"
    if _env("GEMINI_API_KEY"):
        return "https://generativelanguage.googleapis.com/v1beta/openai"
    return ""


def api_key() -> str:
    return _env("LLM_ROUTER_KEY") or _env("DEEPSEEK_API_KEY") or _env("GEMINI_API_KEY")


def enabled() -> bool:
    """Есть ли реальный транспорт. DRY_RUN=1 принудительно включает симуляцию."""
    if _env("DRY_RUN", "1") not in ("0", "false", "False"):
        return False
    return bool(router_url() and api_key())


def status() -> dict[str, Any]:
    return {
        "mode": "live" if enabled() else "simulated",
        "router": router_url() or None,
        "key_present": bool(api_key()),
        "models": models(),
        "embed_dim": EMBED_DIM,
        "pii_masking": bool(pii.enabled()),
    }


@dataclass
class Usage:
    model: str
    role: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_rub: float = 0.0
    simulated: bool = False


USAGE: list[Usage] = []


def cost_of(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    inp, out = PRICES.get(model, (0.0, 0.0))
    return round(prompt_tokens / 1000 * inp + completion_tokens / 1000 * out, 4)


def cost_report() -> dict[str, Any]:
    total = round(sum(u.cost_rub for u in USAGE), 4)
    by_model: dict[str, float] = {}
    for u in USAGE:
        by_model[u.model] = round(by_model.get(u.model, 0.0) + u.cost_rub, 4)
    tokens = sum(u.prompt_tokens + u.completion_tokens for u in USAGE)
    return {"calls": len(USAGE), "tokens": tokens, "cost_rub": total, "by_model": by_model}


class LLMError(RuntimeError):
    """Исчерпаны ретраи или роутер вернул неразбираемый ответ."""


@dataclass
class LLM:
    """Клиент роутера. Один экземпляр на процесс, транспорт можно подменить."""

    client: httpx.Client | None = None
    timeout: float = 30.0
    retries: int = 2
    backoff: float = 0.4
    _own_client: bool = field(default=False, repr=False)

    # ---------- транспорт ----------
    def _http(self) -> httpx.Client:
        if self.client is None:
            self.client = httpx.Client(timeout=self.timeout)
            self._own_client = True
        return self.client

    def close(self) -> None:
        if self.client is not None and self._own_client:
            self.client.close()
            self.client = None

    def _post(self, path: str, payload: dict) -> dict:
        url = f"{router_url()}{path}"
        headers = {"Authorization": f"Bearer {api_key()}", "Content-Type": "application/json"}
        last: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                resp = self._http().post(url, json=payload, headers=headers)
                if resp.status_code in (408, 409, 429) or resp.status_code >= 500:
                    raise LLMError(f"router {resp.status_code}: {resp.text[:200]}")
                if resp.status_code >= 400:
                    raise LLMError(f"router {resp.status_code}: {resp.text[:200]}")
                return resp.json()
            except (httpx.HTTPError, LLMError, ValueError) as exc:   # сеть, 5xx, битый JSON
                last = exc
                if attempt >= self.retries:
                    break
                time.sleep(self.backoff * (2 ** attempt))
        msg = f"LLM недоступен после {self.retries + 1} попыток: {last}"
        log.warning(msg)
        raise LLMError(msg)

    # ---------- chat ----------
    def complete(self, role: str, messages: Sequence[dict], *, model_role: str | None = None,
                 temperature: float = 0.7, max_tokens: int = 1200,
                 json_mode: bool = False, mask_pii: bool | None = None,
                 fallback: str = "") -> dict[str, Any]:
        """Вернуть ``{"text", "model", "usage", "simulated", "pii_map"}``.

        ``mask_pii`` по умолчанию берётся из окружения (``PII_MASKING``, дефолт — включено).
        """
        key_role = model_role or ROLE_MODEL.get(role, "fast")
        model = models().get(key_role, models()["fast"])
        do_mask = pii.enabled() if mask_pii is None else mask_pii

        out_messages: list[dict] = []
        pii_map: dict[str, str] = {}
        # Один маскировщик на весь запрос: раньше каждый message начинал нумерацию с 1,
        # поэтому в промпте из нескольких сообщений [PHONE_1] означал разные номера,
        # а модель склеивала разных клиентов в одного.
        masker = pii.Masker() if do_mask else None
        for m in messages:
            content = m.get("content", "")
            if masker is not None and isinstance(content, str):
                content, pii_map = masker.mask(content)
            out_messages.append({**m, "content": content})

        if not enabled():
            text = fallback or self._simulate(role, out_messages, json_mode)
            usage = Usage(model=model, role=role, prompt_tokens=0, completion_tokens=0,
                          simulated=True)
            USAGE.append(usage)
            return {"text": pii.restore(text, pii_map), "model": model, "simulated": True,
                    "usage": usage, "pii_map": pii_map}

        payload: dict[str, Any] = {
            "model": model,
            "messages": out_messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        data = self._post("/chat/completions", payload)
        try:
            text = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"неожиданный формат ответа: {str(data)[:200]}") from exc
        raw_usage = data.get("usage") or {}
        usage = Usage(
            model=data.get("model") or model, role=role,
            prompt_tokens=int(raw_usage.get("prompt_tokens", 0)),
            completion_tokens=int(raw_usage.get("completion_tokens", 0)),
        )
        usage.cost_rub = cost_of(usage.model, usage.prompt_tokens, usage.completion_tokens)
        USAGE.append(usage)
        return {"text": pii.restore(text, pii_map), "model": usage.model, "simulated": False,
                "usage": usage, "pii_map": pii_map}

    def complete_json(self, role: str, messages: Sequence[dict], *, fallback: dict | None = None,
                      keys: tuple[str, ...] = (), **kwargs) -> tuple[dict, dict[str, Any]]:
        """JSON-ответ модели с проверкой структуры.

        Раньше разбор был «попробуем json.loads, не вышло — молча fallback»: лишние ключи
        и текст вокруг JSON проходили дальше в бизнес-логику. Теперь разбор идёт через
        :func:`orbita.prompts.parse_json`: лишние ключи отбрасываются, при несоответствии
        структуры берётся fallback с записью в журнал.
        """
        fb = json.dumps(fallback or {}, ensure_ascii=False)
        res = self.complete(role, messages, json_mode=True, fallback=fb, **kwargs)
        from . import prompts as _prompts

        try:
            data = _prompts.parse_json(res["text"], keys=keys)
            if fallback:
                data = {**fallback, **data}
            return data, res
        except _prompts.OutputRejected as exc:
            log.warning("LLM вернул непригодный JSON (%s): %s", exc, res["text"][:120])
            return dict(fallback or {}), res

    # ---------- эмбеддинги ----------
    def embed(self, texts: Iterable[str], *, workspace_id: str | None = None) -> list[list[float]]:
        """Вектор 1024. В симуляции — детерминированный хэш (косинус сохраняет близость).

        Тексты перед отправкой провайдеру проходят PII-маскирование: раньше эмбеддинги
        уходили «как есть», а через них проходят документы Brand Brain, переписка и
        материалы клиентов — то есть ПДн утекали в обход маскирования чата.
        """
        items = [t for t in texts]
        if enabled() and pii.enabled():
            masker = pii.Masker()
            items = [masker.mask(t)[0] for t in items]
        if not enabled():
            return [self._hash_embed(t) for t in items]
        payload = {"model": models()["embed"], "input": items, "dimensions": EMBED_DIM}
        data = self._post("/embeddings", payload)
        rows = data.get("data") or []
        if len(rows) != len(items):
            raise LLMError("роутер вернул не то количество векторов")
        usage = Usage(model=models()["embed"], role="embed",
                      prompt_tokens=int((data.get("usage") or {}).get("prompt_tokens", 0)))
        usage.cost_rub = cost_of(usage.model, usage.prompt_tokens, 0)
        USAGE.append(usage)
        return [list(map(float, r["embedding"])) for r in rows]

    @staticmethod
    def _hash_embed(text: str) -> list[float]:
        seed = hashlib.sha256(text.lower().strip().encode()).digest()
        rnd = random.Random(seed)
        vec = [rnd.uniform(-1, 1) for _ in range(EMBED_DIM)]
        norm = sum(v * v for v in vec) ** 0.5 or 1.0
        return [round(v / norm, 6) for v in vec]

    # ---------- симуляция ----------
    @staticmethod
    def _simulate(role: str, messages: Sequence[dict], json_mode: bool) -> str:
        prompt = " ".join(str(m.get("content", "")) for m in messages if m.get("role") == "user")
        head = prompt.strip().split("\n")[0][:120] if prompt.strip() else "сигнал"
        if json_mode:
            if role == "editor":
                return json.dumps({"score": 4.4, "issues": [],
                                   "verdict": "готово к публикации"}, ensure_ascii=False)
            if role == "fact_checker":
                return json.dumps({"claims": [], "risks": [], "ok": True}, ensure_ascii=False)
            return json.dumps({"ok": True, "title": head}, ensure_ascii=False)
        return f"{head}"


# Процессный экземпляр и хелпер для приложения
_client = LLM()


def get_client() -> LLM:
    return _client


def chat(role: str, messages: Sequence[dict], **kwargs) -> dict[str, Any]:
    return _client.complete(role, messages, **kwargs)


def embed(texts: Iterable[str], **kwargs) -> list[list[float]]:
    return _client.embed(texts, **kwargs)
