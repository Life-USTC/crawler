"""Adapter registry: exact source_id first, then host match."""

from __future__ import annotations

from urllib.parse import urlsplit

from .base import SiteAdapter

_by_source: dict[str, SiteAdapter] = {}
_by_host: dict[str, SiteAdapter] = {}


def register(adapter: SiteAdapter) -> None:
    for source_id in adapter.source_ids:
        _by_source[source_id] = adapter
    for host in adapter.hosts:
        _by_host[host.lower()] = adapter


def adapter_for(source_id: str, url: str) -> SiteAdapter | None:
    if source_id in _by_source:
        return _by_source[source_id]
    host = (urlsplit(url).hostname or "").lower()
    return _by_host.get(host)


from . import jhtml, vsb, wordpress  # noqa: E402, F401
