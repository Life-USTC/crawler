from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from bs4 import BeautifulSoup

from .canonicalize import normalize_url
from .models import SourceConfig

UNIT_WORDS = re.compile(r"学院|学部|系|中心|实验室|研究院|研究所|医院|工程师|平台|基地|部")
EXCLUDED_HOSTS = {
    "www.ustc.edu.cn",
    "news.ustc.edu.cn",
    "lib.ustc.edu.cn",
    "course.ustc.edu.cn",
    "www.teach.ustc.edu.cn",
    "jxzy.ustc.edu.cn",
    "gradschool.ustc.edu.cn",
    "yz.ustc.edu.cn",
    "scc.ustc.edu.cn",
    "hospital.ustc.edu.cn",
    "welcome.ustc.edu.cn",
    "zsb.ustc.edu.cn",
    "www.job.ustc.edu.cn",
    "ustcnet.ustc.edu.cn",
    "ugs.ustc.edu.cn",
}


def _level(name: str) -> str:
    if "系" in name:
        return "department"
    if "学院" in name or "学部" in name:
        return "college"
    if "中心" in name:
        return "center"
    if "实验室" in name or "研究所" in name or "研究院" in name:
        return "research"
    return "unit"


def discover_units(
    directory_url: str = "https://www.ustc.edu.cn/yxjs.htm",
    output: str | Path = "data/discovered_units.json",
    timeout: float = 30.0,
) -> dict:
    target = Path(output)
    target.parent.mkdir(parents=True, exist_ok=True)
    result = {
        "directory_url": directory_url,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "units": [],
        "excluded": [],
        "error": "",
    }
    try:
        response = httpx.get(
            directory_url,
            follow_redirects=True,
            timeout=timeout,
            headers={"User-Agent": "ustc-public-site-crawler/0.1"},
        )
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
    except (httpx.HTTPError, UnicodeError) as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        target.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        return result
    seen: set[tuple[str, str]] = set()
    for anchor in soup.find_all("a", href=True):
        name = re.sub(r"\s+", " ", anchor.get_text(" ", strip=True))
        url = normalize_url(anchor["href"], directory_url)
        if not url or not name or not UNIT_WORDS.search(name):
            continue
        host = (urlsplit(url).hostname or "").lower()
        item = {
            "name": name,
            "url": url,
            "host": host,
            "source_url": directory_url,
            "level": _level(name),
            "official_ustc": host == "ustc.edu.cn" or host.endswith(".ustc.edu.cn"),
        }
        key = (name, url)
        if key in seen:
            continue
        seen.add(key)
        if item["official_ustc"] and host not in EXCLUDED_HOSTS:
            result["units"].append(item)
        else:
            result["excluded"].append(item)
    result["units"].sort(key=lambda item: (item["level"], item["name"], item["url"]))
    result["excluded"].sort(key=lambda item: (item["name"], item["url"]))
    target.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def unit_sources(path: str | Path) -> list[SourceConfig]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    by_host: dict[str, SourceConfig] = {}
    for item in raw.get("units", []):
        host = str(item["host"]).lower()
        if not host or host in by_host:
            existing = by_host.get(host)
            if existing and item.get("name") not in existing.aliases:
                existing.aliases.append(str(item.get("name", "")))
            continue
        source_id = "unit-" + re.sub(r"[^a-z0-9]+", "-", host).strip("-")
        by_host[host] = SourceConfig(
            id=source_id,
            name=str(item["name"]),
            organization_level=str(item.get("level", "unit")),
            seed_urls=[normalize_url(str(item["url"]))],
            allowed_hosts=[host],
            aliases=[],
        )
    return sorted(by_host.values(), key=lambda source: source.id)
