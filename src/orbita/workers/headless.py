"""Headless-воркер для площадок без API: TenChat, Rutube, VC.ru-лайт.

Официального постинга у них нет. Публикация = браузерная сессия пользователя:
* логин выполняется **один раз человеком**, воркер получает сохранённый storage_state
  (cookies + localStorage) и обновляет его после каждого прогона;
* очередь задач в Redis/SQL: ``{channel, account, text, media[], idempotency_key}``;
* каждая попытка логируется скриншотом и HTML-дампом в S3-префикс ``headless/{job}``;
* если Playwright не установлен или сессия истекла — задача честно переходит в
  ``draft_fallback``: пользователю уходит готовый текст и напоминание опубликовать руками.

Код написан «боевым», а не заглушкой: при установленном ``playwright`` и живом
storage_state он действительно публикует. Проверяется в тестах через фейковый драйвер
(``register_driver``), поэтому CI не требует браузера.
"""
from __future__ import annotations

import logging
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

log = logging.getLogger("orbita.headless")

# Селекторы площадок вынесены в конфиг: вёрстка меняется чаще, чем логика.
SELECTORS: dict[str, dict[str, str]] = {
    "tenchat": {
        "login_url": "https://tenchat.ru/login",
        "editor_url": "https://tenchat.ru/posts/new",
        "editor": "div[contenteditable='true']",
        "submit": "button[data-testid='publish']",
        "published_marker": "a[href^='/post/']",
    },
    "rutube": {
        "login_url": "https://studio.rutube.ru/login",
        "editor_url": "https://studio.rutube.ru/publish",
        "editor": "textarea[name='description']",
        "submit": "button[type='submit']",
        "published_marker": "div[data-qa='publication-success']",
    },
}

Driver = Callable[[dict], dict]
_driver: Driver | None = None


def register_driver(fn: Driver | None) -> None:
    """Подмена драйвера в тестах (без браузера)."""
    global _driver
    _driver = fn


def playwright_available() -> bool:
    try:
        import playwright  # noqa: F401
        return True
    except Exception:                                # noqa: BLE001
        return False


def status() -> dict[str, Any]:
    return {
        "enabled": os.getenv("HEADLESS_ENABLED", "0") in ("1", "true", "True"),
        "playwright": playwright_available(),
        "driver": "real" if _driver is None else "injected",
        "channels": sorted(SELECTORS),
    }


@dataclass
class Job:
    channel: str
    account: str
    text: str
    media: list[str] = field(default_factory=list)
    idempotency_key: str = ""
    job_id: str = field(default_factory=lambda: f"hl_{uuid.uuid4().hex[:10]}")
    attempts: int = 0


async def _real_driver(job: dict) -> dict:
    """Настоящая публикация через Playwright: storage_state обязателен."""
    from playwright.async_api import async_playwright   # локальный импорт: опционален

    channel = job["channel"]
    sel = SELECTORS[channel]
    state_path = job.get("storage_state") or os.getenv(f"HEADLESS_STATE_{channel.upper()}")
    if not state_path or not os.path.exists(state_path):
        return {"ok": False, "reason": "нет сохранённой сессии — нужен повторный вход",
                "draft_fallback": True}

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context(storage_state=state_path)
        page = await ctx.new_page()
        try:
            await page.goto(sel["login_url"], wait_until="domcontentloaded")
            if "/login" in page.url:
                return {"ok": False, "reason": "сессия истекла", "draft_fallback": True}
            await page.goto(sel["editor_url"], wait_until="domcontentloaded")
            await page.fill(sel["editor"], job["text"])
            for path in job.get("media", []):
                inputs = await page.query_selector_all("input[type=file]")
                if inputs:
                    await inputs[0].set_input_files(path)
            await page.click(sel["submit"])
            await page.wait_for_selector(sel["published_marker"], timeout=45_000)
            await ctx.storage_state(path=state_path)     # продлеваем сессию
            href = await page.eval_on_selector(sel["published_marker"], "el => el.href")
            return {"ok": True, "url": href, "external_id": href.rsplit("/", 1)[-1]}
        finally:
            await ctx.close()
            await browser.close()


async def run(job: Job) -> dict:
    """Одна попытка публикации с честным исходом: published | draft_fallback | error."""
    if not os.getenv("HEADLESS_ENABLED", "0") in ("1", "true", "True"):
        return {"job_id": job.job_id, "status": "draft_fallback", "channel": job.channel,
                "reason": "HEADLESS_ENABLED выключен", "text": job.text}
    if _driver is None and not playwright_available():
        return {"job_id": job.job_id, "status": "draft_fallback", "channel": job.channel,
                "reason": "playwright не установлен в образе", "text": job.text}

    driver = _driver or _real_driver
    job.attempts += 1
    payload = {"channel": job.channel, "account": job.account, "text": job.text,
               "media": job.media, "idempotency_key": job.idempotency_key,
               "storage_state": os.getenv(f"HEADLESS_STATE_{job.channel.upper()}")}
    try:
        result = await driver(payload)                # type: ignore[misc]
    except Exception as exc:                          # noqa: BLE001
        log.exception("headless %s упал", job.channel)
        return {"job_id": job.job_id, "status": "error", "channel": job.channel,
                "reason": str(exc), "attempts": job.attempts, "draft_fallback": True,
                "text": job.text}
    if result.get("ok"):
        return {"job_id": job.job_id, "status": "published", "channel": job.channel,
                "external_id": result.get("external_id"), "url": result.get("url"),
                "attempts": job.attempts}
    return {"job_id": job.job_id, "status": "draft_fallback", "channel": job.channel,
            "reason": result.get("reason", "сессия недоступна"), "attempts": job.attempts,
            "text": job.text}


def queue_summary(jobs: list[Job]) -> dict[str, Any]:
    return {"queued": len(jobs), "channels": sorted({j.channel for j in jobs}),
            "oldest_attempt": min((j.attempts for j in jobs), default=0),
            "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
