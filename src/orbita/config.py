"""Конфигурация из переменных окружения (12-factor).

Правило проекта: **безопасные значения по умолчанию**. Флаги, которые ослабляют защиту
(демо-роли, нестрогие вебхуки), включены только явной переменной окружения и читаются
в момент вызова, а не на импорте — иначе тесты и рантайм расходятся.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).lower() in {"1", "true", "yes", "on"}


def _csv(name: str, default: tuple[str, ...] = ()) -> tuple[str, ...]:
    raw = os.getenv(name)
    if not raw:
        return default
    return tuple(part.strip() for part in raw.split(",") if part.strip())


#: Ключи Redis, к которым имеют право подключаться роли. Права разделены не «для
#: красоты»: утёкший ключ API-процесса не должен давать доступ к состоянию очереди
#: публикаций, а скомпрометированный воркер — возможность выпускать/гасить сессии.
REDIS_ROLE_PATTERNS: dict[str, tuple[str, ...]] = {
    # auth:state:* — одноразовые OAuth state и 2FA challenge (см. orbita.auth)
    # user_sessions:* — индекс «все сессии пользователя» для logout-everywhere
    "api": ("sess:*", "user_sessions:*", "state:*", "auth:state:*", "rl:*", "chanrl:*",
            "arq:*"),
    "worker": ("arq:*", "chanrl:*", "pub:*", "rl:*"),
}


def redis_role() -> str:
    """Роль процесса для прав в Redis: ``api`` (по умолчанию) или ``worker``."""
    role = os.getenv("ORBITA_REDIS_ROLE", "api").strip().lower()
    return role if role in REDIS_ROLE_PATTERNS else "api"


def redis_url(role: str | None = None) -> str:
    """URL Redis с учётными данными **роли**, если они заданы.

    Базовая переменная — ``REDIS_URL`` (общий пароль). Если заданы
    ``REDIS_API_PASSWORD`` / ``REDIS_WORKER_PASSWORD``, пароль и пользователь подменяются
    на роль-специфичные: приложение работает под ``orbita_api`` / ``orbita_worker`` с
    ограниченным набором ключей (см. ``REDIS_ROLE_PATTERNS`` и ``scripts/dev_stack.sh``).
    Без этих переменных поведение прежнее — один общий пользователь, что допустимо
    только в разработке.
    """
    base = os.getenv("REDIS_URL", "").strip()
    role = role or redis_role()
    password = os.getenv(f"REDIS_{role.upper()}_PASSWORD", "").strip()
    if not base or not password:
        return base
    from urllib.parse import quote, urlsplit, urlunsplit

    parts = urlsplit(base)
    host = parts.hostname or "127.0.0.1"
    port = parts.port or 6379
    user = f"orbita_{role}"
    netloc = f"{user}:{quote(password, safe='')}@{host}:{port}"
    return urlunsplit((parts.scheme or "redis", netloc, parts.path, parts.query, ""))


@dataclass(frozen=True)
class Settings:
    env: str = field(default_factory=lambda: os.getenv("APP_ENV", "dev"))
    dry_run: bool = field(default_factory=lambda: _bool("DRY_RUN", True))
    database_url: str = field(default_factory=lambda: os.getenv("DATABASE_URL", ""))
    redis_url: str = field(default_factory=lambda: redis_url("api"))
    llm_router_default: str = field(default_factory=lambda: os.getenv("LLM_ROUTER_DEFAULT", "gigachat"))

    telegram_bot_token: str = field(default_factory=lambda: os.getenv("TELEGRAM_BOT_TOKEN", ""))
    vk_service_key: str = field(default_factory=lambda: os.getenv("VK_SERVICE_KEY", ""))
    vk_community_token: str = field(default_factory=lambda: os.getenv("VK_COMMUNITY_TOKEN", ""))
    max_bot_token: str = field(default_factory=lambda: os.getenv("MAX_BOT_TOKEN", ""))
    x_bearer_token: str = field(default_factory=lambda: os.getenv("X_BEARER_TOKEN", ""))

    # ОРД: получение erid по API
    ord_provider: str = field(default_factory=lambda: os.getenv("ORD_PROVIDER", "mock"))
    yandex_ord_token: str = field(default_factory=lambda: os.getenv("YANDEX_ORD_TOKEN", ""))
    vk_ord_token: str = field(default_factory=lambda: os.getenv("VK_ORD_TOKEN", ""))

    # Короткие ссылки
    link_domain: str = field(default_factory=lambda: os.getenv("LINK_DOMAIN", "posiz.ru"))


settings = Settings()

# ---------- то, что обязано читаться в момент вызова ----------
# Демо-роли через заголовок X-Actor-Role. По умолчанию ВЫКЛЮЧЕНО: в проде роль
# приходит только из подписанного JWT. Включать только для локальной демонстрации.


def dry_run() -> bool:
    """DRY_RUN читается в момент вызова, а не при импорте: тесты и переключение на лету."""
    return _bool("DRY_RUN", True)


def llm_mode() -> str:
    """``live`` только когда есть и ключ, и роутер, и DRY_RUN=0."""
    if _bool("DRY_RUN", True):
        return "simulated"
    has_router = bool(os.getenv("LLM_ROUTER_URL") or os.getenv("LLM_BASE_URL"))
    has_key = bool(os.getenv("LLM_ROUTER_KEY") or os.getenv("DEEPSEEK_API_KEY")
                   or os.getenv("GEMINI_API_KEY"))
    return "live" if has_router and has_key else "simulated"


def demo_roles_enabled() -> bool:
    """Демо-роли (роль из заголовка) — только вне прода.

    Флаг живёт в окружении, а окружение ошибается: оставленный в проде
    ``RBAC_DEMO_ROLES=true`` превращает любой анонимный запрос в запрос владельца
    (``X-Actor-Role: owner``). Поэтому в ``APP_ENV=prod`` флаг не действует вообще,
    независимо от значения, и об этом пишется предупреждение при старте.
    """
    if not _bool("RBAC_DEMO_ROLES", False):
        return False
    if os.getenv("APP_ENV", "dev").lower() in {"prod", "production"}:
        return False
    return True


def jwt_secret() -> str:
    return os.getenv("JWT_SECRET", "")


def jwt_algorithm() -> str:
    return os.getenv("JWT_ALG", "HS256").upper()


def data_layer() -> str:
    """``memory`` (дефолт, каркас и тесты) или ``db`` (PostgreSQL через SQLAlchemy)."""
    explicit = os.getenv("DATA_LAYER", "").strip().lower()
    if explicit in {"memory", "db"}:
        return explicit
    return "db" if settings.database_url else "memory"


def cors_origins() -> tuple[str, ...]:
    return _csv("CORS_ORIGINS", (
        "http://localhost:8000", "http://127.0.0.1:8000",
        "http://localhost:5173", "http://127.0.0.1:5173",
    ))


def rate_limit_enabled() -> bool:
    return _bool("RATE_LIMIT_ENABLED", True)


def rate_limit_trust_proxy() -> bool:
    """Брать IP из ``X-Forwarded-For`` (только если приложение стоит за своим nginx)."""
    return _bool("RATE_LIMIT_TRUST_PROXY", False)


# ---------- вебхуки ----------
def yookassa_webhook_secret() -> str:
    return os.getenv("YOOKASSA_WEBHOOK_SECRET", "")


def webhook_shared_secret() -> str:
    return os.getenv("WEBHOOK_SHARED_SECRET", "")


def telegram_webhook_secret() -> str:
    return os.getenv("TELEGRAM_WEBHOOK_SECRET", "")


def vk_webhook_secret() -> str:
    return os.getenv("VK_WEBHOOK_SECRET", "")


# Официальные подсети уведомлений ЮKassa (https://yookassa.ru/developers/using-api/webhooks)
YOOKASSA_NETWORKS: tuple[str, ...] = (
    "185.71.76.0/27", "185.71.77.0/27",
    "77.75.153.0/25", "77.75.154.128/25",
    "77.75.156.11/32", "77.75.156.35/32",
)


# ---------- секреты каналов ----------
def token_encryption_key() -> str:
    """Ключ AES-256-GCM: base64/hex на 32 байта либо парольная фраза (PBKDF2)."""
    return os.getenv("TOKEN_ENCRYPTION_KEY", "")
