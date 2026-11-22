"""Автоматизация ОРД: получение erid по API.

MVP-требование: пользователь получает erid в один клик из модального окна комплаенса,
не уходя в кабинет ОРД. Реализация — провайдер-адаптер с общей сигнатурой:

* ``mock``        — песочница, детерминированный erid, для тестов и демо;
* ``yandex_ord``  — Яндекс ОРД (боевой путь, требует токена и договора);
* ``vk_ord``      — VK Реклама / ОРД VK.

Каждая заявка попадает в очередь отчётности (``ord_submissions`` в каркасе,
``ord_report_queue`` в PostgreSQL) и в журнал решений.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from itertools import count
from typing import Protocol

from .config import dry_run as _dry


class OrdNotConfigured(RuntimeError):
    """Провайдер выбран, но токен/договор не настроены."""


class OrdProviderError(RuntimeError):
    """ОРД ответил ошибкой — заявку нужно повторить или разобрать вручную."""


@dataclass
class OrdCreative:
    channel: str
    advertiser_name: str
    advertiser_inn: str | None
    text: str
    variant_id: str | None = None
    external_url: str | None = None


@dataclass
class OrdResult:
    erid: str
    provider: str
    status: str                 # registered | pending_moderation | rejected
    creative_id: str
    submitted_at: datetime
    marking_block: str
    note: str = ""


class OrdProvider(Protocol):
    name: str
    label: str
    configured: bool

    def submit(self, creative: OrdCreative) -> OrdResult: ...

    def describe(self) -> dict: ...


def marking_block(creative: OrdCreative, erid: str) -> str:
    """Блок маркировки по требованиям ФАС: «Реклама», рекламодатель, erid."""
    inn = f", ИНН {creative.advertiser_inn}" if creative.advertiser_inn else ""
    return f"Реклама. {creative.advertiser_name}{inn}\nerid: {erid}"


_counter = count(1)
_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _fake_erid(n: int) -> str:
    out = ""
    x = n * 6364136223846793005 % (2 ** 61)
    for _ in range(12):
        out += _ALPHABET[x % len(_ALPHABET)]
        x //= len(_ALPHABET)
    return "2Vtz" + out[:8]


class MockOrdProvider:
    """Песочница ОРД. Ведёт себя как боевой провайдер, но без сети."""

    name = "mock"
    label = "ОРД-песочница"
    configured = True

    def submit(self, creative: OrdCreative) -> OrdResult:
        erid = _fake_erid(next(_counter))
        return OrdResult(
            erid=erid,
            provider=self.name,
            status="registered",
            creative_id=f"cr_{erid[-6:].lower()}",
            submitted_at=datetime.now(timezone.utc),
            marking_block=marking_block(creative, erid),
            note="Песочница: erid валиден по формату, но не зарегистрирован в ЕРИР.",
        )

    def describe(self) -> dict:
        return {"name": self.name, "label": self.label, "configured": True,
                "mode": "sandbox", "eta": "мгновенно"}


class _HttpOrdProvider:
    """Общая часть боевых провайдеров: заявка → erid."""

    name = "http_ord"
    label = "ОРД"
    endpoint = ""
    token_env = ""

    def __init__(self, client=None) -> None:
        self._client = client      # httpx.Client подменяется в тестах

    @property
    def configured(self) -> bool:
        return bool(_env(self.token_env))

    def submit(self, creative: OrdCreative) -> OrdResult:
        if not self.configured:
            raise OrdNotConfigured(
                f"{self.label}: не задан {self.token_env}. Пока провайдер не настроен, "
                "используйте ОРД-песочницу, чтобы не ломать сценарий публикации."
            )
        if _dry():
            raise OrdProviderError(
                f"{self.label}: DRY_RUN=1. Боевая заявка не отправлена. "
                "Для реальной регистрации нужен DRY_RUN=0 и договор с ОРД."
            )
        import httpx

        own = self._client is None
        http = self._client or httpx.Client(timeout=20.0)
        try:
            resp = http.post(
                self.endpoint,
                headers={"Authorization": f"Bearer {_env(self.token_env)}"},
                json={
                    "advertiser": {"name": creative.advertiser_name,
                                   "inn": creative.advertiser_inn},
                    "creative": {"text": creative.text, "url": creative.external_url,
                                 "platform": creative.channel},
                },
            )
            if resp.status_code >= 400:
                raise OrdProviderError(f"{self.label}: HTTP {resp.status_code} {resp.text[:160]}")
            data = resp.json() if resp.content else {}
        except OrdProviderError:
            raise
        except Exception as exc:  # noqa: BLE001 - контракт ОРД может меняться
            raise OrdProviderError(f"{self.label}: {exc}") from exc
        finally:
            if own:
                http.close()
        erid = data.get("erid") or data.get("token")
        status = data.get("status") or ("registered" if erid else "pending_moderation")
        if not erid and status != "pending_moderation":
            raise OrdProviderError(f"{self.label}: ответ без erid: {list(data)[:6]}")
        if not erid:
            return OrdResult(erid="", provider=self.name, status="pending_moderation",
                             creative_id=str(data.get("creative_id", "")),
                             submitted_at=datetime.now(timezone.utc), marking_block="",
                             note="заявка на модерации: erid появится после проверки ОРД")
        return OrdResult(erid=erid, provider=self.name, status=status,
                         creative_id=str(data.get("creative_id", "")),
                         submitted_at=datetime.now(timezone.utc),
                         marking_block=marking_block(creative, erid))

    def describe(self) -> dict:
        return {"name": self.name, "label": self.label, "configured": self.configured,
                "mode": "live", "eta": "1–24 ч модерации"}


class YandexOrdProvider(_HttpOrdProvider):
    name = "yandex_ord"
    label = "Яндекс ОРД"
    endpoint = "https://ord.yandex.ru/api/v1/creatives"
    token_env = "YANDEX_ORD_TOKEN"


class VkOrdProvider(_HttpOrdProvider):
    name = "vk_ord"
    label = "VK Реклама / ОРД"
    endpoint = "https://ads.vk.com/api/v2/ord/creatives"
    token_env = "VK_ORD_TOKEN"


def _env(name: str) -> str:
    import os

    return os.getenv(name, "")


PROVIDERS: dict[str, OrdProvider] = {
    "mock": MockOrdProvider(),
    "yandex_ord": YandexOrdProvider(),
    "vk_ord": VkOrdProvider(),
}


def get_provider(name: str) -> OrdProvider:
    provider = PROVIDERS.get(name)
    if provider is None:
        raise OrdNotConfigured(f"провайдер {name!r} неизвестен; доступны: {sorted(PROVIDERS)}")
    return provider


def default_provider() -> str:
    """Боевой провайдер, если он настроен, иначе песочница."""
    for name in (_env("ORD_PROVIDER") or "mock", "yandex_ord", "vk_ord"):
        p = PROVIDERS.get(name)
        if p is not None and p.configured:
            return p.name
    return "mock"


def describe_providers() -> list[dict]:
    return [p.describe() for p in PROVIDERS.values()]
