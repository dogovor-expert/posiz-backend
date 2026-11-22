"""Pydantic-модели публичного контракта (см. openapi.yaml)."""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

# Единственный источник правды о каналах — карта площадок: Literal собирается из
# catalog.CATALOG, поэтому Дзен/TenChat/Pinterest больше не отбиваются валидацией 422
# и не расходятся с ENUM channel_kind в БД (см. tests/test_schema_sync.py).
from .catalog import CATALOG  # noqa: E402

CHANNEL_KINDS: tuple[str, ...] = tuple(spec.kind for spec in CATALOG)
ChannelKind = Literal[*CHANNEL_KINDS]

PublicationStatus = Literal[
    "draft", "queued", "publishing", "published", "failed",
    "needs_reauth", "blocked_by_compliance", "cancelled",
]

ComplianceVerdict = Literal["informational", "ad_requires_marking", "forbidden_resource", "unknown"]


class ChannelHealth(BaseModel):
    last_ok_at: datetime | None = None
    error_rate_24h: float = 0.0
    delivery_risk: Literal["low", "medium", "high"] = "low"


class ChannelAccount(BaseModel):
    id: str
    channel: ChannelKind
    title: str
    audience_size: int = 0
    status: Literal["active", "needs_reauth", "rate_limited", "paused", "revoked"] = "active"
    capabilities: dict = Field(default_factory=dict)
    health: ChannelHealth = Field(default_factory=ChannelHealth)
    note: str = ""
    connection_state: str | None = None
    sandbox: bool = False


class SignalIn(BaseModel):
    kind: Literal["url", "manual", "voice", "rss", "trend", "recycle", "law_change"] = "manual"
    title: str = Field(min_length=3, max_length=500)
    source_url: str | None = None
    raw_text: str | None = None
    goal: Literal["lead", "trust", "launch", "engage", "educate"] = "trust"


class Signal(BaseModel):
    id: str
    kind: str
    title: str
    status: str = "new"
    created_at: datetime


class GenerateIn(BaseModel):
    channels: list[ChannelKind] = Field(default_factory=lambda: ["max", "vk", "x"])
    formats: list[str] | None = None


class GenerateOut(BaseModel):
    run_id: str
    status: str = "completed"
    variants: list[dict]
    cost_rub: float


class ComplianceReason(BaseModel):
    rule_code: str
    severity: Literal["info", "warn", "block"]
    message: str


class ComplianceReport(BaseModel):
    verdict: ComplianceVerdict
    reasons: list[ComplianceReason]
    marking_block: str | None = None
    rules_version: int = 202611


class PublicationIn(BaseModel):
    variant_id: str
    channel_account_id: str
    scheduled_at: datetime
    erid: str | None = None
    is_ad: bool = False
    require_approval: bool = False
    #: артефакты медиа-конвейера (карточки, обложки). Порядок = порядок вложений в посте
    media_ids: list[str] = Field(default_factory=list, max_length=10)


class Publication(BaseModel):
    id: str
    variant_id: str
    channel_account_id: str
    scheduled_at: datetime
    status: PublicationStatus = "queued"
    idempotency_key: str
    external_id: str | None = None
    external_url: str | None = None
    cost_estimate_rub: float = 0.0
    media_ids: list[str] = Field(default_factory=list)
    last_error: str | None = None
    is_ad: bool = False
    erid: str | None = None
    approved_by_role: str | None = None


class AnalyticsSummary(BaseModel):
    impressions: int
    reactions: int
    clicks: int
    cost_rub: float
    by_channel: list[dict]
    link_clicks: int = 0
    links_total: int = 0


# ---------- v2: ОРД, короткие ссылки, онбординг, RBAC ----------
class OrdRequestIn(BaseModel):
    channel: ChannelKind
    advertiser_name: str = Field(min_length=2, max_length=200)
    advertiser_inn: str | None = Field(default=None, max_length=12)
    text: str = Field(min_length=1, max_length=4000)
    provider: str = "mock"
    variant_id: str | None = None
    external_url: str | None = None


class OrdSubmissionOut(BaseModel):
    erid: str
    provider: str
    status: Literal["registered", "pending_moderation", "rejected"]
    creative_id: str = ""
    submitted_at: datetime
    marking_block: str
    note: str = ""


class OrdProviderOut(BaseModel):
    name: str
    label: str
    configured: bool
    mode: str
    eta: str


class LinkIn(BaseModel):
    target_url: str = Field(min_length=4, max_length=2000)
    channel: ChannelKind
    campaign: str = Field(min_length=1, max_length=80)
    variant_id: str | None = None


class LinkOut(BaseModel):
    code: str
    short_url: str
    target_url: str
    channel: ChannelKind
    campaign: str
    created_at: datetime
    clicks: int = 0


class LinkStatsOut(BaseModel):
    code: str
    clicks: int
    clicks_7d: int
    uniques_7d: int
    by_day: list[dict]
    short_url: str
    target_url: str


class ConnectOut(BaseModel):
    channel: str
    state: Literal["connected", "pending_verification", "error"]
    mode: str
    sandbox: bool = False
    eta: str = ""
    requirements: list[str] = Field(default_factory=list)
    instructions: str = ""


class ActorOut(BaseModel):
    model_config = {"extra": "allow"}
    id: str
    role: str
    role_title: str
    workspace_id: str
    permissions: list[str]
