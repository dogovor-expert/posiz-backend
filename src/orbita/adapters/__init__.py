"""Адаптеры каналов. Ядро работает только через интерфейс ChannelAdapter (ADR 0002)."""
from .base import ChannelAdapter, NormalizedError, PublishInput, PublishResult
from .registry import get_adapter, ADAPTERS

__all__ = ["ChannelAdapter", "NormalizedError", "PublishInput", "PublishResult", "get_adapter", "ADAPTERS"]
