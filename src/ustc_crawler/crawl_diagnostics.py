"""Bounded crawl diagnostics without response bodies, headers or exception text."""

from __future__ import annotations

import hashlib
import json
import sys
from collections import Counter
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

MAX_FAILURE_DETAILS = 50


class CrawlFailureDiagnostics:
    def __init__(self, run_id: str, report_path: str = "") -> None:
        self.run_id = run_id
        self.report_path = report_path
        self.sources: Counter[str] = Counter()
        self.details: list[dict] = []

    def record(
        self,
        url: str,
        source_id: str,
        *,
        kind: str,
        status: int | None = None,
        exception: Exception | None = None,
    ) -> None:
        self.sources[source_id] += 1
        if len(self.details) >= MAX_FAILURE_DETAILS:
            return
        try:
            parsed = urlsplit(url)
            # Queries/fragments and URL userinfo can contain credentials. The
            # digest correlates this safe locator with the private stored URL.
            locator = urlunsplit((parsed.scheme, parsed.hostname or "", parsed.path, "", ""))
        except ValueError:
            locator = "invalid-url"
        detail = {
            "event": "crawl.failure",
            "run_id": self.run_id,
            "source_id": source_id[:128],
            "url": locator[:512],
            "url_sha256": hashlib.sha256(url.encode("utf-8", errors="replace")).hexdigest(),
            "kind": kind,
            "status": status,
        }
        if exception is not None:
            detail["exception_type"] = type(exception).__name__[:64]
            trace = exception.__traceback__
            if trace is not None:
                while trace.tb_next is not None:
                    trace = trace.tb_next
                detail["location"] = {
                    "module": str(trace.tb_frame.f_globals.get("__name__", ""))[:128],
                    "function": trace.tb_frame.f_code.co_name[:128],
                    "line": trace.tb_lineno,
                }
        self.details.append(detail)
        print(json.dumps(detail, ensure_ascii=False), file=sys.stderr)

    def summary(self, *, completed: bool = True) -> None:
        total = self.sources.total()
        summary = {
            "event": "crawl.failure_summary",
            "run_id": self.run_id,
            "completed": completed,
            "errors": total,
            "reported": len(self.details),
            "omitted": total - len(self.details),
            "sources": [
                {"source_id": source[:128], "errors": count}
                for source, count in sorted(self.sources.items())[:MAX_FAILURE_DETAILS]
            ],
            "omitted_sources": max(0, len(self.sources) - MAX_FAILURE_DETAILS),
        }
        if total:
            print(json.dumps(summary, ensure_ascii=False), file=sys.stderr)
        if self.report_path:
            path = Path(self.report_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps({**summary, "failures": self.details}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
