"""Шифрование секретов каналов: AES-256-GCM, ключ вне кода.

Токены сообществ (VK, Telegram, MAX, X) дают право публикации от имени клиента —
их нельзя держать в открытом виде ни в памяти, ни в дампе БД. Формат шифротекста:
``v1:<base64(nonce|tag|ciphertext)>``; версия нужна для ротации ключей (``key_version``).

Ключ берётся из ``TOKEN_ENCRYPTION_KEY``:
* base64/hex на 32 байта — используется как есть;
* иначе строка трактуется как парольная фраза и растягивается PBKDF2-HMAC-SHA256
  с фиксированной солью приложения (ключ выводится детерминированно).

Без ключа модуль работает в dev-режиме: генерирует эфемерный ключ процесса и
предупреждает один раз. В ``APP_ENV=prod`` отсутствие ключа — ошибка, а не догадка.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import logging
import os
import secrets

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .config import token_encryption_key

log = logging.getLogger("orbita.crypto")
VERSION = "v1"
_NONCE_BYTES = 12
_SALT = b"orbita.token.vault.v1"
_warned = False
_ephemeral: bytes | None = None


class TokenVaultError(RuntimeError):
    """Секрет нельзя расшифровать: другой ключ, повреждённый шифротекст."""


def _derive(secret: str) -> bytes:
    if not secret:
        raise TokenVaultError("TOKEN_ENCRYPTION_KEY не задан")
    raw: bytes | None = None
    for decoder in (base64.b64decode, binascii.unhexlify):
        try:
            candidate = decoder(secret, validate=True) if decoder is base64.b64decode else decoder(secret)
            if len(candidate) == 32:
                raw = candidate
                break
        except Exception:                             # noqa: BLE001 — не base64/hex, это пароль
            continue
    if raw is None:
        raw = hashlib.pbkdf2_hmac("sha256", secret.encode(), _SALT, 200_000, dklen=32)
    return raw


def _key() -> bytes:
    global _warned, _ephemeral
    configured = token_encryption_key()
    if configured:
        return _derive(configured)
    if os.getenv("APP_ENV", "dev").lower() in {"prod", "production"}:
        raise TokenVaultError(
            "TOKEN_ENCRYPTION_KEY обязателен в проде: токены каналов нельзя хранить без шифрования")
    if _ephemeral is None:
        _ephemeral = secrets.token_bytes(32)
    if not _warned:
        log.warning("TOKEN_ENCRYPTION_KEY не задан — используется эфемерный ключ процесса (только dev)")
        _warned = True
    return _ephemeral


def is_configured() -> bool:
    return bool(token_encryption_key())


def encrypt(plaintext: str) -> str:
    """Зашифровать токен. Возвращает строку для колонки ``token_ciphertext``."""
    key = _key()
    nonce = secrets.token_bytes(_NONCE_BYTES)
    sealed = AESGCM(key).encrypt(nonce, plaintext.encode(), VERSION.encode())
    return f"{VERSION}:{base64.b64encode(nonce + sealed).decode()}"


def decrypt(ciphertext: str) -> str:
    if not ciphertext:
        raise TokenVaultError("пустой шифротекст")
    if not ciphertext.startswith(VERSION + ":"):
        # значение без версии — это не наш формат (например, старый plaintext)
        raise TokenVaultError("неизвестная версия шифротекста")
    blob = base64.b64decode(ciphertext.split(":", 1)[1])
    nonce, sealed = blob[:_NONCE_BYTES], blob[_NONCE_BYTES:]
    try:
        return AESGCM(_key()).decrypt(nonce, sealed, VERSION.encode()).decode()
    except InvalidTag as exc:
        raise TokenVaultError("шифротекст повреждён или ключ не тот") from exc


def fingerprint(plaintext: str) -> str:
    """Стабильный «отпечаток» для логов и UI: последние 4 символа + hash8."""
    digest = hashlib.sha256(plaintext.encode()).hexdigest()[:8]
    tail = plaintext[-4:] if len(plaintext) >= 4 else "****"
    return f"…{tail}#{digest}"


def mask(ciphertext: str) -> str:
    """Безопасное представление секрета для API: без расшифровки."""
    return "•••" if ciphertext else ""
