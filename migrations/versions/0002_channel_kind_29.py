"""channel_kind: 29 каналов (синхронизация ENUM с catalog.py)

Revision ID: 0002_channel_kind_29
Revises: 0001_baseline
Create Date: 2026-11-17

Проблема: в ENUM было 15 значений, а карта площадок знает 29. Сохранение публикации
для Дзена, TenChat, Pinterest падало с ``invalid input value for enum channel_kind``.

``ALTER TYPE … ADD VALUE IF NOT EXISTS`` нельзя выполнять в транзакции вместе с
использованием нового значения — поэтому миграция ставит ``autocommit_block``.
"""
from __future__ import annotations

from alembic import op

revision = "0002_channel_kind_29"
down_revision = "0001_baseline"
branch_labels = None
depends_on = None

NEW_KINDS = (
    "dzen", "tenchat", "vk_video", "rutube", "vcru", "habr", "pinterest",
    "yandex_business", "twogis", "discord", "twitch", "kick", "email", "webpush",
)


def upgrade() -> None:
    with op.get_context().autocommit_block():
        for kind in NEW_KINDS:
            op.execute(f"ALTER TYPE channel_kind ADD VALUE IF NOT EXISTS '{kind}'")


def downgrade() -> None:
    # Удалить значение из ENUM в PostgreSQL нельзя без пересоздания типа.
    # Осознанно оставляем значения: они безвредны и могут быть использованы данными.
    pass
