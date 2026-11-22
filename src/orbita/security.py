"""Безопасность периметра: JWT, заголовки ответа, CORS и проверка вебхуков.

Почему отдельный модуль: до него проверки были размазаны между ``rbac.py`` (роль из
заголовка), ``main.py`` (CORS ``*``) и обработчиками вебхуков (без подписи). Здесь
одна точка правды, которую можно тестировать без FastAPI.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import os
import time
from typing import Any

from fastapi import HTTPException

from .config import YOOKASSA_NETWORKS, cors_origins, jwt_secret, yookassa_webhook_secret

# ---------------------------------------------------------------------------
# JWT (HS256/HS384/HS512) без внешних зависимостей
# ---------------------------------------------------------------------------
_ALGORITHMS = {
    "HS256": hashlib.sha256,
    "HS384": hashlib.sha384,
    "HS512": hashlib.sha512,
}


def _b64url_decode(part: str) -> bytes:
    pad = "=" * (-len(part) % 4)
    return base64.urlsafe_b64decode(part + pad)


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def decode_jwt(token: str, *, secret: str | None = None, leeway: int = 30) -> dict[str, Any]:
    """Проверить подпись и срок жизни токена. Возвращает claims или бросает 401."""
    secret = secret if secret is not None else jwt_secret()
    if not secret:
        raise HTTPException(401, detail={"kind": "unauthenticated",
                                        "message": "JWT_SECRET не настроен: проверка токенов невозможна."})
    try:
        header_b64, payload_b64, signature_b64 = token.split(".")
    except ValueError:
        raise HTTPException(401, detail={"kind": "unauthenticated", "message": "Некорректный формат JWT."})

    try:
        header = json.loads(_b64url_decode(header_b64))
        payload = json.loads(_b64url_decode(payload_b64))
    except (ValueError, json.JSONDecodeError):
        raise HTTPException(401, detail={"kind": "unauthenticated", "message": "Некорректный JWT."})

    alg = str(header.get("alg", "HS256")).upper()
    if alg == "NONE" or alg not in _ALGORITHMS:
        raise HTTPException(401, detail={"kind": "unauthenticated",
                                        "message": f"Алгоритм подписи {alg} запрещён."})

    signing_input = f"{header_b64}.{payload_b64}".encode()
    expected = hmac.new(secret.encode(), signing_input, _ALGORITHMS[alg]).digest()
    try:
        actual = _b64url_decode(signature_b64)
    except Exception:                                    # noqa: BLE001 — битая подпись
        raise HTTPException(401, detail={"kind": "unauthenticated", "message": "Некорректная подпись JWT."})
    if not hmac.compare_digest(expected, actual):
        raise HTTPException(401, detail={"kind": "unauthenticated", "message": "Подпись JWT не совпадает."})

    now = int(time.time())
    exp = payload.get("exp")
    if exp is not None and now > int(exp) + leeway:
        raise HTTPException(401, detail={"kind": "unauthenticated", "message": "Срок действия токена истёк."})
    nbf = payload.get("nbf")
    if nbf is not None and now + leeway < int(nbf):
        raise HTTPException(401, detail={"kind": "unauthenticated", "message": "Токен ещё не действует."})
    if not payload.get("role"):
        raise HTTPException(401, detail={"kind": "unauthenticated",
                                        "message": "В токене нет claim «role»."})

    # Издатель и аудитория. Пока переменные не заданы, проверка не выполняется
    # (обратная совместимость с токенами, выпущенными раньше), но как только
    # JWT_ISSUER/JWT_AUDIENCE заданы — токен чужого издателя не принимается.
    # Без этого токен, выпущенный любым сервисом с тем же секретом (второй контур,
    # старый стенд, копия .env), молча считался нашим.
    expected_iss = os.getenv("JWT_ISSUER", "").strip()
    expected_aud = os.getenv("JWT_AUDIENCE", "").strip()
    if expected_iss and str(payload.get("iss", "")) != expected_iss:
        raise HTTPException(401, detail={"kind": "unauthenticated",
                                         "message": "Токен выпущен не нашим издателем."})
    if expected_aud:
        aud = payload.get("aud")
        audiences = aud if isinstance(aud, list) else [aud]
        if expected_aud not in [str(a) for a in audiences if a is not None]:
            raise HTTPException(401, detail={"kind": "unauthenticated",
                                             "message": "Токен выпущен для другой аудитории."})
    return payload


def issue_jwt(payload: dict[str, Any], *, secret: str | None = None, ttl: int = 3600,
              alg: str = "HS256") -> str:
    """Выпустить токен (используется аутентификацией и тестами)."""
    secret = secret if secret is not None else jwt_secret()
    if not secret:
        raise RuntimeError("JWT_SECRET не настроен")
    body = dict(payload)
    body.setdefault("iat", int(time.time()))
    if os.getenv("JWT_ISSUER", "").strip():
        body.setdefault("iss", os.getenv("JWT_ISSUER", "").strip())
    if os.getenv("JWT_AUDIENCE", "").strip():
        body.setdefault("aud", os.getenv("JWT_AUDIENCE", "").strip())
    body["exp"] = int(time.time()) + ttl
    header_b64 = _b64url_encode(json.dumps({"alg": alg, "typ": "JWT"}).encode())
    payload_b64 = _b64url_encode(json.dumps(body, separators=(",", ":")).encode())
    signature = hmac.new(secret.encode(), f"{header_b64}.{payload_b64}".encode(), _ALGORITHMS[alg]).digest()
    return f"{header_b64}.{payload_b64}.{_b64url_encode(signature)}"


# ---------------------------------------------------------------------------
# Заголовки безопасности
# ---------------------------------------------------------------------------
SECURITY_HEADERS: dict[str, str] = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "X-Permitted-Cross-Domain-Policies": "none",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Permissions-Policy": "geolocation=(), microphone=(), camera=()",
}

# Публичные страницы Link-in-Bio: их встраивают в профили соцсетей, поэтому
# frame-ancestors ограничен, но не запрещён полностью.
BIO_CSP = ("default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
           "script-src 'none'; frame-ancestors 'self' https://*.vk.com https://*.telegram.org")


def security_headers_for(path: str) -> dict[str, str]:
    headers = dict(SECURITY_HEADERS)
    headers["Content-Security-Policy"] = BIO_CSP if path.startswith("/bio/") else (
        "default-src 'none'; frame-ancestors 'none'")
    return headers


def cors_settings() -> dict[str, Any]:
    """CORS: только домены из ``CORS_ORIGINS``; ``*`` несовместим с credentials."""
    origins = cors_origins()
    if "*" in origins:
        return {"allow_origins": origins, "allow_credentials": False,
                "allow_methods": ["*"], "allow_headers": ["*"]}
    return {
        "allow_origins": list(origins),
        "allow_credentials": True,
        "allow_methods": ["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        "allow_headers": ["Authorization", "Content-Type", "X-Workspace-Id"],
        "expose_headers": ["Retry-After", "X-RateLimit-Remaining"],
        "max_age": 600,
    }


# ---------------------------------------------------------------------------
# Вебхуки
# ---------------------------------------------------------------------------
def _ip_in(ip: str | None, networks: tuple[str, ...]) -> bool:
    if not ip:
        return False
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    for net in networks:
        try:
            if addr in ipaddress.ip_network(net, strict=False):
                return True
        except ValueError:
            continue
    return False


def _hmac_sha256(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def verify_yookassa(payload: dict[str, Any], *, raw_body: bytes, signature: str | None,
                    client_ip: str | None) -> None:
    """Платёжное уведомление: подпись HMAC-SHA256 и/или официальная подсеть ЮKassa.

    Ранее эндпоинт принимал любой POST — поддельный запрос активировал платный тариф.
    Теперь: подпись обязательна, если настроен ``YOOKASSA_WEBHOOK_SECRET``; иначе
    пропускаются только запросы из подсетей ЮKassa. Ни то, ни другое — 403.
    """
    secret = yookassa_webhook_secret()
    if secret:
        provided = (signature or "").strip().lower()
        expected = _hmac_sha256(secret, raw_body)
        if not provided or not hmac.compare_digest(expected, provided):
            raise HTTPException(403, detail={"kind": "webhook_rejected",
                                            "message": "Подпись уведомления ЮKassa неверна."})
        return
    if _ip_in(client_ip, YOOKASSA_NETWORKS):
        return
    raise HTTPException(403, detail={
        "kind": "webhook_rejected",
        "message": "Уведомление ЮKassa не подтверждено: нет подписи и IP вне подсетей ЮKassa.",
        "hint": "Задайте YOOKASSA_WEBHOOK_SECRET или принимайте уведомления через доверенный прокси.",
    })


def verify_channel_webhook(channel: str, *, raw_body: bytes, headers: dict[str, str]) -> None:
    """Вебхуки соцсетей: общий секрет (HMAC) либо подпись конкретной платформы.

    * общий секрет — ``X-Webhook-Signature: sha256=<hex>``;
    * Telegram — ``X-Telegram-Bot-Api-Secret-Token``;
    * VK Callback — ``X-VK-Sign``;
    * Discord — ``X-Signature-Ed25519`` (проверяется на стороне шлюза).
    """
    lowered = {k.lower(): v for k, v in headers.items()}

    shared = os.getenv("WEBHOOK_SHARED_SECRET", "")
    if shared:
        provided = lowered.get("x-webhook-signature", "").replace("sha256=", "").strip()
        expected = _hmac_sha256(shared, raw_body)
        if provided and hmac.compare_digest(expected, provided):
            return

    tg_secret = os.getenv("TELEGRAM_WEBHOOK_SECRET", "")
    if channel == "telegram" and tg_secret:
        if hmac.compare_digest(lowered.get("x-telegram-bot-api-secret-token", ""), tg_secret):
            return

    vk_secret = os.getenv("VK_WEBHOOK_SECRET", "")
    if channel == "vk" and vk_secret:
        if hmac.compare_digest(lowered.get("x-vk-sign", ""), vk_secret):
            return

    if not shared and not (channel == "telegram" and tg_secret) and not (channel == "vk" and vk_secret):
        raise HTTPException(503, detail={
            "kind": "webhook_not_configured",
            "message": "Приём вебхуков не настроен: задайте WEBHOOK_SHARED_SECRET "
                       "(или секрет конкретной платформы).",
        })
    raise HTTPException(403, detail={"kind": "webhook_rejected",
                                    "message": "Подпись вебхука отсутствует или неверна."})
