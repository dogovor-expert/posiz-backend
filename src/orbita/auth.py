"""Аутентификация: магическая ссылка, OAuth VK ID / Яндекс ID, сессии, refresh, 2FA.

Что боевое, а что демо — видно в ответе каждого метода (``delivery.transport``,
``mode``), а не только в логах:

* **magic-link** — письмо уходит реальным транспортом (``mailer``: Resend/UniSender/SMTP).
  Ссылка в ответе появляется только в демо-режиме (``RBAC_DEMO_ROLES`` или ``APP_ENV=dev``),
  иначе её место занимает честный статус доставки;
* **verify** — одноразовый токен → пара ``access_token`` (JWT 15 мин) + ``refresh_token``
  (хэш в Redis, ротация, детект повторного использования — ``sessions``);
* **OAuth** — обмен ``code`` на токен реальным POST в token-endpoint провайдера
  (VK ID и Яндекс ID), затем профиль → ``userinfo``;
* **2FA** — TOTP-секрет в сессии, backup-коды;
* **refresh / logout** — ``POST /auth/refresh`` и ``POST /auth/logout`` в api_v3.
"""
from __future__ import annotations

import json

import base64
import hashlib
import hmac
import struct
import logging
import os
import time
import secrets
from datetime import datetime, timedelta, timezone

import httpx

from . import accounts, mailer, persistence, rbac, sessions

log = logging.getLogger("orbita.auth")

MAGIC: dict[str, dict] = {}
SESSIONS: dict[str, dict] = {}          # кэш процессных сессий (для /me и UI)
INVITES: dict[str, dict] = {}           # email → приглашение с явной ролью
MEMBERS: dict[str, dict] = {}           # email → членство в workspace
OAUTH_STATES: dict[str, dict] = {}      # одноразовые state для OAuth (10 минут)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

# OAuth-провайдеры: authorize + token обмен
OAUTH = {
    "vk_id": {
        "authorize_url": "https://id.vk.com/authorize",
        "token_url": "https://id.vk.com/oauth2/auth",
        "userinfo_url": "https://id.vk.com/oauth2/user_info",
        "client_id_env": "VK_ID_CLIENT_ID",
        "client_secret_env": "VK_ID_CLIENT_SECRET",
        "scopes": ["email", "groups"],
        "note": "VK ID: вход в 1 клик, обязателен для РФ-конверсии",
        "code_field": "code",
    },
    "yandex_id": {
        "authorize_url": "https://oauth.yandex.ru/authorize",
        "token_url": "https://oauth.yandex.ru/token",
        "userinfo_url": "https://login.yandex.ru/info?format=json",
        "client_id_env": "YANDEX_ID_CLIENT_ID",
        "client_secret_env": "YANDEX_ID_CLIENT_SECRET",
        "scopes": ["login:email"],
        "note": "Яндекс ID: почта и профиль",
        "code_field": "code",
    },
}

REDIRECT_DEFAULT = "https://app.posiz.ru/auth/callback"


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def demo_mode() -> bool:
    if _env("APP_ENV", "dev") in ("dev", "test", "local"):
        return True
    return _env("RBAC_DEMO_ROLES", "0") in ("1", "true", "True")


def transport_status() -> dict:
    return {"mail": mailer.status(), "sessions": sessions.storage(),
            "demo_mode": demo_mode(), "oauth": {
                name: {"configured": bool(_env(cfg["client_id_env"]) and _env(cfg["client_secret_env"]))}
                for name, cfg in OAUTH.items()}}


# ---------------------------------------------------------------- magic-link
def request_magic(email: str, workspace_id: str | None = None, ttl_minutes: int = 15, *,
                  role: str | None = None, client: httpx.Client | None = None) -> dict:
    """Отправить ссылку входа.

    Роль и workspace НЕ назначаются «по умолчанию owner/ws_de»: они определяются тем,
    что известно о пользователе (приглашение в workspace, членство). Человек, впервые
    вошедший по ссылке, получает ровно те права, которые ему выдали, а не владельца
    чужого пространства — иначе достаточно знать свой e-mail, чтобы стать владельцем.
    """
    token = secrets.token_urlsafe(24)
    # Workspace не «ws_de по умолчанию»: для нового адреса создаётся собственный
    # tenant + workspace + membership(owner). Иначе все клиенты оказывались владельцами
    # одного демо-пространства и видели данные друг друга (P0 второго аудита).
    onboarding = None
    if workspace_id is None:
        onboarding = accounts.register(email)
        workspace_id = onboarding["workspace"]["id"]
    assigned_role = role or resolve_role(email, workspace_id)
    MAGIC[token] = {"email": email, "workspace_id": workspace_id, "role": assigned_role,
                    "expires_at": datetime.now(timezone.utc) + timedelta(minutes=ttl_minutes)}
    base = _env("APP_PUBLIC_URL", "https://app.posiz.ru")
    link = f"{base}/auth/verify?token={token}"
    text, html = mailer.magic_link_text(link, ttl_minutes)
    delivery = mailer.send(email, "Вход в Posiz", text, html, client=client)
    result = {"sent": delivery.get("sent", False), "email": email,
              "role": assigned_role, "workspace_id": workspace_id,
              "workspace_created": bool(onboarding and onboarding.get("is_new")),
              "ttl_minutes": ttl_minutes, "delivery": delivery}
    if demo_mode() and not delivery.get("sent"):
        result["magic_link"] = link            # только для локального стенда
        result["demo_token"] = token
    return result


def resolve_role(email: str, workspace_id: str | None) -> str:
    """Роль пользователя в workspace.

    Порядок: приглашение → существующее членство → первый вход создаёт **свой** workspace.
    «Просто вошёл» никогда не даёт доступ к чужому пространству.
    """
    email_key = (email or "").strip().lower()
    invite = INVITES.get(email_key)
    if invite and invite["workspace_id"] == workspace_id:
        return invite["role"]
    member = MEMBERS.get(email_key)
    if member and member["workspace_id"] == workspace_id:
        return member["role"]
    # Нет приглашения и нет членства: это владелец СВОЕГО нового пространства,
    # а не общего демо-workspace. Проверяется в `accounts.register`.
    personal = accounts.workspace_for(email_key) if email_key else None
    return "owner" if personal == workspace_id else "editor"


def invite_member(email: str, workspace_id: str, role: str = "copywriter",
                  invited_by: str = "owner") -> dict:
    """Приглашение с явной ролью. Роль проверяется по матрице RBAC, а не берётся на слово."""
    role = (role or "").strip().lower()
    if role not in rbac.ROLE_PERMISSIONS:
        return {"error": "неизвестная роль", "role": role,
                "allowed": sorted(rbac.ROLE_PERMISSIONS)}
    if role == "owner":
        return {"error": "второго владельца назначает действующий владелец в панели",
                "role": role}
    INVITES[(email or "").strip().lower()] = {"email": email.lower(), "workspace_id": workspace_id,
                                              "role": role, "invited_by": invited_by,
                                              "created_at": _now()}
    return {"invited": True, "email": email.lower(), "workspace_id": workspace_id, "role": role}


def verify_magic(token: str, *, ip: str | None = None) -> dict:
    """Обменять одноразовый токен на пару access/refresh."""
    row = MAGIC.get(token)
    if not row:
        return {"error": "токен не найден или уже использован"}
    expires = row["expires_at"]
    if isinstance(expires, str):
        expires = datetime.fromisoformat(expires)
    if expires < datetime.now(timezone.utc):
        return {"error": "токен истёк"}
    del MAGIC[token]

    user_id = f"u_{hashlib.sha256(row['email'].encode()).hexdigest()[:10]}"
    platform_admin = _platform_admin_for(user_id, row["email"])
    tokens = sessions.issue(user_id, row["email"], role=row["role"],
                            workspace_id=row["workspace_id"], platform_admin=platform_admin)
    session = {
        "session_id": tokens["session_id"], "email": row["email"], "role": row["role"],
        "workspace_id": row["workspace_id"], "user_id": user_id,
        "is_platform_admin": platform_admin,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "expires_at": (datetime.now(timezone.utc) + timedelta(days=30)).isoformat(),
    }
    INVITES.pop(row["email"].strip().lower(), None)
    MEMBERS.setdefault(row["email"].strip().lower(),
                       {"workspace_id": row["workspace_id"], "role": row["role"]})

    if two_factor_enabled(user_id):
        # Пароль/ссылка подтвердили только первый фактор: access/refresh не выдаются,
        # пока не введён код. Раньше 2FA можно было «включить», но она ни на что не влияла.
        return _issue_challenge(user_id, row["email"], row["role"], row["workspace_id"])
    SESSIONS[tokens["session_id"]] = session
    return {**session, **tokens}


def _platform_admin_for(user_id: str, email: str) -> bool:
    """Флаг платформенного админа — только из БД (или явного env-списка в демо)."""
    db = persistence.database()
    if db is not None:
        try:
            return db.user_is_platform_admin(user_id)
        except Exception as exc:                       # noqa: BLE001
            log.warning("auth: не смог прочитать флаг platform_admin: %s", exc)
    allow = {e.strip().lower() for e in _env("PLATFORM_ADMINS", "").split(",") if e.strip()}
    return email.lower() in allow


def refresh(refresh_token: str) -> dict:
    return sessions.refresh(refresh_token)


def logout(refresh_token: str | None = None, session_id: str | None = None) -> dict:
    result = {"revoked": False}
    if refresh_token:
        result = sessions.revoke(refresh_token)
    if session_id:
        SESSIONS.pop(session_id, None)
        result["session_removed"] = True
    return result


# ---------------------------------------------------------------- одноразовые состояния
#: Одноразовые состояния (OAuth `state`, 2FA `challenge`) живут в Redis, если он настроен,
#: иначе в памяти процесса. В памяти они видны только одному процессу: при нескольких
#: репликах API вход через OAuth и второй фактор ломаются — поэтому в проде нужен Redis.
_STATE_PREFIX = "auth:state:"
_RAW: dict[str, dict] = {}          # память процесса: ключ → payload


def _state_redis():
    from .config import redis_url as _redis_url

    url = _redis_url().strip()
    if not url:
        return None
    try:
        import redis  # noqa: PLC0415 — необязательная зависимость

        client = redis.Redis.from_url(url, decode_responses=True,
                                      socket_connect_timeout=0.5, socket_timeout=0.5)
        client.ping()
        return client
    except Exception:                     # noqa: BLE001 — нет Redis: остаёмся в памяти
        return None


def state_backend() -> str:
    """Где живут одноразовые состояния: `redis` или `memory` (видно в /auth/status)."""
    return "redis" if _state_redis() is not None else "memory"


def _state_put(key: str, payload: dict, ttl_seconds: int) -> None:
    client = _state_redis()
    if client is not None:
        client.setex(f"{_STATE_PREFIX}{key}", ttl_seconds, json.dumps(payload))
    else:
        _RAW[key] = payload


def _state_pop(key: str) -> dict | None:
    """Забрать состояние ровно один раз (в Redis — атомарно через GETDEL)."""
    client = _state_redis()
    if client is not None:
        raw = client.getdel(f"{_STATE_PREFIX}{key}")
        return json.loads(raw) if raw else None
    return _RAW.pop(key, None)


def oauth_url(provider: str, redirect_uri: str = REDIRECT_DEFAULT, *,
              browser_binding: str | None = None) -> dict:
    cfg = OAUTH.get(provider)
    if not cfg:
        return {"error": f"провайдер {provider} не поддерживается", "available": list(OAUTH)}
    state = secrets.token_urlsafe(24)
    # state привязан к браузеру (cookie) и к конкретному redirect_uri; живёт 10 минут
    _state_put(state, {"provider": provider, "redirect_uri": redirect_uri,
                       "binding": browser_binding, "kind": "oauth",
                       "created_at": datetime.now(timezone.utc).isoformat()}, 600)
    OAUTH_STATES[state] = {"provider": provider, "redirect_uri": redirect_uri,
                           "binding": browser_binding}     # «последние» — для диагностики
    client_id = _env(cfg["client_id_env"], "orbita")
    url = (f"{cfg['authorize_url']}?response_type=code&client_id={client_id}"
           f"&redirect_uri={redirect_uri}&scope={' '.join(cfg['scopes'])}&state={state}")
    return {"provider": provider, "state": state, "url": url, "note": cfg["note"],
            "configured": bool(_env(cfg["client_id_env"]) and _env(cfg["client_secret_env"]))}


def exchange_code(provider: str, code: str, *, redirect_uri: str = REDIRECT_DEFAULT,
                  state: str | None = None, browser_binding: str | None = None,
                  client: httpx.Client | None = None) -> dict:
    """Обменять ``code`` на токен провайдера и получить профиль.

    ``state`` обязателен: без сверки одноразового значения любой может подсунуть свой
    ``code`` в чужую сессию (CSRF на входе). Проверяется провайдер, срок 10 минут
    и однократность; значение удаляется независимо от результата.
    """
    cfg = OAUTH.get(provider)
    if not cfg:
        return {"error": f"провайдер {provider} не поддерживается"}
    client_id = _env(cfg["client_id_env"])
    client_secret = _env(cfg["client_secret_env"])
    if not (client_id and client_secret):
        return {"error": f"{provider}: не заданы {cfg['client_id_env']}/{cfg['client_secret_env']}",
                "code": "oauth_not_configured"}

    saved = _state_pop(state or "") if state else None
    if saved is not None:
        OAUTH_STATES.pop(state or "", None)
    if not saved:
        return {"error": "state не найден: запрос входа не начинался или ссылка устарела",
                "code": "invalid_state"}
    if saved["provider"] != provider:
        return {"error": "state выдан для другого провайдера", "code": "invalid_state"}
    if saved.get("redirect_uri") and redirect_uri and saved["redirect_uri"] != redirect_uri:
        return {"error": "redirect_uri не совпадает с тем, на который выдавался state",
                "code": "invalid_state"}
    if saved.get("binding") and browser_binding and saved["binding"] != browser_binding:
        return {"error": "state выдан другому браузеру", "code": "invalid_state"}
    created = saved.get("created_at")
    if isinstance(created, str):
        created = datetime.fromisoformat(created)
    age = (datetime.now(timezone.utc) - created).total_seconds() if created else 0
    if age > 600:
        return {"error": "state истёк: начните вход заново", "code": "invalid_state"}

    own = client is None
    http = client or httpx.Client(timeout=20)
    try:
        token_resp = http.post(cfg["token_url"], data={
            "grant_type": "authorization_code", "code": code,
            "client_id": client_id, "client_secret": client_secret,
            "redirect_uri": redirect_uri,
        })
        token_data = token_resp.json() if token_resp.content else {}
        access = token_data.get("access_token")
        if token_resp.status_code >= 400 or not access:
            return {"error": f"{provider}: обмен кода не удался",
                    "detail": token_data.get("error_description") or token_data.get("error"),
                    "status": token_resp.status_code}
        info_resp = http.get(cfg["userinfo_url"],
                             headers={"Authorization": f"OAuth {access}"},
                             params={"client_id": client_id})
        info = info_resp.json() if info_resp.content else {}
        user = info.get("user") if isinstance(info.get("user"), dict) else {}
        email = (info.get("email") or info.get("default_email") or user.get("email")
                 or (user.get("emails") or [None])[0] or "")
        external_id = str(info.get("user_id") or info.get("id") or info.get("sub")
                          or user.get("user_id") or user.get("id") or "")
        user_id = f"u_{hashlib.sha256(f'{provider}:{external_id}'.encode()).hexdigest()[:10]}"
        tokens = sessions.issue(user_id, email or f"{external_id}@{provider}",
                                platform_admin=_platform_admin_for(user_id, email or ""))
        return {"provider": provider, "email": email, "external_id": external_id,
                "user_id": user_id, **tokens}
    except httpx.HTTPError as exc:
        return {"error": f"{provider}: сеть недоступна ({exc.__class__.__name__})"}
    finally:
        if own:
            http.close()


# ---------------------------------------------------------------- 2FA и прочее
def _totp(secret: str, at: int | None = None, step: int = 30, digits: int = 6) -> str:
    """TOTP (RFC 6238) на HMAC-SHA1 — без внешних зависимостей."""
    counter = int((at if at is not None else time.time()) // step)
    key = base64.b32decode(secret + "=" * ((8 - len(secret) % 8) % 8), casefold=True)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(code % 10 ** digits).zfill(digits)


def verify_totp(secret: str, code: str, *, window: int = 1) -> bool:
    """Проверка кода с окном ±1 шаг (компенсация расхождения часов)."""
    now = time.time()
    return any(hmac.compare_digest(_totp(secret, at=now + i * 30), (code or "").strip())
               for i in range(-window, window + 1))


def enable_2fa(session_id: str) -> dict:
    """Включить 2FA: TOTP-секрет + **настоящие** одноразовые backup-коды.

    Раньше ручка возвращала ``"backup_codes": 10`` — число вместо кодов. Пользователь
    считал, что у него есть резервные коды, а восстановить доступ было нечем.
    """
    session = SESSIONS.get(session_id)
    if not session:
        return {"error": "сессия не найдена"}
    secret = base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")
    codes = [f"{secrets.token_hex(2)}-{secrets.token_hex(2)}" for _ in range(10)]
    session["two_factor"] = "totp"
    session["totp_secret"] = secret
    session["backup_codes"] = [hashlib.sha256(c.encode()).hexdigest() for c in codes]
    session["two_factor_confirmed"] = False
    issuer, account = "Posiz", session.get("email", "user")
    return {"two_factor": "totp", "pending_confirmation": True,
            "secret": secret,
            "otpauth": (f"otpauth://totp/{issuer}:{account}?secret={secret}"
                        f"&issuer={issuer}&algorithm=SHA1&digits=6&period=30"),
            # коды показываются один раз; в хранилище лежат только их хэши
            "backup_codes": codes,
            "note": "Сохраните коды: повторно они не показываются. Вход по коду: /auth/2fa/verify."}


def confirm_2fa(session_id: str, code: str) -> dict:
    """Подтвердить включение 2FA и/или пройти второй фактор.

    Backup-код одноразовый: после успешного использования он удаляется из списка.
    """
    session = SESSIONS.get(session_id)
    if not session:
        return {"error": "сессия не найдена"}
    if session.get("two_factor") != "totp":
        return {"error": "2FA не включена"}
    typed = (code or "").strip()

    hashes = session.get("backup_codes") or []
    h = hashlib.sha256(typed.encode()).hexdigest()
    if h in hashes:
        hashes.remove(h)
        session["two_factor_confirmed"] = True
        return {"ok": True, "method": "backup_code", "remaining_codes": len(hashes)}

    if verify_totp(session.get("totp_secret", ""), typed):
        session["two_factor_confirmed"] = True
        return {"ok": True, "method": "totp",
                "remaining_codes": len(session.get("backup_codes") or [])}

    return {"error": "код неверный или истёк", "code": "invalid_totp"}


# ---------------------------------------------------------------- 2FA как обязательный фактор
#: Настройки второго фактора по пользователю: ``{user_id: {secret, backup_codes, confirmed}}``.
TWO_FACTOR: dict[str, dict] = {}
#: Незавершённые входы: ``challenge → {user_id, email, role, workspace_id, expires_at}``.
CHALLENGES: dict[str, dict] = {}
CHALLENGE_TTL_SECONDS = 300


def two_factor_enabled(user_id: str) -> bool:
    return bool((TWO_FACTOR.get(user_id) or {}).get("confirmed"))


def setup_2fa(user_id: str, email: str) -> dict:
    """Начать включение 2FA: секрет и backup-коды. Активация — только после верного кода."""
    secret = base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")
    codes = [f"{secrets.token_hex(2)}-{secrets.token_hex(2)}" for _ in range(10)]
    TWO_FACTOR[user_id] = {"secret": secret, "confirmed": False,
                           "backup_codes": [hashlib.sha256(c.encode()).hexdigest() for c in codes]}
    return {"two_factor": "totp", "secret": secret,
            "otpauth": (f"otpauth://totp/Posiz:{email}?secret={secret}"
                        f"&issuer=Posiz&algorithm=SHA1&digits=6&period=30"),
            "backup_codes": codes,
            "note": "2FA включится после подтверждения кодом: POST /auth/2fa/activate."}


def activate_2fa(user_id: str, code: str) -> dict:
    """Подтвердить включение 2FA реальным кодом из приложения."""
    record = TWO_FACTOR.get(user_id)
    if record is None:
        return {"error": "2FA не настроена: сначала POST /auth/2fa/setup"}
    if not verify_totp(record["secret"], code):
        return {"error": "код неверный или истёк", "code": "invalid_totp"}
    record["confirmed"] = True
    return {"two_factor": "totp", "enabled": True, "remaining_backup_codes": len(record["backup_codes"])}


def _issue_challenge(user_id: str, email: str, role: str, workspace_id: str) -> dict:
    challenge = secrets.token_urlsafe(24)
    payload = {"user_id": user_id, "email": email, "role": role,
               "workspace_id": workspace_id, "kind": "2fa",
               "expires_at": (datetime.now(timezone.utc)
                              + timedelta(seconds=CHALLENGE_TTL_SECONDS)).isoformat()}
    _state_put(challenge, payload, CHALLENGE_TTL_SECONDS)
    CHALLENGES[challenge] = payload          # «последние» — только для диагностики
    return {"two_factor_required": True, "challenge": challenge,
            "methods": ["totp", "backup_code"], "expires_in": CHALLENGE_TTL_SECONDS,
            "message": "Введите код из приложения-аутентификатора или backup-код."}


def verify_second_factor(challenge: str, code: str) -> dict:
    """Завершить вход вторым фактором и выдать пару токенов."""
    row = _state_pop(challenge)
    CHALLENGES.pop(challenge, None)
    if row is None:
        return {"error": "challenge не найден или уже использован", "code": "invalid_challenge"}
    expires = row["expires_at"]
    if isinstance(expires, str):
        expires = datetime.fromisoformat(expires)
    if expires < datetime.now(timezone.utc):
        CHALLENGES.pop(challenge, None)
        return {"error": "время на ввод кода истекло", "code": "challenge_expired"}
    record = TWO_FACTOR.get(row["user_id"]) or {}
    typed = (code or "").strip()
    hashed = hashlib.sha256(typed.encode()).hexdigest()
    codes = record.get("backup_codes") or []
    used_backup = hashed in codes
    if not (used_backup or verify_totp(record.get("secret", ""), typed)):
        return {"error": "код неверный", "code": "invalid_totp"}
    if used_backup:
        codes.remove(hashed)          # backup-код одноразовый
    CHALLENGES.pop(challenge, None)

    platform_admin = _platform_admin_for(row["user_id"], row["email"])
    tokens = sessions.issue(row["user_id"], row["email"], role=row["role"],
                            workspace_id=row["workspace_id"], platform_admin=platform_admin)
    return {"two_factor_verified": True, "method": "backup_code" if used_backup else "totp",
            "user_id": row["user_id"], "email": row["email"], "role": row["role"],
            "workspace_id": row["workspace_id"], **tokens}


def sessions_of(email: str) -> list[dict]:
    """Активные сессии пользователя (панель «Безопасность»)."""
    return [s for s in SESSIONS.values() if s["email"] == email]


def describe_roles() -> dict:
    return rbac.describe()


def onboarding_steps() -> list[dict]:
    return [
        {"id": "brand", "title": "Бренд и голос", "eta_min": 2,
         "hint": "Название, ниша, тон. Можно пропустить и заполнить позже."},
        {"id": "knowledge", "title": "База знаний", "eta_min": 4,
         "hint": "Сайт или PDF — 7 карточек знаний собираются автоматически."},
        {"id": "channels", "title": "Подключение каналов", "eta_min": 3,
         "hint": "VK, Telegram, MAX. Официальные API, без серых схем."},
        {"id": "first_post", "title": "Первый пост", "eta_min": 2,
         "hint": "Черновик по вашей базе знаний за 40 секунд."},
    ]


def trial_policy() -> dict:
    return {"days": 14, "cards": 25, "channels": 2, "card_required": False,
            "note": "Без карты. По окончании — тариф «Старт» или архив данных."}
