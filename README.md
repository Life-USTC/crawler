# USTC Public Site Crawler

面向中国科学技术大学公开站点的本地归档与检索工具。它遵守 `robots.txt`，不使用账号凭据、不绕过登录或访问限制，并将抓取结果保存在本地 SQLite 和内容寻址文件中。

## 能力

- 从学校主页、新闻网、职能部门和院系站点发现公开页面。
- 可断点续跑，并支持只刷新新闻列表和当前文章的增量更新。
- 解析标题、作者、发布时间、栏目、摘要、正文、图片和附件。
- 保存原始响应、结构化文章 bundle、媒体、附件和完整审计状态。
- 提供只读网页预览和 JSON API，区分新闻与通知。
- 对每个配置来源执行离线抽样，验证来源归属、原始文件、字段、bundle 和媒体。

## 项目结构

```text
config/                  来源配置
src/ustc_crawler/        抓取、解析、存储、路由和网页预览
scripts/                 质量报告、在线覆盖检查和离线抽样验证
tests/                   单元与集成测试
data/                    本地运行数据，不提交 Git
```

模块职责和数据流见 [ARCHITECTURE.md](ARCHITECTURE.md)。

## 快速开始

需要 Python 3.13 和 [uv](https://docs.astral.sh/uv/)。

```bash
uv sync --locked
uv run ustc-crawler discover-ustc

# 小规模试跑
uv run ustc-crawler crawl \
  --config config/sources.yaml \
  --units data/discovered_units.json \
  --include-units --include-supplemental \
  --max-pages 200 --max-pages-per-source 20
```

首次完整抓取：

```bash
uv run ustc-crawler crawl \
  --config config/sources.yaml \
  --units data/discovered_units.json \
  --include-units --include-supplemental \
  --max-pages 0 --max-depth 6
```

169 个来源的增量更新：

```bash
uv run ustc-crawler crawl \
  --config config/sources.yaml \
  --units data/discovered_units.json \
  --include-units --include-supplemental \
  --incremental --max-depth 6 \
  --concurrency 12 --delay 0.5
```

`--incremental` 只刷新 seed、新闻列表/feed 和需要修复的文章页，并跳过已归档的空壳、重复页、导航页和课程资源。请求按主机限速；中断后重新执行同一命令即可续跑。

## 检查与预览

```bash
# 数据统计与导出
uv run ustc-crawler stats
uv run ustc-crawler export
uv run ustc-crawler report

# 每来源离线抽样验证
uv run python scripts/validate_source_samples.py

# 只读本地预览
uv run ustc-crawler serve
```

预览默认地址为 <http://127.0.0.1:8765/>。主要 API：

- `/api/summary`
- `/api/news?type=news|notice`
- `/api/sources`
- `/api/article?url=...`

改进解析规则后可以仅使用本地原始 HTML 重建数据：

```bash
uv run ustc-crawler reindex
uv run ustc-crawler reindex --source unit-math-ustc-edu-cn
```

## 同步到服务端

同步客户端使用部署在爬虫机器上的服务密钥，不依赖任何个人账号或本地密钥环。服务器地址和服务密钥分别通过环境变量提供；密钥只在内存中用于发送 `X-Publication-Ingestion-Secret` 请求头，不会写入 SQLite、归档、命令参数、输出或日志：

```bash
export USTC_CRAWLER_SERVER=https://example.invalid
export USTC_CRAWLER_INGESTION_SECRET='set-this-in-the-machine-secret-store'

# 先把历史文章分块写入本地 outbox，不发起网络请求
uv run ustc-crawler sync-backfill \
  --db data/crawler.sqlite --data-dir data --chunk-size 100

# 重试并上传已持久化的不可变批次
uv run ustc-crawler sync \
  --db data/crawler.sqlite --data-dir data \
  --object-concurrency 8
```

批次默认最多 50 篇、单次请求上限 100 篇且正文约 2 MiB，断点重跑使用同一批次和幂等键；上传对象先从本地内容寻址 spool 校验 SHA-256/大小，再按服务端返回的请求头上传。未配置服务密钥时命令会以安全错误码退出。
同一批次内的对象上传和完成确认默认使用最多 8 个受限工作线程，并按计划顺序验证结果；可通过 `--object-concurrency` 或 `SyncOptions.object_concurrency` 调整到 1 至 32。

## 本地数据

`data/` 完全排除在 Git 之外。主要内容包括：

- `crawler.sqlite`：抓取状态、页面、文章、媒体和附件关系。
- `pages/`：按响应 SHA-256 保存的原始内容。
- `articles/`：按文章 URL 哈希保存的 JSON 和正文 HTML。
- `media/`、`assets/`：去重后的图片和公开附件。
- `exports/`：JSONL、来源报告和验证报告。
- `discovered_units.json`：从官方院系目录生成的来源候选。

仓库只包含代码和静态来源配置。数据库、网页内容、备份、日志及生成报告不会被提交。

## 开发

```bash
uv run ruff check src tests scripts
uv run pytest -q
```

GitHub Actions 使用锁定依赖执行相同检查。

## 访问边界

- 只跟随配置允许域名中的 HTTP(S) 链接。
- 遵守 `robots.txt`，不提交登录凭据。
- 登录门槛、403、验证码和不可访问页面只记录状态，不尝试绕过。
- PDF、Office 等公开文件作为附件归档，不冒充 HTML 文章。
