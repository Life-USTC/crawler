from __future__ import annotations

from collections.abc import Mapping
from urllib.parse import urlsplit


def source_id_for_url(
    url: str,
    source_hosts: Mapping[str, tuple[list[str], list[str]]],
) -> str:
    """Resolve a URL to one configured source using deterministic host ownership."""

    try:
        host = (urlsplit(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""
    candidates: list[tuple[int, str]] = []
    for source_id, (allowed_hosts, blocked_hosts) in source_hosts.items():
        blocked = any(
            host == value.lower().rstrip(".")
            or host.endswith("." + value.lower().rstrip("."))
            for value in blocked_hosts
        )
        if blocked:
            continue
        matches = [
            value.lower().rstrip(".")
            for value in allowed_hosts
            if host == value.lower().rstrip(".")
            or host.endswith("." + value.lower().rstrip("."))
        ]
        if matches:
            candidates.append((-max(map(len, matches)), source_id))
    return min(candidates)[1] if candidates else ""
