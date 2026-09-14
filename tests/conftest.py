"""Shared factories for the TemporaryDirectory + Store + add_source boilerplate.

New tests should use these instead of re-rolling the setUp/tearDown pattern
found in test_cleanup/test_web/test_crawl/test_routing; those files predate
the fixtures and are intentionally left untouched.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from ustc_crawler.models import SourceConfig
from ustc_crawler.store import Store


@pytest.fixture
def make_source_config() -> Callable[..., SourceConfig]:
    def _make(**overrides: Any) -> SourceConfig:
        values: dict[str, Any] = {
            "id": "news",
            "name": "测试新闻",
            "organization_level": "university",
            "seed_urls": ["https://news.example.test/"],
            "allowed_hosts": ["news.example.test"],
        }
        values.update(overrides)
        return SourceConfig(**values)

    return _make


@pytest.fixture
def make_store(
    tmp_path: Path,
    make_source_config: Callable[..., SourceConfig],
) -> Iterator[Callable[..., Store]]:
    stores: list[Store] = []
    counter = 0

    def _make(sources: SourceConfig | list[SourceConfig] | None = None) -> Store:
        nonlocal counter
        counter += 1
        data_dir = tmp_path / f"data-{counter}"
        store = Store(data_dir / "crawler.sqlite", data_dir)
        if sources is None:
            resolved = [make_source_config()]
        elif isinstance(sources, SourceConfig):
            resolved = [sources]
        else:
            resolved = sources
        store.add_sources(resolved)
        stores.append(store)
        return store

    yield _make

    for store in stores:
        store.close()
