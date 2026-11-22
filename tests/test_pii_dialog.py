"""PII: сквозная нумерация плейсхолдеров и маскирование ДО внешней модели.

Аудит (6): ``mask()`` нумеровал с единицы в каждом вызове, поэтому в переписке
``[PHONE_1]`` означал разные номера, а внешний NER получал **сырой** текст.
Оба утверждения проверены и закрыты.
"""
from __future__ import annotations

import pytest

from orbita import pii


def test_same_value_keeps_same_placeholder_within_call():
    text = "Звоните +7 916 123-45-67 или снова +7 916 123-45-67"
    masked, mapping = pii.mask(text)
    assert masked.count("[PHONE_1]") == 2
    assert len(mapping) == 1


def test_different_values_get_different_placeholders_in_one_dialog():
    """Ключевое исправление: один маскировщик на диалог → разные люди не склеиваются."""
    masker = pii.Masker()
    first, _ = masker.mask("Клиент Пётр Сидоров, телефон 8 916 123-45-67")
    second, _ = masker.mask("Клиент Иван Петров, телефон 8 903 000-11-22")

    assert "[PHONE_1]" in first and "[PHONE_2]" in second
    assert "[PERSON_1]" in first and "[PERSON_2]" in second
    # восстановление возвращает каждому его собственные данные
    assert "8 916 123-45-67" in masker.restore(first)
    assert "8 903 000-11-22" in masker.restore(second)


def test_masker_restores_original_in_llm_answer():
    masker = pii.Masker()
    masked, _ = masker.mask("Договор с Иваном Петровым, телефон +7 903 000-11-22")
    answer = masker.restore(f"Проверьте договор с {masked.split(',')[0]} — перезвоним.")
    assert "Иван" in answer or "Петров" in answer
    assert "Дженерал" not in answer


def test_counters_continue_from_existing_mapping():
    _, mapping = pii.mask("тел +7 916 111-22-33")
    masked, mapping2 = pii.mask("тел +7 916 444-55-66", mapping)
    # нумерация продолжается, а не начинается заново: старый плейсхолдер занят другим номером
    assert "[PHONE_1]" in mapping2 and "[PHONE_2]" in mapping2
    assert "[PHONE_2]" in masked
    assert len(mapping2) == 2


def test_ner_receives_already_masked_text(monkeypatch):
    """Внешняя модель не должна получать сырые ПДн — иначе маскирование бессмысленно."""
    seen: list[str] = []

    def fake_ner(text: str) -> list[str]:
        seen.append(text)
        return []

    monkeypatch.setattr(pii, "_ner_remote", fake_ner)
    text = "Позвоните +7 916 123-45-67, паспорт 45 06 123456, почта client@mail.ru"
    masked, _ = pii.mask(text)

    assert seen, "NER вызывается"
    assert all("916" not in t and "client@mail.ru" not in t for t in seen), seen
    assert "[PHONE_1]" in masked


def test_pii_never_leaks_into_masked_fields():
    masker = pii.Masker()
    masked, _ = masker.mask("ИНН 7707083893, СНИЛС 112-233-445 95")
    for raw in ("7707083893", "112-233-445"):
        assert raw not in masked


@pytest.mark.parametrize("text,kind", [
    ("почта anna@example.ru", "EMAIL"),
    ("карта 4276 1234 5678 9012", "CARD"),
    ("р/с 40702810900000012345", "ACCOUNT"),
])
def test_kinds_are_recognised(text, kind):
    masked, mapping = pii.mask(text)
    assert any(ph.startswith(f"[{kind}_") for ph in mapping), masked


def test_audit_report_shows_counts_not_values():
    report = pii.audit_report("Пётр Сидоров, +7 916 123-45-67, anna@example.ru")
    assert report["found"] >= 3
    assert "916" not in str(report) and "anna@" not in str(report)


def test_llm_masks_all_messages_with_one_numbering(monkeypatch):
    """Промпт из нескольких сообщений не должен склеивать разных людей в один плейсхолдер."""
    from orbita import llm

    captured: dict = {}

    def fake_post(path, payload):
        captured["messages"] = payload["messages"]
        return {"choices": [{"message": {"content": "ok"}}], "usage": {"prompt_tokens": 5,
                                                                      "completion_tokens": 1}}

    client = llm.LLM()
    monkeypatch.setattr(llm, "enabled", lambda: True)      # включаем «живой» путь
    monkeypatch.setattr(client, "_post", fake_post)
    client.complete("fast", [
        {"role": "user", "content": "Клиент Пётр Сидоров, тел +7 916 111-22-33"},
        {"role": "user", "content": "Клиент Иван Петров, тел +7 916 444-55-66"},
    ])

    joined = " ".join(m["content"] for m in captured["messages"])
    assert "[PHONE_1]" in joined and "[PHONE_2]" in joined
    assert "916" not in joined                      # телефоны не ушли в модель


def test_billing_webhook_is_idempotent():
    """Провайдер повторяет уведомления — повтор не должен продлевать подписку дважды."""
    from orbita import billing

    inv = billing.create_invoice(4990.0, plan="business")
    first = billing.webhook(inv["payment_id"])
    second = billing.webhook(inv["payment_id"])
    assert first["status"] == "succeeded"
    assert second.get("idempotent") is True
    # позднее событие об ошибке не отменяет уже полученные деньги
    assert billing.webhook(inv["payment_id"], "payment.canceled").get("ignored")
