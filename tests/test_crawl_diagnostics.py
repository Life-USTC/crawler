import hashlib
import json
import sqlite3

import httpx
import pytest

from ustc_crawler import crawl
from ustc_crawler.cli import main
from ustc_crawler.crawl_diagnostics import CrawlFailureDiagnostics
from ustc_crawler.http import Fetcher


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
