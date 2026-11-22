"""Общая настройка тестов.

Важно: ``RBAC_DEMO_ROLES=1`` включается **только здесь**, для автотестов каркаса.
В рантайме демо-заголовок ``X-Actor-Role`` по умолчанию выключен, и роль приходит
исключительно из подписанного JWT (см. ``tests/test_security.py``).
"""
from __future__ import annotations

import os

os.environ.setdefault("RBAC_DEMO_ROLES", "1")
os.environ.setdefault("WEBHOOK_SHARED_SECRET", "test-shared-secret")
os.environ.setdefault("YOOKASSA_WEBHOOK_SECRET", "test-yookassa-secret")
os.environ.setdefault("TELEGRAM_WEBHOOK_SECRET", "test-tg-secret")
os.environ.setdefault("VK_WEBHOOK_SECRET", "test-vk-secret")
os.environ.setdefault("TOKEN_ENCRYPTION_KEY", "test-token-encryption-key-32-bytes!")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret")
os.environ.setdefault("DATA_LAYER", "memory")

import pytest  # noqa: E402

from orbita import ratelimit  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    """Каждый тест начинает с чистых счётчиков, иначе лимиты протекают между тестами."""
    ratelimit.limiter.reset()
    yield
    ratelimit.limiter.reset()
