from __future__ import annotations

import html
import json
import mimetypes
from datetime import date, timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from shutil import copyfileobj
from urllib.parse import parse_qs, quote, unquote, urljoin, urlparse, urlsplit

from bs4 import BeautifulSoup
from sqlalchemy import create_engine, event
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.pool import NullPool

from .db import ALEMBIC_HEAD
from .publication import publication_type_sql

_publication_type_sql = publication_type_sql


class DashboardStore:
    """Small read-only query layer for the local crawl dashboard."""

    def __init__(self, db_path: str | Path, data_dir: str | Path) -> None:
        self.db_path = Path(db_path).resolve()
        self.data_dir = Path(data_dir).resolve()
        if not self.db_path.is_file():
            raise FileNotFoundError(f"SQLite database does not exist: {self.db_path}")
        self.engine = create_engine(
            f"sqlite:///file:{self.db_path.as_posix()}?mode=ro&uri=true",
            connect_args={"check_same_thread": False, "timeout": 5, "uri": True},
            poolclass=NullPool,
        )
        event.listen(self.engine, "connect", self._configure_read_only)
        try:
            version = self._fetchone("SELECT version_num FROM alembic_version")
        except SQLAlchemyError as exc:
            self.engine.dispose()
            raise RuntimeError(
                "SQLite schema is not managed by Alembic; run ustc-crawler db-upgrade first"
            ) from exc
        if not version or version["version_num"] != ALEMBIC_HEAD:
            raise RuntimeError(
                f"SQLite schema revision {version.get('version_num') if version else None!r} "
                f"is not {ALEMBIC_HEAD!r}; run ustc-crawler db-upgrade first"
            )

    @staticmethod
    def _configure_read_only(dbapi_connection: object, _connection_record: object) -> None:
        cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
        try:
            cursor.execute("PRAGMA query_only=ON")
            cursor.execute("PRAGMA busy_timeout=5000")
        finally:
            cursor.close()

    def _fetchall(self, query: str, params: tuple[object, ...] = ()) -> list[dict[str, object]]:
        with self.engine.connect() as connection:
            return [dict(row) for row in connection.exec_driver_sql(query, params).mappings().all()]

    def _fetchone(self, query: str, params: tuple[object, ...] = ()) -> dict[str, object] | None:
        rows = self._fetchall(query, params)
        return rows[0] if rows else None

    def close(self) -> None:
        self.engine.dispose()

    @staticmethod
    def cutoff() -> str:
        return (date.today() - timedelta(days=365)).isoformat()

    @staticmethod
    def today() -> str:
        return date.today().isoformat()

    def summary(self) -> dict[str, object]:
        cutoff = self.cutoff()
        today = self.today()
        queries = {
            "sources": ("SELECT COUNT(*) AS value FROM sources", ()),
            "frontier": ("SELECT COUNT(*) AS value FROM frontier", ()),
            "pending": ("SELECT COUNT(*) AS value FROM frontier WHERE status='pending'", ()),
            "filtered": ("SELECT COUNT(*) AS value FROM frontier WHERE status='filtered'", ()),
            "pages": ("SELECT COUNT(*) AS value FROM pages", ()),
            "pages_indexable": ("SELECT COUNT(*) AS value FROM pages WHERE value_score >= 16", ()),
            "news_pages": ("SELECT COUNT(*) AS value FROM pages WHERE page_kind='news_article'", ()),
            "news_indexable": ("""
                SELECT COUNT(*) AS value FROM pages
                WHERE page_kind='news_article' AND value_score >= 40
                  AND (duplicate_of IS NULL OR duplicate_of='')
            """, ()),
            "recent_news": ("""
                SELECT COUNT(*) AS value FROM pages
                WHERE page_kind='news_article' AND published_at >= ? AND published_at <= ?
                  AND value_score >= 40
                  AND (duplicate_of IS NULL OR duplicate_of='')
            """, (cutoff, today)),
            "articles": ("SELECT COUNT(*) AS value FROM articles", ()),
            "recent_articles": ("SELECT COUNT(*) AS value FROM articles WHERE published_at >= ? AND published_at <= ?", (cutoff, today)),
            "media": ("SELECT COUNT(*) AS value FROM media", ()),
            "media_ok": ("SELECT COUNT(*) AS value FROM media WHERE status='ok'", ()),
            "assets": ("SELECT COUNT(*) AS value FROM assets", ()),
            "article_media": ("SELECT COUNT(*) AS value FROM article_media", ()),
            "failures": ("SELECT COUNT(*) AS value FROM failures", ()),
            "last_fetched": ("SELECT MAX(fetched_at) AS value FROM pages", ()),
        }
        result: dict[str, object] = {"cutoff": cutoff, "as_of": today}
        for name, (query, params) in queries.items():
            row = self._fetchone(query, params)
            result[name] = row["value"] if row else 0
        result["tiers"] = self._fetchall(
            "SELECT value_tier AS tier, COUNT(*) AS count FROM pages GROUP BY value_tier ORDER BY count DESC",
        )
        result["kinds"] = self._fetchall(
            "SELECT page_kind AS kind, COUNT(*) AS count FROM pages GROUP BY page_kind ORDER BY count DESC LIMIT 12",
        )
        return result

    def sources(self, limit: int = 130) -> list[dict[str, object]]:
        cutoff = self.cutoff()
        today = self.today()
        query = """
            WITH page_stats AS (
              SELECT source_id, COUNT(*) AS pages,
                SUM(page_kind='news_article' AND value_score >= 40
                    AND (duplicate_of IS NULL OR duplicate_of='')) AS news_pages,
                SUM(page_kind='news_article' AND published_at >= ? AND published_at <= ? AND value_score >= 40
                    AND (duplicate_of IS NULL OR duplicate_of='')) AS recent_news
              FROM pages GROUP BY source_id
            ), article_stats AS (
              SELECT source_id, COUNT(*) AS articles,
                SUM(published_at >= ? AND published_at <= ?) AS recent_articles
              FROM articles GROUP BY source_id
            ), frontier_stats AS (
              SELECT source_id, COUNT(*) AS pending
              FROM frontier WHERE status='pending' GROUP BY source_id
            )
            SELECT s.id, s.name, s.organization_level,
              COALESCE(ps.pages, 0) AS pages, COALESCE(ps.news_pages, 0) AS news_pages,
              COALESCE(ps.recent_news, 0) AS recent_news, COALESCE(ast.articles, 0) AS articles,
              COALESCE(ast.recent_articles, 0) AS recent_articles,
              COALESCE(fs.pending, 0) AS pending
            FROM sources s
            LEFT JOIN page_stats ps ON ps.source_id=s.id
            LEFT JOIN article_stats ast ON ast.source_id=s.id
            LEFT JOIN frontier_stats fs ON fs.source_id=s.id
            ORDER BY news_pages DESC, recent_news DESC, pages DESC, s.name
            LIMIT ?
        """
        return self._fetchall(query, (cutoff, today, cutoff, today, max(1, min(limit, 500))))

    def news(
        self,
        query_text: str = "",
        source_id: str = "",
        page: int = 1,
        page_size: int = 30,
        publication_type: str = "",
    ) -> tuple[list[dict[str, object]], int, int]:
        page = max(1, page)
        page_size = max(1, min(page_size, 100))
        clauses = [
            "s.discovery_only = 0",
            "(p.url IS NULL OR (COALESCE(p.page_kind, '') IN ('news_article', 'article') "
            "AND (p.duplicate_of IS NULL OR p.duplicate_of='')))",
        ]
        params: list[object] = []
        if source_id:
            clauses.append("a.source_id = ?")
            params.append(source_id)
        if query_text:
            clauses.append("(a.title LIKE ? OR a.summary LIKE ? OR a.body_text LIKE ?)")
            needle = f"%{query_text}%"
            params.extend([needle, needle, needle])
        type_expression = _publication_type_sql()
        where = " AND ".join(clauses)
        from_clause = (
            "FROM articles a JOIN sources s ON s.id=a.source_id "
            "LEFT JOIN pages p ON p.url=COALESCE(NULLIF(a.source_page_url, ''), a.url)"
        )
        base_params = tuple(params)
        type_where = " AND publication_type=?" if publication_type in {"news", "notice"} else ""
        type_params: tuple[object, ...] = (publication_type,) if type_where else ()
        count_row = self._fetchone(
            f"""
            SELECT COUNT(*) AS value FROM (
              SELECT {type_expression} AS publication_type,
                     ROW_NUMBER() OVER (
                       PARTITION BY a.source_id, COALESCE(NULLIF(a.content_hash, ''), a.url)
                       ORDER BY a.url
                     ) AS dedupe_rank
              {from_clause}
              WHERE {where}
            ) visible
            WHERE dedupe_rank=1{type_where}
            """,
            base_params + type_params,
        )
        total = int(count_row["value"] if count_row else 0)
        rows = self._fetchall(
            f"""
            SELECT visible.url, visible.source_id, visible.source_name, visible.title, visible.author,
                   visible.published_at, visible.updated_at, visible.category, visible.summary,
                   visible.excerpt, visible.publication_type
            FROM (
              SELECT a.url, a.source_id, s.name AS source_name, a.title, a.author,
                     a.published_at, a.updated_at, a.category, a.summary,
                     substr(a.body_text, 1, 240) AS excerpt,
                     {type_expression} AS publication_type,
                     ROW_NUMBER() OVER (
                       PARTITION BY a.source_id, COALESCE(NULLIF(a.content_hash, ''), a.url)
                       ORDER BY a.url
                     ) AS dedupe_rank
              {from_clause}
              WHERE {where}
            ) visible
            WHERE visible.dedupe_rank=1{type_where}
            ORDER BY visible.published_at DESC, visible.url
            LIMIT ? OFFSET ?
            """,
            base_params + type_params + (page_size, (page - 1) * page_size),
        )
        return rows, total, page

    def article(self, url: str) -> dict[str, object] | None:
        type_expression = _publication_type_sql()
        row = self._fetchone(
            f"""
            SELECT a.*, s.name AS source_name, {type_expression} AS publication_type
            FROM articles a JOIN sources s ON s.id=a.source_id
            LEFT JOIN pages p ON p.url=COALESCE(NULLIF(a.source_page_url, ''), a.url)
            WHERE a.url=? AND s.discovery_only = 0
            """,
            (url,),
        )
        if row is None:
            return None
        images = self._fetchall(
            """
            SELECT am.image_url, am.local_path, am.alt, am.title, am.caption,
                   m.mime_type, m.status
            FROM article_media am LEFT JOIN media m ON m.url=am.image_url
            WHERE am.article_url=? ORDER BY am.image_url
            """,
            (url,),
        )
        row["images"] = images
        return row

    def local_path(self, value: str, kind: str) -> Path | None:
        """Resolve only files inside data/media or data/assets."""
        raw = Path(unquote(value))
        root = self.data_dir / kind
        if raw.is_absolute():
            candidate = raw.resolve()
        elif raw.parts and raw.parts[0] == self.data_dir.name:
            candidate = (self.data_dir.parent / raw).resolve()
        else:
            candidate = (self.data_dir / raw).resolve()
        try:
            candidate.relative_to(root.resolve())
        except ValueError:
            return None
        return candidate if candidate.is_file() else None


def _escape(value: object) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _number(value: object) -> str:
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return "0"


def _truncate(value: object, length: int = 180) -> str:
    text = "" if value is None else str(value).strip()
    return text if len(text) <= length else f"{text[: length - 1]}…"


def _layout(title: str, content: str, active: str = "/") -> str:
    def nav_link(path: str, label: str) -> str:
        class_name = ' class="active"' if active == path else ""
        return f'<a href="{path}"{class_name}>{label}</a>'

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{_escape(title)} · USTC Crawl Explorer</title>
  <style>
    :root {{
      color-scheme: light;
      --ink: #14213d;
      --muted: #65748b;
      --line: #dce3ed;
      --wash: #f6f8fb;
      --blue: #2364d2;
      --blue-soft: #eaf1ff;
      --green: #198754;
      --shadow: 0 14px 34px rgba(20, 33, 61, .07);
    }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; color: var(--ink); background: white; font: 15px/1.55 Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }}
    a {{ color: var(--blue); text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    .shell {{ max-width: 1440px; margin: 0 auto; padding: 28px 34px 64px; }}
    header {{ display: flex; justify-content: space-between; align-items: end; gap: 24px; padding-bottom: 22px; border-bottom: 1px solid var(--line); }}
    h1, h2, h3 {{ margin: 0; letter-spacing: -.025em; }}
    h1 {{ font-size: clamp(27px, 4vw, 44px); line-height: 1.08; }}
    h2 {{ font-size: 22px; }}
    h3 {{ font-size: 16px; }}
    .subtitle {{ margin: 9px 0 0; color: var(--muted); max-width: 720px; }}
    nav {{ display: flex; flex-wrap: wrap; gap: 16px; font-size: 14px; white-space: nowrap; }}
    nav a {{ color: var(--ink); }}
    nav a.active {{ color: var(--blue); font-weight: 700; }}
    main {{ padding-top: 26px; }}
    .metrics {{ display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; margin-bottom: 28px; }}
    .metric {{ padding: 18px 20px; background: var(--wash); border: 1px solid var(--line); box-shadow: var(--shadow); }}
    .metric .label {{ color: var(--muted); font-size: 13px; }}
    .metric .value {{ margin-top: 4px; font-size: 30px; line-height: 1.1; font-weight: 750; }}
    .metric .hint {{ margin-top: 6px; color: var(--muted); font-size: 12px; }}
    .columns {{ display: grid; grid-template-columns: minmax(0, 1.08fr) minmax(0, .92fr); gap: 26px; align-items: start; }}
    .section {{ margin-top: 32px; }}
    .section-head {{ display: flex; justify-content: space-between; align-items: baseline; gap: 14px; margin-bottom: 12px; }}
    .section-head p {{ margin: 0; color: var(--muted); font-size: 13px; }}
    .table-wrap {{ overflow-x: auto; border-top: 1px solid var(--line); border-bottom: 1px solid var(--line); }}
    table {{ width: 100%; border-collapse: collapse; min-width: 620px; }}
    th, td {{ padding: 11px 10px; border-bottom: 1px solid var(--line); text-align: left; vertical-align: top; }}
    th {{ color: var(--muted); font-size: 12px; font-weight: 650; letter-spacing: .02em; text-transform: uppercase; }}
    tr:last-child td {{ border-bottom: 0; }}
    td.num, th.num {{ text-align: right; white-space: nowrap; }}
    .muted {{ color: var(--muted); }}
    .small {{ font-size: 13px; }}
    .tag {{ display: inline-block; padding: 2px 8px; font-size: 12px; font-weight: 700; vertical-align: 1px; }}
    .tag.news {{ color: #1557b0; background: var(--blue-soft); }}
    .tag.notice {{ color: #8a4b08; background: #fff0dc; }}
    .news-list {{ border-top: 1px solid var(--line); }}
    .news-item {{ padding: 14px 0; border-bottom: 1px solid var(--line); }}
    .news-item h3 {{ font-size: 16px; line-height: 1.35; }}
    .news-meta {{ margin-top: 5px; color: var(--muted); font-size: 12px; }}
    .news-excerpt {{ margin: 7px 0 0; color: #42516a; font-size: 13px; }}
    .bar {{ display: flex; height: 10px; overflow: hidden; background: var(--line); }}
    .bar span {{ display: block; min-width: 2px; }}
    .bar .full {{ background: var(--blue); }} .bar .metadata {{ background: #6f9be7; }} .bar .audit {{ background: #a9bfe8; }} .bar .none {{ background: #d9e0ea; }}
    .legend {{ display: flex; flex-wrap: wrap; gap: 15px; margin-top: 8px; color: var(--muted); font-size: 12px; }}
    .legend i {{ display: inline-block; width: 9px; height: 9px; margin-right: 5px; vertical-align: -1px; }}
    .search {{ display: flex; flex-wrap: wrap; gap: 9px; margin-bottom: 18px; }}
    input, select, button {{ min-height: 38px; border: 1px solid var(--line); border-radius: 0; background: white; color: var(--ink); font: inherit; padding: 7px 10px; }}
    input[type=search] {{ flex: 1 1 300px; }}
    button {{ background: var(--ink); color: white; border-color: var(--ink); cursor: pointer; }}
    .pager {{ display: flex; justify-content: space-between; gap: 12px; margin-top: 16px; color: var(--muted); font-size: 13px; }}
    .article {{ max-width: 900px; }}
    .article h1 {{ font-size: clamp(27px, 4vw, 40px); }}
    .article-meta {{ display: flex; flex-wrap: wrap; gap: 12px 20px; margin: 12px 0 24px; padding-bottom: 15px; color: var(--muted); border-bottom: 1px solid var(--line); font-size: 13px; }}
    .article-body {{ overflow-wrap: anywhere; color: #26364f; font-size: 16px; line-height: 1.82; }}
    .article-body p {{ margin: 0 0 1em; }} .article-body h2, .article-body h3, .article-body h4 {{ margin: 1.4em 0 .55em; }}
    .article-body img {{ display: block; max-width: 100%; height: auto; margin: 1em auto; }}
    .article-body table {{ width: 100%; border-collapse: collapse; margin: 1em 0; }} .article-body th, .article-body td {{ border: 1px solid var(--line); padding: 7px; text-align: left; vertical-align: top; }}
    .article-body blockquote {{ margin: 1em 0; padding: .6em 1em; border-left: 3px solid var(--line); color: var(--muted); }}
    .article-body pre {{ overflow-x: auto; padding: 12px; background: var(--wash); }}
    .image-grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(170px, 1fr)); gap: 12px; margin: 26px 0; }}
    .image-grid figure {{ margin: 0; }} .image-grid img {{ display: block; width: 100%; max-height: 180px; object-fit: cover; background: var(--wash); }}
    figcaption {{ padding-top: 5px; color: var(--muted); font-size: 12px; }}
    .notice {{ padding: 13px 15px; border-left: 3px solid var(--blue); background: var(--blue-soft); color: #334566; }}
    code {{ padding: 1px 4px; background: var(--wash); font-size: .92em; }}
    footer {{ margin-top: 42px; padding-top: 14px; border-top: 1px solid var(--line); color: var(--muted); font-size: 12px; }}
    @media (max-width: 850px) {{ .shell {{ padding: 22px 18px 48px; }} header {{ align-items: start; flex-direction: column; }} .metrics {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }} .columns {{ grid-template-columns: 1fr; gap: 10px; }} }}
    @media (max-width: 460px) {{ .metrics {{ grid-template-columns: 1fr; }} nav {{ gap: 10px; white-space: normal; }} }}
  </style>
</head>
<body>
  <div class="shell">
    <header>
      <div><h1>USTC Crawl Explorer</h1><p class="subtitle">公开站点新闻、文章和文档的本地只读浏览器。</p></div>
      <nav>
        {nav_link('/', '概览')}{nav_link('/news', '新闻与通知')}{nav_link('/sources', '来源')}
      </nav>
    </header>
    <main>{content}</main>
    <footer>本页面只读取本地 SQLite；不会向来源站点发起请求。</footer>
  </div>
</body>
</html>"""


def _metric(label: str, value: object, hint: str = "") -> str:
    return f'<div class="metric"><div class="label">{_escape(label)}</div><div class="value">{_number(value)}</div><div class="hint">{_escape(hint)}</div></div>'


def _source_rows(rows: list[dict[str, object]]) -> str:
    if not rows:
        return '<tr><td colspan="5" class="muted">没有来源数据。</td></tr>'
    return "".join(
        f"""<tr>
          <td><a href="/news?source={quote(str(row['id']))}">{_escape(row['name'])}</a><div class="muted small">{_escape(row['organization_level'])}</div></td>
          <td class="num">{_number(row['news_pages'])}</td><td class="num">{_number(row['recent_news'])}</td>
          <td class="num">{_number(row['articles'])}</td><td class="num">{_number(row['pending'])}</td>
        </tr>"""
        for row in rows
    )


def _news_items(rows: list[dict[str, object]]) -> str:
    if not rows:
        return '<div class="notice">没有找到匹配的文章。</div>'
    return "".join(
        f"""<article class="news-item">
          <h3><span class="tag {_escape(row.get('publication_type') or 'news')}">{'通知' if row.get('publication_type') == 'notice' else '新闻'}</span> <a href="/article?url={quote(str(row['url']), safe='')}">{_escape(row['title'] or '未命名文章')}</a></h3>
          <div class="news-meta">{_escape(row['published_at'] or '日期未记录')} · {_escape(row['source_name'])}{(' · ' + _escape(row['author'])) if row.get('author') else ''}</div>
          {f'<p class="news-excerpt">{_escape(_truncate(row.get("excerpt")))}</p>' if row.get('excerpt') else ''}
        </article>"""
        for row in rows
    )


def render_home(store: DashboardStore) -> str:
    summary = store.summary()
    source_rows = store.sources(10)
    news_rows, _, _ = store.news(page=1, page_size=8)
    tiers = {str(row["tier"]): int(row["count"]) for row in summary["tiers"]}  # type: ignore[index]
    total = max(1, int(summary["pages"]))
    tier_html = (
        '<div class="bar">'
        f'<span class="full" style="width:{tiers.get("full_index", 0) / total * 100:.2f}%"></span>'
        f'<span class="metadata" style="width:{tiers.get("metadata_and_assets", 0) / total * 100:.2f}%"></span>'
        f'<span class="audit" style="width:{tiers.get("audit_only", 0) / total * 100:.2f}%"></span>'
        f'<span class="none" style="width:{tiers.get("not_indexed", 0) / total * 100:.2f}%"></span></div>'
        '<div class="legend"><span><i style="background:#2364d2"></i>完整索引</span><span><i style="background:#6f9be7"></i>元数据/附件</span><span><i style="background:#a9bfe8"></i>审计</span><span><i style="background:#d9e0ea"></i>未索引</span></div>'
    )
    content = f"""
      <section class="metrics">
        {_metric('来源站点', summary['sources'], '官方来源')}
        {_metric('已抓取页面', summary['pages'], 'SQLite 页面记录')}
        {_metric('可索引新闻', summary['news_indexable'], f"近一年 {summary['recent_news']:,} 页")}
        {_metric('待处理 URL', summary['pending'], '可断点续跑')}
      </section>
      <div class="columns">
        <section>
          <div class="section-head"><h2>来源排行</h2><p><a href="/sources">查看全部来源 →</a></p></div>
          <div class="table-wrap"><table><thead><tr><th>来源</th><th class="num">新闻页</th><th class="num">近一年</th><th class="num">文章</th><th class="num">待处理</th></tr></thead><tbody>{_source_rows(source_rows)}</tbody></table></div>
        </section>
        <section>
          <div class="section-head"><h2>最近文章</h2><p><a href="/news">检索全部 →</a></p></div>
          <div class="news-list">{_news_items(news_rows)}</div>
        </section>
      </div>
      <section class="section">
        <div class="section-head"><h2>页面价值分布</h2><p>按评分后的索引层级</p></div>
        {tier_html}
      </section>
      <section class="section">
        <div class="section-head"><h2>抓取说明</h2><p>更新时间 {_escape(summary.get('last_fetched') or '未知')}</p></div>
        <div class="notice">文章正文、标题、作者、发布时间、图片关联和公开文档均来自本地结构化数据；页面不会自动重新抓取网络。</div>
      </section>
    """
    return _layout("概览", content)


def render_news(store: DashboardStore, params: dict[str, list[str]]) -> str:
    query_text = params.get("q", [""])[0].strip()
    source_id = params.get("source", [""])[0].strip()
    publication_type = params.get("type", [""])[0].strip()
    if publication_type not in {"news", "notice"}:
        publication_type = ""
    try:
        page = int(params.get("page", ["1"])[0])
    except ValueError:
        page = 1
    rows, total, page = store.news(query_text, source_id, page, 30, publication_type)
    sources = store.sources(200)
    source_name = next((str(row["name"]) for row in sources if row["id"] == source_id), "全部来源")
    max_page = max(1, (total + 29) // 30)

    def page_link(target: int) -> str:
        values = {
            "q": query_text,
            "source": source_id,
            "type": publication_type,
            "page": str(target),
        }
        return "/news?" + "&".join(f"{quote(k)}={quote(v)}" for k, v in values.items() if v)

    content = f"""
      <section class="section" style="margin-top:0">
        <div class="section-head"><div><h2>新闻与通知</h2><p>共 {_number(total)} 条匹配记录 · 当前来源：{_escape(source_name)}</p></div></div>
        <form class="search" method="get" action="/news">
          <input type="search" name="q" value="{_escape(query_text)}" placeholder="搜索标题、摘要或正文">
          <select name="source"><option value="">全部来源</option>{''.join(f'<option value="{_escape(row["id"])}"{" selected" if row["id"] == source_id else ""}>{_escape(row["name"])}</option>' for row in sources)}</select>
          <select name="type"><option value="">新闻与通知</option><option value="news"{" selected" if publication_type == "news" else ""}>仅新闻</option><option value="notice"{" selected" if publication_type == "notice" else ""}>仅通知</option></select>
          <button type="submit">搜索</button>
        </form>
        <div class="news-list">{_news_items(rows)}</div>
        <div class="pager"><span>第 {page} / {max_page} 页</span><span>{f'<a href="{page_link(page - 1)}">← 上一页</a>' if page > 1 else ''}{'　' if page > 1 and page < max_page else ''}{f'<a href="{page_link(page + 1)}">下一页 →</a>' if page < max_page else ''}</span></div>
      </section>
    """
    return _layout("新闻与通知", content, "/news")


def render_sources(store: DashboardStore) -> str:
    rows = store.sources(200)
    content = f"""
      <section class="section" style="margin-top:0">
        <div class="section-head"><div><h2>来源统计</h2><p>按可索引新闻页、近一年新闻页和抓取队列排序。</p></div><p>{_number(len(rows))} 个来源</p></div>
        <div class="table-wrap"><table><thead><tr><th>来源</th><th class="num">页面</th><th class="num">可索引新闻</th><th class="num">近一年新闻</th><th class="num">文章</th><th class="num">待处理</th></tr></thead><tbody>{''.join(f'<tr><td><a href="/news?source={quote(str(row["id"]))}">{_escape(row["name"])}</a><div class="muted small">{_escape(row["organization_level"])}</div></td><td class="num">{_number(row["pages"])}</td><td class="num">{_number(row["news_pages"])}</td><td class="num">{_number(row["recent_news"])}</td><td class="num">{_number(row["articles"])}</td><td class="num">{_number(row["pending"])}</td></tr>' for row in rows)}</tbody></table></div>
      </section>
    """
    return _layout("来源", content, "/sources")


def render_article(article: dict[str, object]) -> str:
    images = article.get("images") or []
    image_html = ""
    if images:
        figures = []
        for image in images:
            if not isinstance(image, dict) or not image.get("local_path"):
                continue
            path = quote(str(image["local_path"]), safe="")
            caption = image.get("caption") or image.get("alt") or image.get("title") or ""
            figures.append(f'<figure><img loading="lazy" src="/media?path={path}" alt="{_escape(image.get("alt"))}"><figcaption>{_escape(caption)}</figcaption></figure>')
        if figures:
            image_html = '<div class="image-grid">' + "".join(figures) + "</div>"
    body_html = _safe_article_html(article)
    body = str(article.get("body_text") or article.get("summary") or "暂无正文。")
    body_markup = body_html or f"<p>{_escape(body)}</p>"
    content = f"""
      <article class="article section" style="margin-top:0">
        <p class="small"><a href="/news">← 返回新闻与通知</a></p>
        <h1>{_escape(article.get('title') or '未命名文章')}</h1>
        <div class="article-meta"><span class="tag {_escape(article.get('publication_type') or 'news')}">{'通知' if article.get('publication_type') == 'notice' else '新闻'}</span><span>{_escape(article.get('source_name'))}</span><span>发布时间：{_escape(article.get('published_at') or '未记录')}</span>{f'<span>作者：{_escape(article.get("author"))}</span>' if article.get('author') else ''}{f'<span>栏目：{_escape(article.get("category"))}</span>' if article.get('category') else ''}<a href="{_escape(article.get('url'))}" target="_blank" rel="noreferrer">查看原文 ↗</a></div>
        {f'<p class="subtitle">{_escape(article.get("summary"))}</p>' if article.get('summary') else ''}
        {image_html}
        <div class="article-body">{body_markup}</div>
      </article>
    """
    return _layout(str(article.get("title") or "文章"), content, "/news")


def _safe_article_html(article: dict[str, object]) -> str:
    """Render the stored content HTML without executing source-site markup."""

    raw = str(article.get("body_html") or "")
    if not raw:
        return ""
    soup = BeautifulSoup(raw, "html.parser")
    container = soup.body or soup
    allowed = {
        "a", "blockquote", "br", "code", "div", "em", "figcaption", "figure", "h1", "h2",
        "h3", "h4", "h5", "h6", "hr", "i", "img", "li", "ol", "p", "pre", "span",
        "strong", "table", "tbody", "td", "tfoot", "th", "thead", "tr", "u", "ul",
    }
    image_paths = {
        str(item.get("image_url")): str(item.get("local_path"))
        for item in article.get("images") or []
        if isinstance(item, dict) and item.get("image_url") and item.get("local_path")
    }
    for node in list(container.find_all(True)):
        # Decomposing or unwrapping an ancestor can detach descendants that
        # were already present in the snapshot returned by ``find_all``.
        # Detached nodes cannot be unwrapped again and used to turn otherwise
        # valid article pages into a dashboard 500 response.
        if node.parent is None:
            continue
        name = str(node.name).lower()
        if name in {"script", "style", "noscript", "template", "iframe", "object", "embed", "form", "input", "button"}:
            node.decompose()
            continue
        if name not in allowed:
            node.unwrap()
            continue
        for attribute in list(node.attrs):
            if attribute not in {"alt", "title", "colspan", "rowspan", "class", "href", "src"}:
                del node.attrs[attribute]
        if name == "a":
            href = str(node.get("href") or "")
            absolute = urljoin(str(article.get("url") or ""), href)
            scheme = urlsplit(absolute).scheme.lower()
            if not (absolute.startswith("#") or scheme in {"http", "https", "mailto"}):
                node.unwrap()
                continue
            node["href"] = absolute
            node["target"] = "_blank"
            node["rel"] = "noreferrer"
        elif name == "img":
            source = urljoin(str(article.get("url") or ""), str(node.get("src") or ""))
            local_path = image_paths.get(source)
            if not local_path:
                node.decompose()
                continue
            node.attrs = {key: value for key, value in node.attrs.items() if key in {"alt", "title"}}
            node["src"] = "/media?path=" + quote(local_path, safe="")
    return "".join(str(child) for child in container.contents).strip()


def _json_default(value: object) -> object:
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


class DashboardHandler(BaseHTTPRequestHandler):
    server: DashboardHTTPServer

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query, keep_blank_values=True)
        try:
            if parsed.path == "/":
                self._html(render_home(self.server.store))
            elif parsed.path == "/news":
                self._html(render_news(self.server.store, params))
            elif parsed.path == "/sources":
                self._html(render_sources(self.server.store))
            elif parsed.path == "/article":
                self._article(params)
            elif parsed.path == "/media":
                self._file(params, "media")
            elif parsed.path == "/asset":
                self._file(params, "assets", attachment=True)
            elif parsed.path == "/api/summary":
                self._json(self.server.store.summary())
            elif parsed.path == "/api/news":
                self._api_news(params)
            elif parsed.path == "/api/sources":
                self._json({"sources": self.server.store.sources(500)})
            elif parsed.path == "/api/article":
                self._api_article(params)
            else:
                self._error(HTTPStatus.NOT_FOUND, "没有这个页面")
        except (SQLAlchemyError, OSError, ValueError) as error:
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, f"读取本地数据失败：{error}")

    def _article(self, params: dict[str, list[str]]) -> None:
        url = params.get("url", [""])[0]
        article = self.server.store.article(url)
        if article is None:
            self._error(HTTPStatus.NOT_FOUND, "找不到这篇文章")
        else:
            self._html(render_article(article))

    def _api_article(self, params: dict[str, list[str]]) -> None:
        url = params.get("url", [""])[0]
        article = self.server.store.article(url)
        if article is None:
            self._error(HTTPStatus.NOT_FOUND, "找不到这篇文章")
        else:
            self._json(article)

    def _api_news(self, params: dict[str, list[str]]) -> None:
        try:
            page = int(params.get("page", ["1"])[0])
        except ValueError:
            page = 1
        rows, total, page = self.server.store.news(
            params.get("q", [""])[0].strip(),
            params.get("source", [""])[0].strip(),
            page,
            30,
            params.get("type", [""])[0].strip(),
        )
        self._json({"items": rows, "total": total, "page": page})

    def _file(self, params: dict[str, list[str]], kind: str, attachment: bool = False) -> None:
        path = self.server.store.local_path(params.get("path", [""])[0], kind)
        if path is None:
            self._error(HTTPStatus.NOT_FOUND, "文件不存在或不在允许的目录中")
            return
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(path.stat().st_size))
        if attachment:
            self.send_header("Content-Disposition", f'attachment; filename="{path.name}"')
        self.end_headers()
        with path.open("rb") as handle:
            copyfileobj(handle, self.wfile)

    def _html(self, content: str, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = content.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, value: object, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(value, ensure_ascii=False, default=_json_default).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status: HTTPStatus, message: str) -> None:
        self._html(_layout("错误", f'<div class="notice">{_escape(message)}</div>'), status)

    def log_message(self, format: str, *args: object) -> None:
        return


class DashboardHTTPServer(ThreadingHTTPServer):
    def __init__(self, address: tuple[str, int], store: DashboardStore) -> None:
        super().__init__(address, DashboardHandler)
        self.store = store


def serve_dashboard(
    db_path: str | Path = "data/crawler.sqlite",
    data_dir: str | Path = "data",
    host: str = "127.0.0.1",
    port: int = 8765,
) -> int:
    store = DashboardStore(db_path, data_dir)
    server = DashboardHTTPServer((host, port), store)
    print(f"USTC crawl explorer: http://{host}:{port}/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping dashboard")
    finally:
        server.server_close()
        store.close()
    return 0
