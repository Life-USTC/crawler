import asyncio
import hashlib
import json
import sqlite3

import httpx
import pytest

from ustc_crawler import crawl
from ustc_crawler.cli import main
from ustc_crawler.config import load_config
from ustc_crawler.crawl_diagnostics import CrawlFailureDiagnostics
from ustc_crawler.http import Fetcher
from ustc_crawler.models import SourceConfig


@pytest.mark.parametrize("write_report", [False, True])
def test_crawl_reports_three_current_failures_and_keeps_healthy_progress(
    tmp_path, monkeypatch, capsys, write_report
):
    config = tmp_path / "sources.json"
    config.write_text(
        json.dumps(
            {
                "sources": [
                    {
                        "id": "news",
                        "name": "Test news",
                        "allowed_hosts": ["news.example.test"],
                        "seed_urls": [
                            f"https://news.example.test/{path}"
                            for path in ["unavailable", "blocked", "malformed", "healthy"]
                        ],
                    }
                ]
            }
        )
    )

    def response(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /blocked\n")
        if request.url.path == "/unavailable":
            return httpx.Response(503, text="raw-response-secret")
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            text="<html><title>Healthy page</title><body>Public content</body></html>",
        )

    extract = crawl.extract_page

    def parse(url, *args):
        if url.endswith("/malformed"):
            raise ValueError("raw-parser-payload-secret")
        return extract(url, *args)

    monkeypatch.setattr(crawl, "extract_page", parse)
    monkeypatch.setattr(
        crawl,
        "Fetcher",
        lambda **_: Fetcher(
            transport=httpx.MockTransport(response),
            delay=0,
            retries=0,
        ),
    )
    report = tmp_path / "diag" / "crawl-failures.json"
    database = tmp_path / "crawler.sqlite"
    code = main(
        [
            "crawl",
            "--config",
            str(config),
            "--db",
            str(database),
            "--data-dir",
            str(tmp_path / "data"),
            "--max-pages",
            "20",
            *(["--failure-report", str(report)] if write_report else []),
        ]
    )
    captured = capsys.readouterr()
    output = json.loads(captured.out)
    assert code == 2
    assert output["processed"] >= 4  # Includes automatic discovery feeds.
    assert output["errors"] == 3
    messages = [
        json.loads(line)
        for line in captured.err.splitlines()
        if line.startswith('{"event": "crawl.')
    ]
    summary = next(message for message in messages if message["event"] == "crawl.failure_summary")
    diagnostic = {
        **summary,
        "failures": [message for message in messages if message["event"] == "crawl.failure"],
    }
    if write_report:
        assert json.loads(report.read_text()) == diagnostic
    else:
        assert not report.exists()
    assert diagnostic["errors"] == diagnostic["reported"] == 3
    assert diagnostic["completed"] is True
    assert diagnostic["omitted"] == 0
    assert diagnostic["sources"] == [{"source_id": "news", "errors": 3}]
    failures = {entry["kind"]: entry for entry in diagnostic["failures"]}
    assert set(failures) == {"http_status", "robots_disallowed", "processing_exception"}
    assert failures["http_status"]["status"] == 503
    assert failures["robots_disallowed"]["status"] == 0
    assert failures["robots_disallowed"]["robots_reason"] == "rule_disallowed"
    assert failures["robots_disallowed"]["robots_status"] == 200
    assert "robots_reason" not in failures["http_status"]
    assert failures["processing_exception"]["exception_type"] == "ValueError"
    assert failures["processing_exception"]["location"]["function"] == "parse"
    assert all(entry["run_id"] == diagnostic["run_id"] for entry in failures.values())
    assert "raw-response-secret" not in json.dumps(diagnostic) + captured.err
    assert "raw-parser-payload-secret" not in json.dumps(diagnostic) + captured.err
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM failures").fetchone()[0] == 3
        assert (
            connection.execute(
                "SELECT status FROM frontier WHERE url='https://news.example.test/healthy'"
            ).fetchone()[0]
            == "done"
        )
        # Private archive evidence still retains the original exception.
        assert (
            connection.execute(
                "SELECT error FROM failures WHERE url='https://news.example.test/malformed'"
            ).fetchone()[0]
            == "ValueError: raw-parser-payload-secret"
        )


def test_diagnostics_are_bounded_and_omit_credentials_payloads_and_old_runs(tmp_path, capsys):
    report = tmp_path / "report.json"
    diagnostic = CrawlFailureDiagnostics("new-run", str(report))
    url = (
        "https://user:password@news.example.test/"
        + "x" * 700
        + "?token=query-secret#fragment-secret"
    )
    for _ in range(53):
        diagnostic.record(
            url, "news", kind="processing_exception", exception=ValueError("payload-secret")
        )
    diagnostic.summary()
    result = json.loads(report.read_text())
    captured = capsys.readouterr().err
    assert result["errors"] == 53
    assert result["reported"] == len(result["failures"]) == 50
    assert result["omitted"] == 3
    assert result["sources"] == [{"source_id": "news", "errors": 53}]
    assert result["failures"][0]["url_sha256"] == hashlib.sha256(url.encode()).hexdigest()
    assert all(len(entry["url"]) <= 512 for entry in result["failures"])
    for secret in ["password", "query-secret", "fragment-secret", "payload-secret"]:
        assert secret not in report.read_text() + captured
    assert len(captured.splitlines()) == 51  # 50 samples and one exact-count summary.
    CrawlFailureDiagnostics("next-run", str(report)).summary()
    fresh = json.loads(report.read_text())
    assert fresh["run_id"] == "next-run"
    assert fresh["errors"] == 0
    assert fresh["failures"] == fresh["sources"] == []


@pytest.mark.parametrize(
    ("scenario", "reason", "robots_status"),
    [
        ("rule", "rule_disallowed", 200),
        (401, "http_error", 401),
        (403, "http_error", 403),
        (410, "http_error", 410),
        (429, "http_error", 429),
        (503, "http_error", 503),
        ("transport", "transport_error", None),
        ("decoding", "decoding_error", None),
        ("unicode", "decoding_error", 200),
    ],
)
@pytest.mark.parametrize("expected", [False, True])
def test_robots_reason_reaches_report_without_fetching_page_or_leaking_details(
    tmp_path, monkeypatch, capsys, scenario, reason, robots_status, expected
):
    calls = []

    class InvalidTextResponse(httpx.Response):
        @property
        def text(self):
            raise UnicodeError("raw-text-secret")

    def response(request):
        calls.append(str(request.url))
        assert request.url.path == "/robots.txt", "A denied target page must not be requested"
        if scenario == "transport":
            raise httpx.ConnectError("raw-transport-secret", request=request)
        if scenario == "decoding":
            raise httpx.DecodingError("raw-decoder-secret", request=request)
        if scenario == "unicode":
            return InvalidTextResponse(200)
        return httpx.Response(
            scenario if isinstance(scenario, int) else 200,
            text="User-agent: *\nDisallow: /private/\n# raw-policy-secret",
            headers={"private-header": "raw-header-secret"},
        )

    monkeypatch.setattr(
        crawl,
        "Fetcher",
        lambda **_: Fetcher(transport=httpx.MockTransport(response), delay=0, retries=0),
    )
    report = tmp_path / "failures.json"
    target = "https://user:password@example.test/private/page?token=query-secret#fragment-secret"

    async def run():
        crawler = crawl.AsyncCrawler(
            crawl.CrawlOptions(
                db_path=str(tmp_path / "crawler.sqlite"),
                data_dir=str(tmp_path / "data"),
                failure_report=str(report),
            )
        )
        source = SourceConfig(
            id="news",
            name="Test source",
            organization_level="university",
            seed_urls=[target],
            allowed_hosts=["example.test"],
            expected_robots_disallow=expected,
        )
        crawler.sources[source.id] = source
        crawler.store.add_source(source)
        crawler.store.enqueue(target, source.id, 0, "", 500)
        try:
            await crawler._process(target, source.id, 0, "")
            crawler.failure_diagnostics.summary()
            assert crawler.errors == (0 if expected and scenario == "rule" else 1)
        finally:
            await crawler.close()

    asyncio.run(run())
    assert len(calls) == 1
    result = json.loads(report.read_text())
    if expected and scenario == "rule":
        assert result["errors"] == result["reported"] == 0
        assert result["failures"] == []
        assert result["expected_unavailable_pages"] == 1
        assert result["expected_unavailable_sources"] == [
            {"source_id": "news", "reason": "rule_disallowed", "pages": 1}
        ]
        with sqlite3.connect(tmp_path / "crawler.sqlite") as connection:
            assert connection.execute("SELECT COUNT(*) FROM failures").fetchone()[0] == 0
            assert connection.execute("SELECT status FROM frontier").fetchone()[0] == "done"
            assert connection.execute("SELECT blocked_by_robots FROM pages").fetchone()[0] == 1
        public_output = report.read_text() + capsys.readouterr().err
        for secret in ["password", "query-secret", "fragment-secret", "raw-policy-secret"]:
            assert secret not in public_output
        return
    assert result["errors"] == result["reported"] == 1
    detail = result["failures"][0]
    assert detail["kind"] == "robots_disallowed"
    assert detail["status"] == 0  # The target did not return the robots HTTP response.
    assert detail["robots_reason"] == reason
    assert detail["robots_status"] == robots_status
    assert detail["url"] == "https://example.test/private/page"
    public_output = report.read_text() + capsys.readouterr().err
    for secret in [
        "password",
        "query-secret",
        "fragment-secret",
        "raw-policy-secret",
        "raw-header-secret",
        "raw-transport-secret",
        "raw-decoder-secret",
        "raw-text-secret",
    ]:
        assert secret not in public_output


@pytest.mark.parametrize("denied", [False, True])
@pytest.mark.parametrize("page_status", [200, 404, 503])
def test_expected_source_keeps_policy_checks_and_real_http_failures(
    tmp_path, monkeypatch, capsys, denied, page_status
):
    config = tmp_path / "sources.json"
    config.write_text(
        json.dumps(
            {
                "sources": [
                    {
                        "id": "course",
                        "name": "Course",
                        "seed_urls": ["https://course.example.test/portal"],
                        "allowed_hosts": ["course.example.test"],
                        "expected_robots_disallow": True,
                    }
                ]
            }
        )
    )
    calls = []

    def response(request):
        calls.append(request.url.path)
        if request.url.path == "/robots.txt":
            return httpx.Response(
                200, text="User-agent: *\n" + ("Disallow: /\n" if denied else "Allow: /\n")
            )
        if request.url.path.endswith("sitemap.xml"):
            return httpx.Response(
                200, headers={"content-type": "application/xml"}, text="<urlset></urlset>"
            )
        return httpx.Response(
            page_status,
            headers={"content-type": "text/html"},
            text="<html><title>Course</title></html>",
        )

    monkeypatch.setattr(
        crawl,
        "Fetcher",
        lambda **_: Fetcher(transport=httpx.MockTransport(response), delay=0, retries=0),
    )
    report = tmp_path / "report.json"
    args = [
        "crawl",
        "--config",
        str(config),
        "--db",
        str(tmp_path / "crawler.sqlite"),
        "--data-dir",
        str(tmp_path / "data"),
        "--failure-report",
        str(report),
    ]
    code = main(args)
    result = json.loads(capsys.readouterr().out)
    diagnostic = json.loads(report.read_text())
    assert "/robots.txt" in calls
    if denied:
        assert code == result["errors"] == 0
        assert result["expected_unavailable_pages"] == diagnostic["expected_unavailable_pages"] == 3
        assert calls == ["/robots.txt"]
        # A later scheduled run must recheck the policy and resume this same archive.
        denied = False
        calls.clear()
        code = main([*args, "--incremental"])
        result = json.loads(capsys.readouterr().out)
        diagnostic = json.loads(report.read_text())
        assert "/robots.txt" in calls
    assert "/portal" in calls
    assert result["expected_unavailable_pages"] == 0
    assert result["errors"] == (0 if page_status == 200 else 1)
    assert code == (0 if page_status == 200 else 2)
    if page_status != 200:
        assert diagnostic["failures"][0]["status"] == page_status


def test_expected_robots_configuration_is_explicit(tmp_path):
    sources, _ = load_config("config/sources.yaml")
    assert [source.id for source in sources if source.expected_robots_disallow] == ["course"]
    config = tmp_path / "sources.json"
    config.write_text(
        json.dumps(
            {"sources": [{"id": "course", "name": "Course", "expected_robots_disallow": "false"}]}
        )
    )
    with pytest.raises(ValueError, match="expected_robots_disallow must be a boolean"):
        load_config(config)


def test_expected_unavailability_summary_is_bounded_and_separate_from_failures(tmp_path, capsys):
    report = tmp_path / "report.json"
    diagnostic = CrawlFailureDiagnostics("run", str(report))
    for index in range(53):
        diagnostic.record_expected_unavailable(f"source-{index}")
    diagnostic.summary()
    result = json.loads(report.read_text())
    assert result["errors"] == 0
    assert result["failures"] == []
    assert result["expected_unavailable_pages"] == 53
    assert len(result["expected_unavailable_sources"]) == 50
    assert result["omitted_expected_sources"] == 3
    assert len(capsys.readouterr().err.splitlines()) == 1
