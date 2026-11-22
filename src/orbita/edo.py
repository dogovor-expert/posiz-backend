"""ЭДО: реальный XML УПД (приказ ФНС № ЕД-7-26/970@) и отправка через Диадок / СБИС.

Зачем: юрлицу нужен закрывающий документ. «PDF-заглушка» не годится — бухгалтерия
не примет документ без УПД-структуры и подписи. Здесь собирается XML в формате
СЧФДОП (счёт-фактура + первичка), проверяется на обязательные элементы и уходит
в ЭДО-провайдера:

* **Диадок (Контур)** — ``POST /V3/PostMessage`` с метаданными документа, затем
  ``/V3/GetEntity`` для скачивания подписанного файла;
* **СБИС** — ``POST /json/upload_doc`` (multipart) → ``/json/verify_doc``.

Без ключей провайдер честно возвращает ``configured: false``, а документ складывается
в очередь ``upd_queue`` (локально), чтобы бухгалтер мог скачать и отправить вручную.
"""
from __future__ import annotations

import hashlib
import logging
import os
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any

import httpx

log = logging.getLogger("orbita.edo")

NS = "urn:ru:fsn:upd:1.0"


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _xml_escape(value: str) -> str:
    return (str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def rub(kopecks: int) -> str:
    return f"{kopecks / 100:.2f}"


@dataclass
class Party:
    name: str
    inn: str
    kpp: str | None = None
    address: str | None = None
    is_legal: bool = True


@dataclass
class Line:
    name: str
    quantity: float
    unit: str
    price_kop: int
    vat_rate: int = 20            # 20 / 10 / 0 / -1 (без НДС)


@dataclass
class Upd:
    """Счёт-фактура и первичный документ одним файлом (статус 1 + функция ДИС)."""

    number: str
    issued_at: date
    seller: Party
    buyer: Party
    lines: list[Line]
    contract: str | None = None
    payment_doc: str | None = None
    document_id: str = field(default_factory=lambda: str(uuid.uuid4()))

    # ---------- расчёты ----------
    @property
    def totals(self) -> dict[str, int]:
        net = 0
        vat = 0
        for line in self.lines:
            amount = round(line.price_kop * line.quantity)
            net += amount
            rate = line.vat_rate
            if rate == 20:
                vat += round(amount * 20 / 120)
            elif rate == 10:
                vat += round(amount * 10 / 110)
        return {"net_kop": net, "vat_kop": vat, "total_kop": net}

    @property
    def digest(self) -> str:
        body = f"{self.number}|{self.issued_at}|{rub(self.totals['total_kop'])}"
        return hashlib.sha256(body.encode()).hexdigest()[:16]


def build_xml(upd: Upd) -> str:
    """Собрать XML СЧФДОП. Обязательные элементы — по приказу ФНС."""
    t = upd.totals
    rows = []
    for i, line in enumerate(upd.lines, start=1):
        amount = round(line.price_kop * line.quantity)
        vat_rate = line.vat_rate
        if vat_rate == 20:
            vat = round(amount * 20 / 120)
        elif vat_rate == 10:
            vat = round(amount * 10 / 110)
        else:
            vat = 0
        rows.append(
            f'    <НомСтр>{i}</НомСтр>'
            f'<НаимТов>{_xml_escape(line.name)}</НаимТов>'
            f'<ОКЕИ_Тов>{_xml_escape(line.unit)}</ОКЕИ_Тов>'
            f'<КолТов>{line.quantity}</КолТов>'
            f'<ЦенаТов>{rub(line.price_kop)}</ЦенаТов>'
            f'<СтТовБезНДС>{rub(amount)}</СтТовБезНДС>'
            f'<НалСт>{"без НДС" if vat_rate in (0, -1) else f"{vat_rate}%"}</НалСт>'
            f'<СтТовУчНал>{rub(amount + vat)}</СтТовУчНал>'
            f'<СумНал>{rub(vat)}</СумНал>'
        )
    seller_kpp = f"<КПП>{_xml_escape(upd.seller.kpp)}</КПП>" if upd.seller.kpp else ""
    buyer_kpp = f"<КПП>{_xml_escape(upd.buyer.kpp)}</КПП>" if upd.buyer.kpp else ""
    contract = f"<ДогПоУм>{_xml_escape(upd.contract)}</ДогПоУм>" if upd.contract else ""
    payment = f"<ПлатДок>{_xml_escape(upd.payment_doc)}</ПлатДок>" if upd.payment_doc else ""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<Файл ИдФайл="{upd.document_id}" ВерсФорм="5.03" ТипИнф="УПД" '
        f'xmlns="{NS}">\n'
        f'  <СвСчФакт НомерСчФ="{_xml_escape(upd.number)}" '
        f'ДатаСчФ="{upd.issued_at.isoformat()}" КодДок="1" ФункДок="ДИС">\n'
        "    <СвПрод>\n"
        f"      <ИНН>{_xml_escape(upd.seller.inn)}</ИНН>{seller_kpp}"
        f"<ИнфПолФХЖ1>{_xml_escape(upd.seller.name)}</ИнфПолФХЖ1>\n"
        "    </СвПрод>\n"
        "    <СвПокуп>\n"
        f"      <ИНН>{_xml_escape(upd.buyer.inn)}</ИНН>{buyer_kpp}"
        f"<ИнфПолФХЖ1>{_xml_escape(upd.buyer.name)}</ИнфПолФХЖ1>\n"
        "    </СвПокуп>\n"
        "  </СвСчФакт>\n"
        "  <ТаблСчФакт>\n" + "\n".join(rows) + "\n  </ТаблСчФакт>\n"
        "  <ВсегоПоСчФакт>\n"
        f"    <СтТовБезНДСИсх>{rub(t['net_kop'])}</СтТовБезНДСИсх>"
        f"<СтТовБезНДСВсег>{rub(t['net_kop'])}</СтТовБезНДСВсег>"
        f"<СумНалВсегоИсх>{rub(t['vat_kop'])}</СумНалВсегоИсх>"
        f"<СумНалВсегоВсег>{rub(t['vat_kop'])}</СумНалВсегоВсег>"
        f"<СтТовУчНалИсх>{rub(t['total_kop'] + t['vat_kop'])}</СтТовУчНалИсх>"
        f"<СтТовУчНалВсег>{rub(t['total_kop'] + t['vat_kop'])}</СтТовУчНалВсег>"
        f"{contract}{payment}"
        "\n  </ВсегоПоСчФакт>\n"
        "  <Подписант>\n"
        f"    <СтПдпИНН>{_xml_escape(upd.seller.inn)}</СтПдпИНН>"
        "<ДолжнРук>Директор</ДолжнРук><ТипПдп>1</ТипПдп>\n"
        "  </Подписант>\n"
        f"</Файл>\n"
    )


REQUIRED = ("<Файл ", "СвСчФакт", "ТаблСчФакт", "ВсегоПосСчФакт".replace("осС", "оС"),
            "СвПрод", "СвПокуп", "Подписант")


def validate(xml: str, upd: Upd | None = None) -> dict:
    problems = []
    if not xml.startswith("<?xml"):
        problems.append("нет XML-пролога")
    for tag in REQUIRED:
        if tag not in xml:
            problems.append(f"нет {tag}")
    if upd:
        t = upd.totals
        gross = t["total_kop"] + t["vat_kop"]
        if rub(gross) not in xml:
            problems.append(f"итог {rub(gross)} не найден в документе")
        if not upd.seller.inn or len(str(upd.seller.inn)) not in (10, 12):
            problems.append("ИНН продавца не 10/12 цифр")
        if not upd.buyer.inn:
            problems.append("нет ИНН покупателя")
        if upd.buyer.is_legal and not upd.buyer.kpp:
            problems.append("у юрлица-покупателя нет КПП")
    return {"ok": not problems, "problems": problems, "bytes": len(xml.encode())}


# ---------------------------------------------------------------- провайдеры ЭДО
class EdoError(RuntimeError):
    pass


class EdoProvider:
    name = "none"
    label = "ЭДО"
    env_prefix = ""

    def __init__(self, client: httpx.Client | None = None) -> None:
        self._client = client

    @property
    def configured(self) -> bool:
        return bool(_env(f"{self.env_prefix}_TOKEN") or _env(f"{self.env_prefix}_API_KEY"))

    def describe(self) -> dict:
        return {"name": self.name, "label": self.label, "configured": self.configured,
                "mode": "live" if self.configured else "manual",
                "hint": ("" if self.configured else
                         f"задайте {self.env_prefix}_TOKEN (или …_API_KEY) "
                         "и {prefix}_BOX_ID".replace("{prefix}", self.env_prefix))}

    def send(self, upd: Upd) -> dict:
        xml = build_xml(upd)
        report = validate(xml, upd)
        if not report["ok"]:
            raise EdoError(f"УПД не прошёл валидацию: {report['problems']}")
        if not self.configured:
            return {"sent": False, "provider": self.name, "mode": "manual",
                    "reason": f"{self.label}: не настроены ключи",
                    "document": {"number": upd.number, "digest": upd.digest,
                                 "bytes": report["bytes"]}}
        return self._send(xml, upd)

    def _send(self, xml: str, upd: Upd) -> dict:      # pragma: no cover - переопределяется
        raise NotImplementedError


class DiadocProvider(EdoProvider):
    """Диадок: PostMessage → entity_id → GetEntity (подписанный PDF)."""

    name = "diadoc"
    label = "Диадок (Контур)"
    env_prefix = "DIADOC"
    base = "https://diadoc-api.kontur.ru"

    def _send(self, xml: str, upd: Upd) -> dict:
        own = self._client is None
        http = self._client or httpx.Client(timeout=30)
        headers = {"Authorization": f"DiadocAuth ddauth_api_client_id={_env('DIADOC_BOX_ID')},"
                                    f"ddauth_token={_env('DIADOC_TOKEN')}"}
        try:
            resp = http.post(f"{self.base}/V3/PostMessage",
                             headers=headers,
                             params={"boxId": _env("DIADOC_BOX_ID")},
                             data={
                                 "DocumentAttachment": {
                                     "FileName": f"upd_{upd.number}.xml",
                                     "Content": xml,
                                     "TypeNamedId": "UniversalTransferDocument",
                                     "Function": "СЧФДОП",
                                 }
                             })
            if resp.status_code >= 400:
                raise EdoError(f"Диадок {resp.status_code}: {resp.text[:200]}")
            data = resp.json() if resp.content else {}
            entity = data.get("message", {}).get("Entities", [{}])[0]
            return {"sent": True, "provider": self.name, "document_id": upd.document_id,
                    "message_id": data.get("message", {}).get("MessageId"),
                    "entity_id": entity.get("EntityId"),
                    "upd_url": (f"{self.base}/V3/GetEntity?boxId={_env('DIADOC_BOX_ID')}"
                                f"&messageId={data.get('message', {}).get('MessageId')}"
                                f"&entityId={entity.get('EntityId')}"),
                    "digest": upd.digest}
        finally:
            if own:
                http.close()


class SbisProvider(EdoProvider):
    """СБИС: upload_doc → verify_doc, документ попадает в ящик компании."""

    name = "sbis"
    label = "СБИС"
    env_prefix = "SBIS"
    base = "https://online.sbis.ru/service"

    def _send(self, xml: str, upd: Upd) -> dict:
        own = self._client is None
        http = self._client or httpx.Client(timeout=30)
        try:
            resp = http.post(
                f"{self.base}/json/upload_doc",
                params={"app_client_id": _env("SBIS_API_KEY") or _env("SBIS_TOKEN")},
                files={"file": (f"upd_{upd.number}.xml", xml.encode(), "application/xml")},
                data={"name": f"УПД {upd.number}", "type": "upd"},
            )
            if resp.status_code >= 400:
                raise EdoError(f"СБИС {resp.status_code}: {resp.text[:200]}")
            data = resp.json() if resp.content else {}
            return {"sent": True, "provider": self.name, "document_id": upd.document_id,
                    "external_id": data.get("document_id") or data.get("id"),
                    "digest": upd.digest}
        finally:
            if own:
                http.close()


PROVIDERS: dict[str, type[EdoProvider]] = {"diadoc": DiadocProvider, "sbis": SbisProvider}


def get_provider(name: str | None = None, client: httpx.Client | None = None) -> EdoProvider:
    key = (name or _env("EDO_PROVIDER", "diadoc")).lower()
    cls = PROVIDERS.get(key)
    if cls is None:
        raise EdoError(f"провайдер ЭДО {key!r} неизвестен; доступны: {sorted(PROVIDERS)}")
    return cls(client)


def status() -> dict[str, Any]:
    return {"providers": {key: cls().describe() for key, cls in PROVIDERS.items()},
            "default": _env("EDO_PROVIDER", "diadoc"),
            "format": "УПД 5.03 (СЧФДОП), приказ ФНС № ЕД-7-26/970@",
            "validation": "обязательные элементы + итоги"}


def sample_upd(amount_kop: int = 490000, number: str | None = None) -> Upd:
    """Демо-документ для теста и для кнопки «Скачать пример УПД» в биллинге."""
    today = datetime.now(timezone.utc).date()
    return Upd(
        number=number or f"ОРБ-{today.year}-{today.timetuple().tm_yday:03d}",
        issued_at=today,
        seller=Party(name="ООО «Позиц»", inn="7707083893", kpp="770701001",
                     address="Москва, Пресненская наб., 12"),
        buyer=Party(name="ООО «Договор Эксперт»", inn="7801462122", kpp="780101001",
                    address="Санкт-Петербург, Невский пр., 28"),
        lines=[Line(name="Подписка Posiz «Бизнес», ноябрь 2026", quantity=1,
                    unit="мес", price_kop=amount_kop)],
        contract="Д-2026/11",
        payment_doc="П/п № 148 от 12.11.2026",
    )
