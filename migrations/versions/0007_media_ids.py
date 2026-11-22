"""Вложения публикации: `publications.media_ids`

Revision ID: 0007_media_ids
Revises: 0006_rls_strict
Create Date: 2026-11-18

Медиа-конвейер (ADR 0023) генерирует карточки и обложки, но до этой миграции
публикация не могла «взять» артефакт с собой: адаптеры умели загружать файлы
(`PublishInput.media_urls`), а заполнять этот список было некому — поле просто
не доезжало до воркера.

Хранится **id артефакта**, а не URL, и это осознанно: ссылка подписана и живёт час,
а публикация может ждать слота несколько дней. Ссылка собирается в момент отправки,
поэтому «сгоревшая» ссылка не может остаться в базе.

Тип `jsonb` (а не `text[]`): массивом id мы пользуемся только как списком, зато
JSON-тип одинаково работает и в PostgreSQL, и в SQLite-режиме тестов.
"""
from __future__ import annotations

from alembic import op

revision = "0007_media_ids"
down_revision = "0006_rls_strict"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE publications
          ADD COLUMN IF NOT EXISTS media_ids jsonb NOT NULL DEFAULT '[]'::jsonb
        """
    )
    # Поиск «где используется артефакт» перед уборкой по TTL: GIN по jsonb-массиву.
    op.execute(
        """
        DO $$
        BEGIN
          BEGIN
            CREATE INDEX IF NOT EXISTS publications_media_ids_idx
              ON publications USING gin (media_ids);
          EXCEPTION WHEN others THEN
            -- GIN по jsonb недоступен (или колонка уже проиндексирована иначе):
            -- уборка медиа работает и без индекса, ронять миграцию из-за этого нельзя
            NULL;
          END;
        END $$;
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS publications_media_ids_idx")
    op.execute("ALTER TABLE publications DROP COLUMN IF EXISTS media_ids")
