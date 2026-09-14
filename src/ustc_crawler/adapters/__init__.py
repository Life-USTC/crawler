"""Adapter registry: exact source_id first, then host match."""

from __future__ import annotations

import logging
from urllib.parse import urlsplit

from .base import SiteAdapter

logger = logging.getLogger(__name__)

_by_source: dict[str, SiteAdapter] = {}
_by_host: dict[str, SiteAdapter] = {}


def register(adapter: SiteAdapter) -> None:
    for source_id in adapter.source_ids:
        _by_source[source_id] = adapter
    for host in adapter.hosts:
        normalized = host.lower()
        existing = _by_host.get(normalized)
        if existing is not None and existing is not adapter:
            logger.warning(
                "adapter host %s re-registered: %s overrides %s",
                normalized,
                adapter.name,
                existing.name,
            )
        _by_host[normalized] = adapter


def adapter_for(source_id: str, url: str) -> SiteAdapter | None:
    if source_id in _by_source:
        return _by_source[source_id]
    host = (urlsplit(url).hostname or "").lower()
    return _by_host.get(host)


from . import course, jhtml, vsb, wordpress, yz  # noqa: E402, F401
