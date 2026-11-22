"""Документные коллекции для доменов v3: одна точка записи в PostgreSQL.

Контекст: ADR 0012 перевёл в PostgreSQL шесть сущностей с жёсткой схемой. Остальные домены
(Brand Brain, Инбокс, Link-in-Bio, правила автоответов, мониторинг, A/B) продолжали жить
в памяти и стирались при рестарте контейнера.

Здесь — «strangler»-шаг: домены получают **персистентность сейчас**, не дожидаясь полного
набора типизированных репозиториев. Документы лежат в таблице ``entity_docs``
(``collection`` = имя будущей типизированной таблицы, ``payload jsonb`` = сам объект).
Переход на типизированные таблицы (`bio_pages`, `automation_rules`, …) — механический:
имя коллекции уже совпадает с именем целевой таблицы.

Режимы:
* ``DATA_LAYER=memory`` — обычный словарь/список в процессе (демо и тесты);
* ``DATA_LAYER=db`` — чтение/запись через PostgreSQL, память используется как кэш.

Семантика записи: ``put`` делает upsert, ``all`` возвращает все документы коллекции
в порядке создания, ``delete`` удаляет. Ошибки БД не глотаются: если слой ``db`` выбран,
а БД недоступна, приложение падает явно, а не тихо теряет данные.
"""
from __future__ import annotations

import threading

import logging
from datetime import datetime, timezone
from typing import Any, Iterable

from . import tenancy
from .persistence import database, to_uuid

#: Пространство демо-стенда: в него пишутся сиды, и только в его контексте они видны.
DEMO_WORKSPACE = "ws_de"

log = logging.getLogger("orbita.docs")


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Collection:
    """Коллекция документов: память (кэш) + PostgreSQL (источник правды в режиме db)."""

    def __init__(self, name: str, seed: Iterable[dict] | None = None,
                 id_field: str = "id", workspace_id: str = DEMO_WORKSPACE) -> None:
        self.name = name
        self.id_field = id_field
        self.workspace_id = workspace_id
        self._mem: dict[str, dict] = {}
        self._order: list[str] = []
        self._seeded = False
        self._seed = [dict(item) for item in (seed or [])]
        # Кэш коллекции — общий для процесса, а обработчики FastAPI выполняются в пуле
        # потоков. Без блокировки два одновременных запроса к разным арендаторам могли
        # перетирать `_mem`/`_order` друг друга: список «своих» документов собирался из
        # чужого состояния, а на границе окна — из документов другого арендатора.
        self._lock = threading.RLock()

    # ---------- служебное ----------
    @property
    def db(self):
        return database()

    def _scope(self) -> str | None:
        """Пространство текущего запроса.

        Коллекции обслуживают домены v3 (ключи API, Brand Brain, Инбокс, Bio, A/B, квоты,
        платежи) и раньше **не знали про арендатора**: любая вкладка видела документы всех
        клиентов, а запись без ``workspace_id`` уходила в ``ws_de``. Теперь область берётся
        из контекста запроса; вне контекста (сценарии обслуживания) документы видны всем —
        так же, как в слое БД при платформенной роли.
        """
        return tenancy.scope_workspace()

    def _visible(self, doc: dict) -> bool:
        scope = self._scope()
        if scope is None:
            return True
        return str(doc.get("workspace_id") or DEMO_WORKSPACE) == str(scope)

    def _key(self, doc: dict) -> str:
        value = doc.get(self.id_field) or doc.get("id")
        if value is None:
            raise ValueError(f"{self.name}: документ без поля {self.id_field}")
        return str(value)

    def _ensure_seed(self) -> None:
        """Первый доступ: в режиме db засеять таблицу демо-данными, если она пуста."""
        with self._lock:
            if self._seeded:
                return
            self._seeded = True
        if self.db is None:
            for doc in self._seed:
                self._remember(doc)
            return
        existing = self.db.doc_all(self.name, workspace_id=self.workspace_id)
        if not existing and self._seed:
            for doc in self._seed:
                self.db.doc_put(self.name, self._key(doc), self.workspace_id, doc)
            log.info("docs: коллекция %s засеяна (%s документов)", self.name, len(self._seed))
            return
        for doc in existing:
            self._remember(doc)

    def _remember(self, doc: dict) -> dict:
        with self._lock:
            key = self._key(doc)
            if key not in self._mem:
                self._order.append(key)
            self._mem[key] = doc
            return doc

    # ---------- API ----------
    def all(self) -> list[dict]:
        self._ensure_seed()
        scope = self._scope()
        if self.db is not None:
            rows = self.db.doc_all(self.name, workspace_id=scope)
            with self._lock:
                self._mem.clear()
                self._order.clear()
                for doc in rows:
                    self._remember(doc)
                return [self._mem[k] for k in self._order
                        if k in self._mem and self._visible(self._mem[k])]
        with self._lock:
            return [self._mem[k] for k in self._order
                    if k in self._mem and self._visible(self._mem[k])]

    def unscoped(self) -> list[dict]:
        """Документы коллекции без сужения по арендатору.

        Нужно только для **разрешения идентичности**: проверка API-ключа ищет строку по
        хэшу, и ключ сам называет своё пространство. В режиме БД это платформенное чтение:
        если контекст арендатора задан, RLS всё равно сузит выборку — на это и рассчитано.
        Для списков «своих» сущностей использовать нельзя (см. :meth:`all`).
        """
        self._ensure_seed()
        if self.db is not None:
            rows = self.db.doc_all(self.name)
            return [dict(doc) for doc in rows]
        with self._lock:
            return [dict(self._mem[k]) for k in self._order if k in self._mem]

    def upsert_unscoped(self, doc: dict) -> dict:
        """Записать документ в **его** пространство, не подменяя его контекстом запроса."""
        self._ensure_seed()
        workspace = doc.get("workspace_id") or self.workspace_id
        doc = {**doc, "workspace_id": workspace}
        if self.db is not None:
            self.db.doc_put(self.name, self._key(doc), workspace, doc)
        return self._remember(doc)

    def get(self, key: str) -> dict | None:
        self._ensure_seed()
        scope = self._scope()
        if self.db is not None:
            doc = self.db.doc_get(self.name, key, workspace_id=scope)
            return self._remember(doc) if doc else None
        doc = self._mem.get(key)
        return doc if doc is not None and self._visible(doc) else None

    def put(self, doc: dict) -> dict:
        """Записать документ в пространство текущего запроса.

        Если документ явно помечен чужим ``workspace_id``, а контекст задан — это попытка
        записи в чужое пространство: ``TenancyError``, а не «тихо положили к себе/в ws_de».
        """
        self._ensure_seed()
        scope = self._scope()
        declared = doc.get("workspace_id")
        workspace = declared or scope or self.workspace_id
        if scope is not None and str(workspace) != str(scope):
            raise tenancy.TenancyError(
                f"{self.name}: запись в чужое пространство ({workspace}) из контекста {scope}")
        doc = {**doc, "workspace_id": workspace}
        key = self._key(doc)
        if self.db is not None:
            self.db.doc_put(self.name, key, workspace, doc)
        return self._remember(doc)

    def delete(self, key: str) -> bool:
        self._ensure_seed()
        scope = self._scope()
        if scope is not None:                       # удалять можно только своё
            existing = self._mem.get(key)
            if existing is not None and not self._visible(existing):
                return False
        if self.db is not None:
            self.db.doc_delete(self.name, key, workspace_id=scope)
        self._mem.pop(key, None)
        if key in self._order:
            self._order.remove(key)
        return True

    def clear_memory(self) -> None:
        """Сбросить кэш процесса (тесты «перезапуска контейнера»)."""
        self._mem.clear()
        self._order.clear()
        self._seeded = False

    def count(self) -> int:
        return len(self.all())
