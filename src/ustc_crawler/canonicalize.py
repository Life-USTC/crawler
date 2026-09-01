from __future__ import annotations

import posixpath
import re
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

TRACKING_QUERY_KEYS = {
    "fbclid",
    "gclid",
    "mc_cid",
    "mc_eid",
    "ref",
    "ref_src",
}
ASSET_EXTENSIONS = {
    ".7z",
    ".avi",
    ".bmp",
    ".csv",
    ".doc",
    ".docx",
    ".gif",
    ".gz",
    ".jpeg",
    ".jpg",
    ".mp3",
    ".mp4",
    ".pdf",
    ".png",
    ".ppt",
    ".pptx",
    ".pps",
    ".ppsx",
    ".odt",
    ".ods",
    ".odp",
    ".rtf",
    ".rar",
    ".svg",
    ".tar",
    ".tgz",
    ".tif",
    ".tiff",
    ".webp",
    ".xls",
    ".xlsx",
    ".xml",
    ".zip",
    ".wps",
    ".et",
    ".dps",
    ".caj",
    ".epub",
    ".tex",
    ".txt",
    ".md",
    ".pages",
    ".numbers",
    ".key",
}

BINARY_SIGNATURES = (
    b"%PDF-",
    b"PK\x03\x04",
    b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1",
    b"Rar!\x1a\x07",
    b"7z\xbc\xaf\x27\x1c",
    b"\x89PNG\r\n\x1a\n",
    b"GIF87a",
    b"GIF89a",
    b"\xff\xd8\xff",
)


def normalize_url(raw_url: str, base_url: str | None = None) -> str:
    if not raw_url:
        return ""
    raw_value = raw_url.strip()
    # Legacy CMS templates occasionally leave a JavaScript placeholder or an
    # embedded tag in an href.  Treat those as absent instead of persisting a
    # URL which can never identify a public page (for example
    # ``${v_link('%27/``).
    if (
        "${" in raw_value
        or "<a" in raw_value.lower()
        or re.search(r"%27|%22", raw_value[:120], re.IGNORECASE)
    ):
        return ""
    try:
        value = urljoin(base_url or "", raw_value)
        parts = urlsplit(value)
        if parts.scheme.lower() not in {"http", "https"} or not parts.netloc:
            return ""
        hostname = (parts.hostname or "").lower().rstrip(".")
        if not hostname:
            return ""
        port = parts.port
    except (ValueError, UnicodeError):
        # Broken legacy links can contain non-numeric port text. Treat them
        # as undiscoverable rather than allowing one href to abort a worker.
        return ""
    netloc = hostname
    if port and not (
        (parts.scheme.lower() == "http" and port == 80)
        or (parts.scheme.lower() == "https" and port == 443)
    ):
        netloc = f"{hostname}:{port}"
    path = parts.path or "/"
    path = re.sub(r"/{2,}", "/", path)
    path = posixpath.normpath(path)
    if not path.startswith("/"):
        path = "/" + path
    if parts.path.endswith("/") and not path.endswith("/"):
        path += "/"
    query = []
    for key, val in parse_qsl(parts.query, keep_blank_values=True):
        lower_key = key.lower()
        if lower_key.startswith("utm_") or lower_key in TRACKING_QUERY_KEYS:
            continue
        query.append((key, val))
    query.sort()
    return urlunsplit((parts.scheme.lower(), netloc, path, urlencode(query), ""))


def host_matches(url: str, allowed_hosts: list[str] | set[str]) -> bool:
    try:
        host = (urlsplit(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return False
    for allowed in allowed_hosts:
        candidate = allowed.lower().rstrip(".")
        if host == candidate or host.endswith("." + candidate):
            return True
    return False


def looks_like_uploaded_html_attachment(url: str) -> bool:
    """Identify static HTML attachments emitted by the USTC CMS.

    The CMS stores downloadable HTML tutorials and similar files below this
    path.  They are assets, not CMS detail pages, even though their extension
    is HTML and their response may be labelled ``text/html``.
    """

    path = urlsplit(url).path.lower()
    return "/_upload/article/files/" in path and path.endswith((".htm", ".html"))


def looks_like_asset(url: str) -> bool:
    path = urlsplit(url).path.lower()
    return looks_like_uploaded_html_attachment(url) or any(
        path.endswith(ext) for ext in ASSET_EXTENSIONS
    )


def looks_like_binary(body: bytes) -> bool:
    """Identify common document/archive/image payloads before text decoding."""

    sample = body[:16].lstrip()
    return any(sample.startswith(signature) for signature in BINARY_SIGNATURES)


def same_origin(url_a: str, url_b: str) -> bool:
    a, b = urlsplit(url_a), urlsplit(url_b)
    return a.scheme.lower() == b.scheme.lower() and a.netloc.lower() == b.netloc.lower()
