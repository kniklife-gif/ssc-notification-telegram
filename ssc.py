"""SSC (Staff Selection Commission) Notice Board source adapter.

Output contract (consumed by monitor.py): a list of dicts, one per notice:

    {"notice_id": str, "title": str, "date": str, "url": str}

Listing source (observed on the official server, see the marked section):
    GET https://ssc.gov.in/api/general-website/portal/records?... returns JSON
    {"statusCode": "200", "data": [{id, headline, createdAt, attachments:
    [{path, fileName, ...}]}], "paginate": {totalRecords, totalPage, ...}},
    newest first. A document URL is
    https://ssc.gov.in/api/attachment/ + attachment "path" (backslashes
    converted to "/").

Any HTTP error, non-200 statusCode, malformed JSON or empty "data" raises a
SourceError; the monitor treats that as a source failure and keeps its state.
"""

import hashlib
import http.client
import json
import socket
import ssl
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterable, List, NamedTuple, Optional
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

SSC_BASE_URL = "https://ssc.gov.in/"
SSC_HOST = "ssc.gov.in"

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0"
)
DEFAULT_ACCEPT = "application/json, text/html;q=0.9, */*;q=0.8"
MAX_BODY_BYTES = 10 * 1024 * 1024
RETRYABLE_4XX = (408, 429)

UNTITLED = "(untitled notice)"
_IST = timezone(timedelta(hours=5, minutes=30))


class SourceError(Exception):
    """Any failure to obtain trustworthy SSC notice data."""


class SourceFetchError(SourceError):
    """Network / HTTP level failure."""


class SourceParseError(SourceError):
    """Response was received but did not contain usable notice records."""


class SourceUnverifiedError(SourceError):
    """The live SSC listing request/response contract is not yet verified."""


class HttpResponse(NamedTuple):
    status: int
    final_url: str
    text: str


# --------------------------------------------------------------------------
# HTTP client (standard library only, TLS verification always on)
# --------------------------------------------------------------------------

def _is_ssc_host(hostname: Optional[str]) -> bool:
    host = (hostname or "").lower()
    return host == SSC_HOST or host.endswith("." + SSC_HOST)


def _decode(raw: bytes, charset: str) -> str:
    try:
        text = raw.decode(charset)
    except (UnicodeDecodeError, LookupError):
        text = raw.decode("utf-8", errors="replace")
    return text.lstrip("﻿")


def http_get(
    url: str,
    accept: str = DEFAULT_ACCEPT,
    *,
    timeout: float = 30.0,
    attempts: int = 4,
    backoff: float = 2.0,
    max_bytes: int = MAX_BODY_BYTES,
    sleep=time.sleep,
) -> HttpResponse:
    """GET an official SSC URL.

    Retries timeouts, connection errors, 5xx, 408 and 429 with exponential
    backoff. Does not retry 404 or other 4xx, or TLS errors. Redirects are
    followed but must stay on ssc.gov.in. Empty or oversized bodies fail.
    """
    parts = urlsplit(url)
    if parts.scheme != "https" or not _is_ssc_host(parts.hostname):
        raise SourceFetchError(f"refusing non-official or non-HTTPS URL: {url}")

    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": accept,
            "Accept-Language": "en-IN,en;q=0.9",
            "Accept-Encoding": "identity",
        },
    )

    last_error = "unknown error"
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                status = response.status
                final_url = response.geturl()
                raw = response.read(max_bytes + 1)
                charset = response.headers.get_content_charset() or "utf-8"
        except urllib.error.HTTPError as exc:
            code = exc.code
            exc.close()
            if code == 404 or (400 <= code < 500 and code not in RETRYABLE_4XX):
                raise SourceFetchError(f"HTTP {code} from {url} (not retried)") from None
            last_error = f"HTTP {code}"
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, ssl.SSLError):
                raise SourceFetchError(f"TLS error for {url}: {exc.reason}") from None
            last_error = f"network error: {exc.reason}"
        except (socket.timeout, OSError, http.client.HTTPException) as exc:
            last_error = f"network error: {type(exc).__name__}"
        else:
            if not 200 <= status < 300:
                raise SourceFetchError(f"unexpected HTTP status {status} from {url}")
            if not _is_ssc_host(urlsplit(final_url).hostname):
                raise SourceFetchError(f"redirected away from ssc.gov.in: {final_url}")
            if len(raw) > max_bytes:
                raise SourceFetchError(f"response from {url} exceeds {max_bytes} bytes")
            text = _decode(raw, charset)
            if not text.strip():
                raise SourceFetchError(f"empty response body from {url}")
            return HttpResponse(status, final_url, text)

        if attempt < attempts:
            sleep(backoff ** attempt)

    raise SourceFetchError(f"GET {url} failed after {attempts} attempts: {last_error}")


# --------------------------------------------------------------------------
# Normalisation, stable IDs, de-duplication
# --------------------------------------------------------------------------

def _clean(value) -> str:
    if value is None:
        return ""
    return " ".join(str(value).split())


def normalize_url(raw_url) -> str:
    """Return the complete official https URL, or raise ValueError.

    Relative URLs resolve against https://ssc.gov.in/. Only ssc.gov.in (and
    subdomains) are accepted; the path and query are preserved in full.
    """
    value = str(raw_url).strip()
    if not value:
        raise ValueError("empty URL")
    parts = urlsplit(urljoin(SSC_BASE_URL, value))
    if parts.scheme not in ("http", "https"):
        raise ValueError(f"unsupported URL scheme: {parts.scheme!r}")
    if parts.username is not None or parts.password is not None:
        raise ValueError("URL must not contain credentials")
    if not _is_ssc_host(parts.hostname):
        raise ValueError(f"not an official SSC host: {parts.hostname!r}")
    path = quote(parts.path, safe="/%:@!$&'()*+,;=-._~")
    query = quote(parts.query, safe="=&%:@!$'()*+,;/?-._~")
    return urlunsplit(("https", parts.netloc.lower(), path, query, ""))


def make_notice_id(
    official_id: str, document_id: str, url: str, title: str, date: str
) -> str:
    """Deterministic ID. Priority: official record ID, official document ID,
    official document URL, then sha256(title + date + url)."""
    if official_id:
        return official_id
    if document_id:
        return document_id
    if url:
        return url
    payload = "\n".join((title, date, url)).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def normalize_record(raw: Dict) -> Optional[Dict[str, str]]:
    """Turn one raw record into the normalised form.

    Raw keys: title, date, url (all optional) plus optional official_id and
    document_id. Returns None if the record carries no identifying content;
    raises ValueError if its URL is not an official SSC URL.
    """
    title = _clean(raw.get("title"))
    date = _clean(raw.get("date"))
    official_id = _clean(raw.get("official_id"))
    document_id = _clean(raw.get("document_id"))
    raw_url = raw.get("url")
    url = normalize_url(raw_url) if raw_url is not None and str(raw_url).strip() else ""

    if not (title or url or official_id or document_id):
        return None

    return {
        "notice_id": make_notice_id(official_id, document_id, url, title, date),
        "title": title or UNTITLED,
        "date": date,
        "url": url,
    }


def normalize_records(raw_records: Iterable) -> List[Dict[str, str]]:
    """Normalise and de-duplicate by notice_id (first occurrence wins).

    Raises SourceParseError when nothing usable remains: an empty or
    unrecognised result is a source failure, never "no notifications".
    """
    seen = set()
    records: List[Dict[str, str]] = []
    skipped = 0
    for raw in raw_records:
        if not isinstance(raw, dict):
            skipped += 1
            continue
        try:
            record = normalize_record(raw)
        except ValueError:
            skipped += 1
            continue
        if record is None:
            skipped += 1
            continue
        if record["notice_id"] in seen:
            continue
        seen.add(record["notice_id"])
        records.append(record)

    if skipped:
        print(f"WARNING: skipped {skipped} unusable SSC record(s)", file=sys.stderr)
    if not records:
        raise SourceParseError("SSC response contained no usable notice records")
    return records


# ==========================================================================
# SSC Notice Board listing.
#
# Endpoint: the request below was taken from a public third-party project's
# config (mukund-buddy/govt-job-monitor, config/sources.json) and then checked
# against the official server on 2026-10-09: with these parameters
# ssc.gov.in answered statusCode "200", 709 records, newest first, and one
# attachment per record (limit=5 and limit=20 both worked). Without the
# "attributes" parameter the server answers statusCode "203" "Invalid
# attributes in request". Only the parameters listed here have been observed
# to work. Newest-first ordering means page 1 holds the latest notices; older
# pages (paginate.nextPage) are not needed to detect new ones.
#
# Response fields used (observed): data[].id, data[].headline,
# data[].createdAt (UTC ISO timestamp, converted to an IST date),
# data[].attachments[].path.
# ==========================================================================

LISTING_URL = (
    SSC_BASE_URL + "api/general-website/portal/records"
    "?page=1&limit=20&contentType=notice-boards&key=createdAt&order=DESC"
    "&pageType=filter&isAttachment=true"
    "&attributes=id,headline,startDate,createdAt&language=english"
)
ATTACHMENT_BASE = SSC_BASE_URL + "api/attachment/"


def _ist_date(created) -> str:
    """createdAt (observed as a UTC ISO timestamp ending in 'Z') as an Indian
    date, DD.MM.YYYY, the format SSC prints on its notices. Returns '' when
    the value is missing or unparseable."""
    if not isinstance(created, str) or not created.strip():
        return ""
    text = created.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return ""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(_IST).strftime("%d.%m.%Y")


def _attachment_url(attachments) -> str:
    """Official document URL of the first usable attachment, or ''."""
    if not isinstance(attachments, list):
        return ""
    for attachment in attachments:
        path = attachment.get("path") if isinstance(attachment, dict) else None
        if isinstance(path, str) and path.strip():
            relative = path.strip().replace("\\", "/").lstrip("/")
            return urljoin(ATTACHMENT_BASE, relative)
    return ""


def _extract_records(body: str) -> List[Dict]:
    try:
        payload = json.loads(body)
    except ValueError:
        raise SourceParseError("SSC listing response is not valid JSON") from None
    if not isinstance(payload, dict):
        raise SourceParseError("SSC listing response is not a JSON object")
    if str(payload.get("statusCode")) != "200":
        raise SourceParseError(
            "SSC listing returned statusCode "
            f"{_clean(payload.get('statusCode'))!r}: "
            f"{_clean(payload.get('error') or payload.get('statusMessage'))[:120]}"
        )
    data = payload.get("data")
    if not isinstance(data, list) or not data:
        raise SourceParseError("SSC listing contained no records")

    raw_records = []
    for item in data:
        if not isinstance(item, dict):
            continue
        raw_records.append(
            {
                "official_id": item.get("id"),
                "title": item.get("headline"),
                "date": _ist_date(item.get("createdAt")),
                "url": _attachment_url(item.get("attachments")),
            }
        )
    return raw_records


def _load_raw_records() -> List[Dict]:
    response = http_get(LISTING_URL, accept="application/json")
    return _extract_records(response.text)


# ==========================================================================

def fetch_notices() -> List[Dict[str, str]]:
    """Fetch, normalise and de-duplicate current SSC notices."""
    return normalize_records(_load_raw_records())
