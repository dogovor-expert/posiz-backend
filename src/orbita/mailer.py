"""Отправка писем: Resend → UniSender → SMTP, по убыванию удобства.

Никаких «декоративных» отправок: если ни один транспорт не настроен, функция честно
возвращает ``{"sent": False, "reason": …}``, а ссылка пишется в лог уровня WARNING —
именно так выглядит боевой инцидент, а не «письмо отправлено» при пустом SMTP.

Транспорт выбирается по приоритету окружения:
* ``RESEND_API_KEY`` — HTTPS API, лучший DX;
* ``UNISENDER_API_KEY`` + ``UNISENDER_SENDER`` — российский провайдер, доставляемость в РФ;
* ``SMTP_HOST``/``SMTP_PORT``/``SMTP_USER``/``SMTP_PASSWORD`` — корпоративный релей.
"""
from __future__ import annotations

import logging
import os
import smtplib
import ssl
from email.message import EmailMessage

import httpx

log = logging.getLogger("orbita.mailer")

FROM_DEFAULT = "Orbita <no-reply@posiz.ru>"


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def transport() -> str:
    if _env("RESEND_API_KEY"):
        return "resend"
    if _env("UNISENDER_API_KEY"):
        return "unisender"
    if _env("SMTP_HOST"):
        return "smtp"
    return "none"


def status() -> dict:
    kind = transport()
    return {"transport": kind, "configured": kind != "none",
            "from": _env("MAIL_FROM", FROM_DEFAULT),
            "hint": {"none": "задайте RESEND_API_KEY, UNISENDER_API_KEY или SMTP_HOST"}.get(kind, "")}


def send(to: str, subject: str, text: str, html: str | None = None, *,
         client: httpx.Client | None = None) -> dict:
    """Отправить письмо доступным транспортом. Возвращает результат доставки."""
    kind = transport()
    sender = _env("MAIL_FROM", FROM_DEFAULT)
    if kind == "none":
        log.warning("MAIL не настроен: письмо для %s не отправлено (%s)", to, subject)
        return {"sent": False, "transport": "none", "to": to,
                "reason": "нет ни одного настроенного транспорта (Resend/UniSender/SMTP)"}
    try:
        if kind == "resend":
            return _resend(to, subject, text, html, sender, client)
        if kind == "unisender":
            return _unisender(to, subject, text, html, sender, client)
        return _smtp(to, subject, text, html, sender)
    except Exception as exc:                       # noqa: BLE001 — наружу отдаём причину
        log.warning("письмо для %s не отправлено через %s: %s", to, kind, exc)
        return {"sent": False, "transport": kind, "to": to, "reason": str(exc)}


def _resend(to: str, subject: str, text: str, html: str | None, sender: str,
            client: httpx.Client | None) -> dict:
    own = client is None
    http = client or httpx.Client(timeout=15)
    try:
        resp = http.post("https://api.resend.com/emails",
                         headers={"Authorization": f"Bearer {_env('RESEND_API_KEY')}"},
                         json={"from": sender, "to": [to], "subject": subject,
                               "text": text, **({"html": html} if html else {})})
        data = resp.json() if resp.content else {}
        if resp.status_code >= 400:
            raise RuntimeError(f"Resend {resp.status_code}: {data.get('message', resp.text[:120])}")
        return {"sent": True, "transport": "resend", "id": data.get("id"), "to": to}
    finally:
        if own:
            http.close()


def _unisenderitext(text: str) -> str:
    return text


def _unisender(to: str, subject: str, text: str, html: str | None, sender: str,
               client: httpx.Client | None) -> dict:
    own = client is None
    http = client or httpx.Client(timeout=20)
    try:
        resp = http.post("https://api.unisender.com/ru/api/sendEmail",
                         params={"format": "json", "api_key": _env("UNISENDER_API_KEY")},
                         data={"email": to, "sender_name": "Posiz",
                               "sender_email": _env("UNISENDER_SENDER", "no-reply@posiz.ru"),
                               "subject": subject, "body": html or text,
                               "list_id": _env("UNISENDER_LIST_ID", "1")})
        data = resp.json() if resp.content else {}
        if data.get("error"):
            raise RuntimeError(f"UniSender: {data['error']}")
        if resp.status_code >= 400:
            raise RuntimeError(f"UniSender {resp.status_code}: {resp.text[:120]}")
        return {"sent": True, "transport": "unisender", "to": to}
    finally:
        if own:
            http.close()


def _smtp(to: str, subject: str, text: str, html: str | None, sender: str) -> dict:
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(text)
    if html:
        msg.add_alternative(html, subtype="html")
    host = _env("SMTP_HOST")
    port = int(_env("SMTP_PORT", "587"))
    context = ssl.create_default_context()
    if port == 465:
        server: smtplib.SMTP = smtplib.SMTP_SSL(host, port, context=context, timeout=20)
    else:
        server = smtplib.SMTP(host, port, timeout=20)
        server.starttls(context=context)
    try:
        if _env("SMTP_USER"):
            server.login(_env("SMTP_USER"), _env("SMTP_PASSWORD"))
        server.send_message(msg)
    finally:
        server.quit()
    return {"sent": True, "transport": "smtp", "to": to}


def magic_link_text(link: str, ttl_minutes: int = 15) -> tuple[str, str]:
    text = (f"Вход в Posiz\n\nСсылка действует {ttl_minutes} минут и работает один раз:\n{link}\n\n"
            "Если вы не запрашивали вход, просто проигнорируйте письмо.")
    html = (f"<p>Вход в Posiz</p><p><a href=\"{link}\">Открыть ссылку входа</a></p>"
            f"<p>Ссылка действует {ttl_minutes} минут и работает один раз.</p>")
    return text, html
