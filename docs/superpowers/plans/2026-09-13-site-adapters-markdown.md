# 站点适配器与 Markdown 提取实施计划(第一阶段)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 浏览器 UA + 站点适配器架构(注册表 + CMS 家族基类)+ 真 Markdown 提取,修复 library/iat/mech/iid/course/yz 覆盖缺口,并完成全源提取率审计。

**Architecture:** 在 `extract_page` 末尾挂适配器钩子:命中适配器且提取成功则覆盖 article 字段;未命中或返回 None 走现有通用启发式(行为不变)。适配器按 DOM 容器判定文章,URL 模式仅作预筛。Markdown 用 markdownify 从 body_html 转换。

**Tech Stack:** Python 3.13, BeautifulSoup4, markdownify(新增), httpx, pytest。

**Spec:** `docs/superpowers/specs/2026-09-13-site-adapters-markdown-design.md`

## Global Constraints

- 测试命令:`uv run pytest -q`;lint:`uv run ruff check src tests`。
- 测试风格:unittest class + `self.assertEqual`,参考 `tests/test_extract.py`。
- TDD:每个任务先写失败测试再实现。
- robots.txt 与按 host 限流不变。
- 平台协议不变:`body_html` 保持现状,`body_markdown` 变为真 Markdown。
- 每个任务完成后 commit;分支 `feat/site-adapters-markdown`。

---

### Task 1: 浏览器 UA 与请求头

**Files:**
- Modify: `src/ustc_crawler/http.py:13` 与 `src/ustc_crawler/http.py:52-56`
- Test: `tests/test_http.py`(新建)

**Interfaces:**
- Produces: `USER_AGENT`(常量,后续任务不依赖其值,只依赖行为)

- [ ] **Step 1: 写失败测试**

```python
# tests/test_http.py
import unittest

from ustc_crawler.http import USER_AGENT, Fetcher


class HttpClientTests(unittest.TestCase):
    def test_default_user_agent_looks_like_a_browser(self) -> None:
        self.assertIn("Mozilla/5.0", USER_AGENT)
        self.assertIn("Chrome/", USER_AGENT)

    def test_fetcher_sends_browser_headers(self) -> None:
        fetcher = Fetcher()
        try:
            headers = fetcher.client.headers
            self.assertIn("Mozilla/5.0", headers["User-Agent"])
            self.assertIn("zh-CN", headers["Accept-Language"])
        finally:
            import asyncio
            asyncio.run(fetcher.close())


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_http.py -q`
Expected: FAIL(`'Mozilla/5.0' not in ...`)

- [ ] **Step 3: 实现**

`src/ustc_crawler/http.py`:

```python
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
```

Fetcher headers 改为:

```python
            headers={
                "User-Agent": user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            },
```

注意:`robots.py` 用 UA 匹配 robots 组,浏览器 UA 会落到 `*` 组——这是可接受的(校内站 robots 基本只有 `*` 规则)。

- [ ] **Step 4: 运行确认通过 + 全量回归**

Run: `uv run pytest -q`
Expected: 全部通过(222+2)

- [ ] **Step 5: Commit**

```bash
git add src/ustc_crawler/http.py tests/test_http.py
git commit -m "feat(http): present a desktop browser identity by default"
```

---

### Task 2: Markdown 转换模块

**Files:**
- Create: `src/ustc_crawler/markdown.py`
- Modify: `pyproject.toml`(dependencies 加 `markdownify>=1.0.0`)
- Modify: `src/ustc_crawler/extract.py:1263`(`body_markdown=body_text` → 真转换)
- Test: `tests/test_markdown.py`(新建)

**Interfaces:**
- Produces: `html_to_markdown(html: str, *, strip_selectors: tuple[str, ...] = ()) -> str`(Task 3 的 `build_article_from_fields` 依赖)

- [ ] **Step 1: 写失败测试**

```python
# tests/test_markdown.py
import unittest

from ustc_crawler.markdown import html_to_markdown


class HtmlToMarkdownTests(unittest.TestCase):
    def test_headings_lists_tables_and_images_survive(self) -> None:
        html = (
            "<div><h2>一、总则</h2><p>第一段</p><ul><li>甲</li><li>乙</li></ul>"
            "<table><tr><th>A</th></tr><tr><td>1</td></tr></table>"
            "<p><img src='https://x.ustc.edu.cn/a.png' alt='示意图'></p></div>"
        )
        md = html_to_markdown(html)
        self.assertIn("## 一、总则", md)
        self.assertIn("- 甲", md)
        self.assertIn("| A |", md)
        self.assertIn("![示意图](https://x.ustc.edu.cn/a.png)", md)

    def test_strip_selectors_remove_site_chrome(self) -> None:
        html = "<div><p>正文</p><div class='footer-sign'> XX大学 版权所有</div></div>"
        md = html_to_markdown(html, strip_selectors=(".footer-sign",))
        self.assertIn("正文", md)
        self.assertNotIn("版权所有", md)

    def test_script_style_and_base64_images_removed(self) -> None:
        html = (
            "<div><script>var x=1;</script><style>p{}</style>"
            "<img src='data:image/png;base64,AAAA'>"
            "<p>内容</p></div>"
        )
        md = html_to_markdown(html)
        self.assertEqual(md, "内容")

    def test_empty_input(self) -> None:
        self.assertEqual(html_to_markdown(""), "")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_markdown.py -q`
Expected: FAIL(ModuleNotFoundError)

- [ ] **Step 3: 实现**

`pyproject.toml` dependencies 加一行 `"markdownify>=1.0.0",`,然后 `uv lock`。

`src/ustc_crawler/markdown.py`:

```python
"""Convert extracted article body HTML to clean Markdown."""

from __future__ import annotations

import re

from bs4 import BeautifulSoup
from markdownify import MarkdownConverter


class _ArticleConverter(MarkdownConverter):
    def convert_img(self, el, text, parent_tags):
        # Base64-inlined images are unusable in Markdown and can be megabytes.
        src = el.get("src") or ""
        if src.startswith("data:"):
            return ""
        return super().convert_img(el, text, parent_tags)


def html_to_markdown(html: str, *, strip_selectors: tuple[str, ...] = ()) -> str:
    if not html or not html.strip():
        return ""
    soup = BeautifulSoup(html, "html.parser")
    for node in soup.find_all(["script", "style", "form", "noscript"]):
        node.decompose()
    for selector in strip_selectors:
        for node in soup.select(selector):
            node.decompose()
    md = _ArticleConverter(heading_style="ATX", bullets="-").convert_soup(soup)
    md = re.sub(r"[ \t]+\n", "\n", md)
    return re.sub(r"\n{3,}", "\n\n", md).strip()
```

`src/ustc_crawler/extract.py`:顶部 import 加 `from .markdown import html_to_markdown`;`ArticleDocument` 构造处改为:

```python
            body_markdown=html_to_markdown(body_html),
```

- [ ] **Step 4: 运行确认通过 + 全量回归**

Run: `uv run pytest -q`
Expected: 全部通过。注意:若有旧测试断言 `body_markdown == body_text`,将其更新为对 Markdown 的合理断言。

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml uv.lock src/ustc_crawler/markdown.py src/ustc_crawler/extract.py tests/test_markdown.py
git commit -m "feat(extract): emit real Markdown for article bodies"
```

---

### Task 3: 适配器基类、注册表与 extract_page 钩子

**Files:**
- Create: `src/ustc_crawler/adapters/__init__.py`、`src/ustc_crawler/adapters/base.py`
- Modify: `src/ustc_crawler/extract.py`(`extract_page` 末尾、`return PageDocument(...)` 之前)
- Test: `tests/test_adapters.py`(新建)

**Interfaces:**
- Consumes: `html_to_markdown`(Task 2)
- Produces:
  - `ArticleFields` dataclass:`title: str, published_at: str, body_html: str, author: str = "", category: str = "", summary: str = ""`
  - `SiteAdapter`:属性 `name: str`,方法 `extract(self, url: str, html: str) -> ArticleFields | None`
  - `adapter_for(source_id: str, url: str) -> SiteAdapter | None`
  - `register(adapter: SiteAdapter) -> None`(Task 4-7 的适配器模块在被 import 时注册)

- [ ] **Step 1: 写失败测试**

```python
# tests/test_adapters.py
import unittest

from ustc_crawler.adapters import adapter_for, register
from ustc_crawler.adapters.base import ArticleFields, SiteAdapter
from ustc_crawler.extract import extract_page

PROBE_HTML = """<html><body><div class="probe-body"><h1>探针标题</h1>
<p>发布时间：2026-09-01</p><div class="probe-content"><p>这是探针正文内容,长度足够二十个字以上。</p></div>
</div></body></html>"""


class ProbeAdapter(SiteAdapter):
    name = "probe"
    source_ids = ("probe-source",)

    def extract(self, url: str, html: str) -> ArticleFields | None:
        if "probe-content" not in html:
            return None
        return ArticleFields(
            title="探针标题",
            published_at="2026-09-01",
            body_html="<p>这是探针正文内容,长度足够二十个字以上。</p>",
        )


class AdapterRegistryTests(unittest.TestCase):
    def test_unknown_source_falls_back_to_none(self) -> None:
        self.assertIsNone(adapter_for("news", "https://news.ustc.edu.cn/info/1/2.htm"))

    def test_registered_adapter_matches_source_id(self) -> None:
        register(ProbeAdapter())
        self.assertEqual(adapter_for("probe-source", "https://x.test/1").name, "probe")

    def test_extract_page_prefers_adapter_result(self) -> None:
        register(ProbeAdapter())
        page = extract_page("https://x.test/whatever", PROBE_HTML, source_id="probe-source")
        self.assertIsNotNone(page.article)
        self.assertEqual(page.article.title, "探针标题")
        self.assertEqual(page.article.published_at, "2026-09-01")
        self.assertEqual(page.article.extraction_method, "adapter:probe")

    def test_adapter_none_keeps_generic_result(self) -> None:
        register(ProbeAdapter())
        page = extract_page("https://x.test/no-match", "<html><body><p>x</p></body></html>",
                            source_id="probe-source")
        self.assertIsNone(page.article)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_adapters.py -q`
Expected: FAIL(ModuleNotFoundError: ustc_crawler.adapters)

- [ ] **Step 3: 实现**

`src/ustc_crawler/adapters/base.py`:

```python
"""Per-site extraction adapters.

An adapter recognizes article pages for one site (or one CMS family) and
extracts fields with site-specific selectors.  Sites without an adapter keep
the generic heuristic path unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..markdown import html_to_markdown


@dataclass(slots=True, frozen=True)
class ArticleFields:
    title: str
    published_at: str
    body_html: str
    author: str = ""
    category: str = ""
    summary: str = ""


class SiteAdapter:
    name: str = ""
    source_ids: tuple[str, ...] = ()
    hosts: tuple[str, ...] = ()

    def extract(self, url: str, html: str) -> ArticleFields | None:
        raise NotImplementedError


```

`src/ustc_crawler/adapters/__init__.py`:

```python
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
```

`src/ustc_crawler/extract.py`:`extract_page` 里 `return PageDocument(...)` 之前(`article` 已定型后)插入:

```python
    adapter = adapter_for(source_id, url)
    if adapter is not None:
        try:
            fields = adapter.extract(url, html)
        except Exception:
            fields = None
        if fields is not None and fields.title.strip():
            body_text = re.sub(
                r"\n{3,}", "\n\n",
                BeautifulSoup(fields.body_html, "html.parser").get_text("\n", strip=True),
            ).strip()
            article = ArticleDocument(
                url=canonical or url,
                source_id=source_id,
                title=fields.title,
                author=fields.author,
                published_at=fields.published_at,
                updated_at=updated,
                category=fields.category,
                summary=fields.summary,
                body_html=fields.body_html,
                body_text=body_text,
                body_markdown=html_to_markdown(fields.body_html),
                extraction_method=f"adapter:{adapter.name}",
                source_page_url=url,
                raw_metadata={"jsonld": metadata},
                images=images,
            )
```

(import 处加 `from .adapters import adapter_for`;`BeautifulSoup`、`re` 已存在。)

- [ ] **Step 4: 运行确认通过 + 全量回归**

Run: `uv run pytest -q`
Expected: 全部通过

- [ ] **Step 5: Commit**

```bash
git add src/ustc_crawler/adapters/ src/ustc_crawler/extract.py tests/test_adapters.py
git commit -m "feat(extract): add per-site adapter registry with generic fallback"
```

---

### Task 4: vsb CMS 家族适配器(修 mech/iid)

**Files:**
- Create: `src/ustc_crawler/adapters/vsb.py`
- Fixture: `tests/fixtures/adapters/vsb/mech.html`
- Test: `tests/test_adapters_vsb.py`(新建)

**Interfaces:**
- Consumes: `SiteAdapter`, `ArticleFields`, `register`(Task 3)
- Produces: `VsbCmsAdapter`,注册 hosts `mech.ustc.edu.cn`、`iid.ustc.edu.cn`

- [ ] **Step 1: 抓取 fixture**

```bash
mkdir -p tests/fixtures/adapters/vsb
UA='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36'
curl -s -L -A "$UA" 'http://mech.ustc.edu.cn/2022/0331/c4596a550763/page.htm' -o tests/fixtures/adapters/vsb/mech.html
```

已验证该页结构:标题 `.arti_title`,日期 `发布时间：2022-03-31`,正文 `.wp_articlecontent`。

- [ ] **Step 2: 写失败测试**

```python
class VsbAdapterTests(unittest.TestCase):
    def test_mech_fixture(self) -> None:
        from pathlib import Path
        from ustc_crawler.adapters.vsb import VsbCmsAdapter
        html = Path("tests/fixtures/adapters/vsb/mech.html").read_text(encoding="utf-8")
        adapter = VsbCmsAdapter()
        fields = adapter.extract(
            "https://mech.ustc.edu.cn/2022/0331/c4596a550763/page.htm", html)
        self.assertIsNotNone(fields)
        self.assertEqual(fields.title, "近代力学系教师例会（春季学期3月份）")
        self.assertEqual(fields.published_at, "2022-03-31")
        self.assertIn("教师例会", fields.body_html)

    def test_non_article_page_returns_none(self) -> None:
        from ustc_crawler.adapters.vsb import VsbCmsAdapter
        adapter = VsbCmsAdapter()
        self.assertIsNone(adapter.extract(
            "https://mech.ustc.edu.cn/", "<html><body><p>首页</p></body></html>"))
```

- [ ] **Step 3: 运行确认失败**

Run: `uv run pytest tests/test_adapters.py -k vsb -q`
Expected: FAIL(ModuleNotFoundError)

- [ ] **Step 4: 实现**

`src/ustc_crawler/adapters/vsb.py`:

```python
"""Adapter for the university-wide Vsb CMS (most department sites)."""

from __future__ import annotations

import re

from bs4 import BeautifulSoup

from .base import ArticleFields, SiteAdapter

_ARTICLE_URL = re.compile(r"/20\d{2}/\d{4}/c\d+a\d+/", re.I)
_DATE_LABEL = re.compile(r"发布时间[：:]\s*(20\d{2})[-年/](\d{1,2})[-月/](\d{1,2})")


class VsbCmsAdapter(SiteAdapter):
    name = "vsb"
    hosts = ("mech.ustc.edu.cn", "iid.ustc.edu.cn")

    def extract(self, url: str, html: str) -> ArticleFields | None:
        if not _ARTICLE_URL.search(url):
            return None
        soup = BeautifulSoup(html, "html.parser")
        title_node = soup.select_one(".arti_title")
        body = soup.select_one(".wp_articlecontent")
        if title_node is None or body is None:
            return None
        title = title_node.get_text(" ", strip=True)
        if not title:
            return None
        match = _DATE_LABEL.search(soup.get_text(" ", strip=True))
        published = (
            f"{match.group(1)}-{int(match.group(2)):02d}-{int(match.group(3)):02d}"
            if match else ""
        )
        return ArticleFields(title=title, published_at=published, body_html=str(body))
```

模块末尾:

```python
from . import register  # noqa: E402

register(VsbCmsAdapter())
```

并在 `adapters/__init__.py` 末尾 `from . import vsb  # noqa: F401`(确保注册)。

- [ ] **Step 5: 运行确认通过 + 全量回归**

Run: `uv run pytest -q`
Expected: 全部通过

- [ ] **Step 6: Commit**

```bash
git add src/ustc_crawler/adapters/ tests/
git commit -m "feat(adapters): vsb CMS family adapter (mech, iid)"
```

---

### Task 5: WordPress 家族适配器(修 library)

**Files:**
- Create: `src/ustc_crawler/adapters/wordpress.py`
- Fixture: `tests/fixtures/adapters/wordpress/lib.html`
- Test: `tests/test_adapters_wordpress.py`(新建)

**Interfaces:**
- Consumes: 同 Task 3。Produces: `WordPressAdapter`,注册 host `lib.ustc.edu.cn`

- [ ] **Step 1: 抓取 fixture**

```bash
mkdir -p tests/fixtures/adapters/wordpress
curl -s -L -A "$UA" 'https://lib.ustc.edu.cn/?p=6092' -o tests/fixtures/adapters/wordpress/lib.html
```

已验证结构:容器 `div.detail-content` → `div.detail-text`(内有 `h1` 标题 + `h2` 日期 + 正文)。

- [ ] **Step 2: 写失败测试**

```python
class WordPressAdapterTests(unittest.TestCase):
    def test_library_fixture(self) -> None:
        from pathlib import Path
        from ustc_crawler.adapters.wordpress import WordPressAdapter
        html = Path("tests/fixtures/adapters/wordpress/lib.html").read_text(encoding="utf-8")
        fields = WordPressAdapter().extract("https://lib.ustc.edu.cn/?p=6092", html)
        self.assertIsNotNone(fields)
        self.assertEqual(fields.title, "【公告】致新生读者")
        self.assertEqual(fields.published_at, "2008-08-31")
        self.assertIn("一卡通", fields.body_html)

    def test_homepage_returns_none(self) -> None:
        from ustc_crawler.adapters.wordpress import WordPressAdapter
        self.assertIsNone(WordPressAdapter().extract(
            "https://lib.ustc.edu.cn/", "<html><body><p>首页</p></body></html>"))
```

- [ ] **Step 3: 运行确认失败**

Run: `uv run pytest tests/test_adapters.py -k wordpress -q`
Expected: FAIL

- [ ] **Step 4: 实现**

`src/ustc_crawler/adapters/wordpress.py`:

```python
"""Adapter for WordPress-based sites (library and friends)."""

from __future__ import annotations

import re

from bs4 import BeautifulSoup

from .base import ArticleFields, SiteAdapter

_ISO_DATE = re.compile(r"(20\d{2})-(\d{2})-(\d{2})")


class WordPressAdapter(SiteAdapter):
    name = "wordpress"
    hosts = ("lib.ustc.edu.cn",)

    def extract(self, url: str, html: str) -> ArticleFields | None:
        soup = BeautifulSoup(html, "html.parser")
        container = soup.select_one("div.detail-text") or soup.select_one(
            "div.detail-content"
        )
        if container is None:
            return None
        title_node = container.find("h1")
        date_node = container.find("h2", string=_ISO_DATE)
        if title_node is None or date_node is None:
            return None
        title = title_node.get_text(" ", strip=True)
        if not title:
            return None
        published = _ISO_DATE.search(date_node.get_text()).group(0)
        for node in (title_node, date_node):
            node.extract()
        body = container.get_text(" ", strip=True)
        if len(body) < 10:
            return None
        return ArticleFields(title=title, published_at=published, body_html=str(container))
```

模块末尾注册 + `__init__.py` import(同 Task 4 模式)。

- [ ] **Step 5: 运行确认通过 + 全量回归;Commit**

```bash
uv run pytest -q
git add src/ustc_crawler/adapters/ tests/
git commit -m "feat(adapters): WordPress family adapter (library)"
```

---

### Task 6: jhtml CMS 适配器(修 iat)

**Files:**
- Create: `src/ustc_crawler/adapters/jhtml.py`
- Fixture: `tests/fixtures/adapters/jhtml/iat.html`
- Test: `tests/test_adapters_jhtml.py`(新建)

**Interfaces:**
- Produces: `JhtmlAdapter`,注册 host `iat.ustc.edu.cn`

- [ ] **Step 1: 抓取 fixture**

```bash
mkdir -p tests/fixtures/adapters/jhtml
curl -s -L -A "$UA" 'http://iat.ustc.edu.cn/iat/xwdt/20230314/6731.html' -o tests/fixtures/adapters/jhtml/iat.html
```

已验证结构:标题 `p.News-detail-title`,元信息 `div.news-detail-notes`(含 `发布时间：2023-03-14 00:00:00`),正文 `div.news-detail-news-con`。

- [ ] **Step 2: 写失败测试**

```python
class JhtmlAdapterTests(unittest.TestCase):
    def test_iat_fixture(self) -> None:
        from pathlib import Path
        from ustc_crawler.adapters.jhtml import JhtmlAdapter
        html = Path("tests/fixtures/adapters/jhtml/iat.html").read_text(encoding="utf-8")
        fields = JhtmlAdapter().extract(
            "https://iat.ustc.edu.cn/iat/xwdt/20230314/6731.html", html)
        self.assertIsNotNone(fields)
        self.assertIn("创客嘉年华", fields.title)
        self.assertEqual(fields.published_at, "2023-03-14")
        self.assertIn("科大讯飞", fields.body_html)

    def test_listing_page_returns_none(self) -> None:
        from ustc_crawler.adapters.jhtml import JhtmlAdapter
        self.assertIsNone(JhtmlAdapter().extract(
            "https://iat.ustc.edu.cn/iat/xwdt/", "<html><body><p>列表</p></body></html>"))
```

- [ ] **Step 3: 运行确认失败;Step 4: 实现**

`src/ustc_crawler/adapters/jhtml.py`:

```python
"""Adapter for the .jhtml CMS used by iat.ustc.edu.cn and friends."""

from __future__ import annotations

import re

from bs4 import BeautifulSoup

from .base import ArticleFields, SiteAdapter

_DATE_LABEL = re.compile(r"发布时间[：:]\s*(20\d{2})-(\d{2})-(\d{2})")


class JhtmlAdapter(SiteAdapter):
    name = "jhtml"
    hosts = ("iat.ustc.edu.cn",)

    def extract(self, url: str, html: str) -> ArticleFields | None:
        soup = BeautifulSoup(html, "html.parser")
        title_node = soup.select_one("p.News-detail-title")
        body = soup.select_one("div.news-detail-news-con")
        if title_node is None or body is None:
            return None
        title = title_node.get_text(" ", strip=True)
        if not title:
            return None
        notes = soup.select_one("div.news-detail-notes")
        published = ""
        author = ""
        if notes is not None:
            match = _DATE_LABEL.search(notes.get_text(" ", strip=True))
            if match:
                published = match.group(0).split("：", 1)[-1].strip()[:10]
        return ArticleFields(
            title=title, published_at=published, author=author, body_html=str(body)
        )
```

注册 + import(同 Task 4)。

- [ ] **Step 5: 测试 + 回归 + Commit**

```bash
uv run pytest -q
git add src/ustc_crawler/adapters/ tests/
git commit -m "feat(adapters): jhtml CMS adapter (iat)"
```

---

### Task 7: course 与 yz 侦察 + 适配

**Files:**
- 视侦察结果:`src/ustc_crawler/adapters/sites.py` 或归入既有家族
- Test: `tests/test_adapters.py` 追加

- [ ] **Step 1: 侦察**

```bash
UA='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36'
curl -s -L -A "$UA" 'https://course.ustc.edu.cn/portal' -o /tmp/course.html
curl -s -L -A "$UA" 'https://yz.ustc.edu.cn/' -o /tmp/yz.html
sqlite3 data/crawler.sqlite "SELECT url FROM pages WHERE url LIKE '%course.ustc.edu.cn%' LIMIT 20"
sqlite3 data/crawler.sqlite "SELECT url FROM frontier WHERE url LIKE '%yz.ustc.edu.cn%' AND status='done' LIMIT 20"
```

对两个站回答:文章详情页 URL 长什么样?标题/日期/正文容器是什么?首页是否有新文章列表(还是 JS 渲染)?
yz 已有 6 篇文章且 URL 为 `/article/NNNN/NNN`(通用模式已能分类),重点查**发现**环节:首页/栏目页上文章链接是否在 HTML 里。
course 若内容需登录或纯 JS,记录结论并从本阶段移除(写进审计报告)。

- [ ] **Step 2: 按侦察结果写适配器(TDD 同 Task 4-6 模式)**;若结论是"内容本就少/需登录",则在审计报告中说明,不写适配器。

- [ ] **Step 3: 测试 + 回归 + Commit**

---

### Task 8: 全源提取率审计

**Files:**
- Create: `data/extraction_audit.py`(一次性脚本,不入库,参照 `data/seed_export.py` 惯例)

- [ ] **Step 1: 写并运行审计脚本**

```python
# data/extraction_audit.py
"""One-off: per-source extraction-rate report (pages fetched vs articles kept)."""
import sqlite3

con = sqlite3.connect("data/crawler.sqlite")
rows = con.execute("""
    SELECT s.id,
           (SELECT COUNT(*) FROM pages p WHERE p.url LIKE '%' || sh.host || '%') AS pages,
           (SELECT COUNT(*) FROM articles a WHERE a.source_id = s.id) AS articles
    FROM sources s
    JOIN (SELECT DISTINCT source_id,
                 REPLACE(REPLACE(url, 'https://', ''), 'http://', '') AS host
          FROM frontier) sh ON sh.source_id = s.id
    ORDER BY pages DESC
""").fetchall()
print(f"{'source':40s} {'pages':>8s} {'articles':>8s} {'rate':>7s}")
for sid, pages, articles in rows:
    rate = articles / pages if pages else 0
    flag = " <-- LOW" if pages > 500 and rate < 0.05 else ""
    print(f"{sid:40s} {pages:8d} {articles:8d} {rate:7.1%}{flag}")
```

(若 SQL 关联不准,退化为按 `urlsplit` 在 Python 里聚合 host → source。)

- [ ] **Step 2: 对 LOW 源逐一定性**:内容本就少 / 需登录 / 提取失败。提取失败的按家族补适配器(回到 Task 4-6 模式,一个站一个 fixture + 测试)。

- [ ] **Step 3: 把审计结论写入 commit message 或 PR 描述**(脚本本身不入库)。

---

### Task 9: 验收 — 重爬缺口站 + 端到端验证 + PR

- [ ] **Step 1: 对缺口源做定向重爬**(检查 CLI 是否支持按源过滤:`grep -n 'source' src/ustc_crawler/cli.py`;不支持则用全量增量爬,适配器生效后缺口站文章数应上升)

```bash
uv run ustc-crawler crawl --incremental
sqlite3 data/crawler.sqlite "SELECT source_id, COUNT(*) FROM articles WHERE source_id IN ('library','unit-mech-ustc-edu-cn','unit-iid-ustc-edu-cn','unit-iat-ustc-edu-cn') GROUP BY source_id;"
```

预期:library ≥ 1000,其余三个 > 0。

- [ ] **Step 2: 抽查正确性**(对 library/iat/mech 各取 3 篇新文章,人工比对原页标题/日期)

- [ ] **Step 3: 全量测试 + lint**

```bash
uv run pytest -q && uv run ruff check src tests
```

- [ ] **Step 4: 小批量 sync 验证平台协议不回归**(本地 sync,观察 Markdown 对象上行,平台 200)

- [ ] **Step 5: 推分支、开 PR、等 CI 绿、合并**

```bash
git push -u origin feat/site-adapters-markdown
gh pr create --title "feat: per-site extraction adapters + real Markdown (phase 1)"
gh pr checks --watch
gh pr merge --squash --delete-branch
```
