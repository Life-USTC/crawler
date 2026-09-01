from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from .canonicalize import normalize_url
from .models import SourceConfig


def load_config(
    path: str | Path, *, include_supplemental: bool = False
) -> tuple[list[SourceConfig], str]:
    config_path = Path(path)
    raw: dict[str, Any] = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    sources = []
    source_items = list(raw.get("sources", []))
    if include_supplemental:
        source_items.extend(raw.get("supplemental_sources", []))
    for item in source_items:
        seed_urls = [normalize_url(str(url)) for url in item.get("seed_urls", [])]
        max_images = item.get("max_images_per_page")
        sources.append(
            SourceConfig(
                id=str(item["id"]),
                name=str(item["name"]),
                organization_level=str(item.get("organization_level", "unknown")),
                seed_urls=seed_urls,
                allowed_hosts=[str(host).lower() for host in item.get("allowed_hosts", [])],
                blocked_hosts=[str(host).lower() for host in item.get("blocked_hosts", [])],
                aliases=[str(alias) for alias in item.get("aliases", [])],
                discovery_only=bool(item.get("discovery_only", False)),
                max_images_per_page=int(max_images) if max_images is not None else None,
            )
        )
    directory = raw.get("directory", {}) or {}
    directory_url = normalize_url(str(directory.get("url", "https://www.ustc.edu.cn/yxjs.htm")))
    return sources, directory_url


def source_map(sources: list[SourceConfig]) -> dict[str, SourceConfig]:
    return {source.id: source for source in sources}
