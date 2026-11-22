"""Точка входа FastAPI.

Периметр (все три вещи раньше отсутствовали):
* CORS — только домены из ``CORS_ORIGINS``, ``*`` несовместим с ``allow_credentials``;
* заголовки безопасности на каждом ответе (``X-Content-Type-Options``, CSP, frame-ancestors);
* журнал решений пишет, кто действовал — из JWT, а не из заголовка.
"""
from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse

from . import __version__, ratelimit, rbac, security, store, tenancy
from .tenancy import TenancyError
from .api import public_router, router
from .api_v3 import public as public_v3
from .api_v3 import router as router_v3

app = FastAPI(
    title="Posiz Core API",
    version=__version__,
    description=(
        "Posiz — ядро AI-хаба управления соцсетями и мессенджерами (posiz.ru). "
        "Публичный API: api.posiz.ru. Короткие ссылки — posiz.ru/l/{code}, "
        "Link-in-Bio — posiz.ru/@{slug}. "
        "Слой данных переключается: memory (каркас, тесты) или PostgreSQL (DATA_LAYER=db)."
    ),
)

app.add_middleware(CORSMiddleware, **security.cors_settings())

app.include_router(router)
app.include_router(router_v3)
app.include_router(public_router)
app.include_router(public_v3)


NOINDEX_PREFIXES = ("/l/", "/@", "/bio/")


@app.middleware("http")
async def seo_headers_middleware(request: Request, call_next):
    """Служебные и пользовательские страницы закрыты от индексации.

    Короткие ссылки (``/l/<code>``) и Link-in-Bio клиентов (``/@<slug>``) не должны
    попадать в индекс: они размывают тематический вес домена и светят чужие UTM-метки.
    """
    response = await call_next(request)
    if request.url.path.startswith(NOINDEX_PREFIXES):
        response.headers["X-Robots-Tag"] = "noindex, nofollow"
    return response


@app.middleware("http")
async def audit_middleware(request: Request, call_next):
    """Журнал решений: кто (роль из JWT или демо), что, с каким кодом."""
    response = await call_next(request)
    if request.url.path.startswith(("/api/", "/l/")):
        store.audit(
            _actor_label(request), request.method, request.url.path, response.status_code,
        )
    return response


@app.middleware("http")
async def tenancy_middleware(request: Request, call_next):
    """Привязка контекста арендатора на весь запрос.

    Это страховка на уровне слоя данных: даже если обработчик забыл передать
    ``workspace_id``, `persistence` сузит выборку до workspace из токена, а не отдаст
    документы всех клиентов. Публичные маршруты (``/l/``, ``/@``) контекст не получают —
    они читают по коду ссылки/слагу и данных других арендаторов не раскрывают.
    """
    scope = rbac.scope_from_headers(request.headers)
    if scope is None:
        return await call_next(request)
    with tenancy.bind(workspace_id=scope[0], role=scope[1], user_id=scope[2]):
        return await call_next(request)


@app.middleware("http")
async def security_headers_middleware(request: Request, call_next):
    response = await call_next(request)
    for name, value in security.security_headers_for(request.url.path).items():
        response.headers.setdefault(name, value)
    return response


def _actor_label(request: Request) -> str:
    """Роль для журнала: из подписанного JWT, иначе из демо-заголовка, иначе anonymous."""
    authorization = request.headers.get("authorization", "")
    if authorization.lower().startswith("bearer "):
        try:
            claims = security.decode_jwt(authorization.split(" ", 1)[1].strip())
            return str(claims.get("role", "jwt"))
        except Exception:                              # noqa: BLE001 — токен битый, это увидит RBAC
            return "invalid_token"
    return request.headers.get("x-actor-role", "anonymous").lower()


@app.exception_handler(TenancyError)
async def tenancy_violation_handler(request: Request, exc: TenancyError):   # pragma: no cover
    """Попытка выйти за пределы своего пространства — 403 и запись в лог.

    Раньше нарушение изоляции всплывало как 500 (или, хуже, тихо записывалось в ``ws_de``).
    Отдельный обработчик нужен и для клиента (понятная ошибка), и для эксплуатации:
    такие события — сигнал атаки, а не «шум».
    """
    logging.getLogger("orbita.tenancy").warning(
        "tenancy_violation path=%s: %s", request.url.path, exc)
    return JSONResponse({"detail": {"kind": "tenancy_violation",
                                    "message": "Данные принадлежат другому рабочему пространству."}},
                        status_code=403)


@app.exception_handler(429)
async def rate_limited_handler(request: Request, exc):                 # pragma: no cover
    detail = exc.detail if isinstance(exc.detail, dict) else {"kind": "rate_limited"}
    headers = getattr(exc, "headers", None) or {}
    return JSONResponse({"detail": detail}, status_code=429, headers=headers)


@app.get("/metrics", include_in_schema=True, tags=["system"])
def metrics_endpoint(request: Request):
    """Метрики Prometheus. Не публичный маршрут: см. ``metrics.authorize``.

    Возвращается текст (``text/plain``), а не JSON: формат экспозиции фиксирован, и
    скрейпер не должен разбирать вложенные структуры.
    """
    from . import metrics as metrics_mod

    allowed, reason = metrics_mod.authorize(dict(request.headers))
    if not allowed:
        return JSONResponse({"detail": {"kind": "forbidden", "message": reason}},
                            status_code=403)
    return PlainTextResponse(metrics_mod.prometheus(), media_type="text/plain; version=0.0.4")


@app.get("/health", tags=["system"])
def health() -> dict:
    """Статус контура: что боевое, что в песочнице. Врать нельзя — это алертинг."""
    from . import auth, edo, knowledge, llm, mailer, ord, sessions
    from .config import data_layer, demo_roles_enabled, dry_run, llm_mode
    from .workers import headless

    integrations = {
        "llm": llm.status(),
        "mailer": mailer.status(),
        "sessions": sessions.status(),
        "auth": {"demo_mode": auth.demo_mode(), **auth.transport_status()["oauth"]},
        "edo": edo.status(),
        "ord": {"default": ord.default_provider(),
                "providers": ord.describe_providers()},
        "headless": headless.status(),
        "rag": knowledge.stats(),
    }
    live = sum(1 for key in ("llm", "mailer") if integrations[key].get("mode") == "live"
               or integrations[key].get("configured"))
    return {
        "status": "ok",
        "version": __version__,
        "dry_run": dry_run(),
        "data_layer": data_layer(),
        "auth": "demo-header" if demo_roles_enabled() else "jwt",
        "rate_limit": ratelimit.describe(),
        "llm": llm_mode(),
        "live_integrations": live,
        "integrations": integrations,
    }
