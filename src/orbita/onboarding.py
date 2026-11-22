"""Онбординг и песочница каналов.

Проблема: Bot API MAX требует верификации ИП/ООО через Госуслуги и модерации 1–48 ч.
Пользователь не должен ждать: «Быстрый старт» подключает VK или Telegram за секунды,
а MAX висит в состоянии ``pending_verification`` и получает публикации в песочницу.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

QUICKSTART: list[dict] = [
    {
        "channel": "vk",
        "label": "ВКонтакте",
        "mode": "oauth",
        "eta": "30 секунд",
        "eta_minutes": 1,
        "blocking": False,
        "why": "Токен сообщества выдаёт сам админ: публикация и чтение работают сразу.",
        "steps": [
            "Открыть vk.com → Сообщество → Управление → Работа с API",
            "Создать ключ с правами wall и groups",
            "Вставить токен — проверка подключения занимает один запрос",
        ],
    },
    {
        "channel": "telegram",
        "label": "Telegram",
        "mode": "bot_token",
        "eta": "1–2 минуты",
        "eta_minutes": 2,
        "blocking": False,
        "why": "BotFather выдаёт токен сразу; ограничения РКН — риск доставки, не подключения.",
        "steps": [
            "Написать @BotFather → /newbot",
            "Добавить бота админом канала",
            "Вставить токен — канал появится в списке",
        ],
    },
    {
        "channel": "max",
        "label": "MAX",
        "mode": "bot_verification",
        "eta": "1–48 часов",
        "eta_minutes": None,
        "blocking": False,
        "sandbox": True,
        "why": "Требуется ИП/ООО, верификация через Госуслуги и модерация бота. "
               "Пока заявка идёт — публикации уходят в песочницу и видны только внутри продукта.",
        "steps": [
            "Подтвердить организацию (ИНН, Госуслуги)",
            "Создать бота и отправить на модерацию",
            "Работать в песочнице: тексты, превью, расписание — без реальной отправки",
        ],
    },
]

STATES: dict[str, dict] = {}   # channel -> connection state


def quickstart() -> dict:
    connected = {ch for ch, st in STATES.items() if st["state"] == "connected"}
    pending = {ch for ch, st in STATES.items() if st["state"] == "pending_verification"}
    return {
        "steps": [{**s, "connected": s["channel"] in connected,
                   "pending": s["channel"] in pending} for s in QUICKSTART],
        "ready_to_publish": sorted(connected),
        "blocking_steps": [s["channel"] for s in QUICKSTART if s["blocking"]],
        "advice": ("Начните с VK или Telegram — публикация доступна сразу, "
                   "MAX подключится после модерации без блокировки работы."),
    }


def connect(channel: str) -> dict:
    step = next((s for s in QUICKSTART if s["channel"] == channel), None)
    if step is None:
        # Каналы без онбординга (ОК, X) подключаются ручным токеном
        state = {"channel": channel, "state": "connected", "mode": "manual_token",
                 "sandbox": False, "eta": "вручную", "requirements": [],
                 "instructions": "Вставьте токен приложения и проверьте права публикации.",
                 "connected_at": datetime.now(timezone.utc)}
        STATES[channel] = state
        return state

    if channel == "max":
        state = {
            "channel": "max",
            "state": "pending_verification",
            "mode": "bot_verification",
            "sandbox": True,
            "eta": "1–48 часов",
            "requirements": ["ИП или ООО", "верификация через Госуслуги",
                             "модерация бота MAX (до 5 ботов на организацию)"],
            "instructions": ("Публикации в MAX уходят в песочницу: текст, медиа и расписание "
                             "проверяются внутри продукта, реальная отправка начнётся после верификации."),
            "next_check_at": datetime.now(timezone.utc) + timedelta(hours=6),
            "submitted_at": datetime.now(timezone.utc),
        }
    else:
        state = {
            "channel": channel,
            "state": "connected",
            "mode": step["mode"],
            "sandbox": False,
            "eta": step["eta"],
            "requirements": [],
            "instructions": "Токен принят, канал проверен тестовым запросом.",
            "connected_at": datetime.now(timezone.utc),
        }
    STATES[channel] = state
    return state


def verify(channel: str) -> dict:
    """Демо-переключатель: имитирует завершение модерации MAX."""
    state = STATES.get(channel) or connect(channel)
    state.update(state="connected", sandbox=False, eta="готово",
                 connected_at=datetime.now(timezone.utc), verified_manually=True)
    STATES[channel] = state
    return state


def state_of(channel: str) -> dict | None:
    return STATES.get(channel)
