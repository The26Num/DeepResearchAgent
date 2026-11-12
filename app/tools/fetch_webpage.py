"""Bounded HTTP acquisition -> immutable archive -> a bounded original-text preview."""

import json
import re
from urllib.parse import urlparse, parse_qs
from uuid import uuid4

import requests
from bs4 import BeautifulSoup
from langchain_core.tools import tool

from app.config import get_settings
from app.research.runtime import bounded_request_timeout
from app.research.discovery_guard import readable_source_urls
from app.research.page_quality import page_body_error
from app.research.source_archive import SourceArchive, ArchiveLimitError, ArchiveIntegrityError
from app.research.task_resources import active_resources, ResearchBudgetExceeded

_USER_AGENT = "Mozilla/5.0 (compatible; DeepResearchAgent/0.1; research tool)"
DEFAULT_MAX_CHARS = 7500
MAX_ALLOWED_CHARS = 12000


def _body(response, limit):
    declared = response.headers.get("Content-Length", "")
    if str(declared).isdigit() and int(declared) > limit:
        raise ArchiveLimitError("Response exceeds download limit; no truncated body is archived as complete.")
    parts, size = [], 0
    chunks = response.iter_content(chunk_size=65536) if callable(getattr(response, "iter_content", None)) else [getattr(response, "content", b"")]
    for chunk in chunks:
        if not chunk:
            continue
        size += len(chunk)
        if size > limit:
            raise ArchiveLimitError("Response exceeds download limit; no truncated body is archived as complete.")
        parts.append(chunk)
    return b"".join(parts)


def _parse(raw):
    soup = BeautifulSoup(raw, "html.parser")
    title_tag = soup.select_one('meta[name="citation_title"]')
    title = title_tag.get("content", "") if title_tag else (soup.title.get_text(" ", strip=True) if soup.title else "")
    date_tag = soup.select_one('meta[property="article:published_time"], meta[name="citation_date"], meta[name="date"]')
    published_at = date_tag.get("content", "") if date_tag else ""
    for element in soup(["script", "style", "noscript", "nav", "footer", "header", "aside"]):
        element.decompose()
    for element in soup.select(".cookie-banner, #cookie-banner, [id*='cookie-consent']"):
        element.decompose()
    main = soup.find("main") or soup.find("article") or soup.body or soup
    # Stable paragraph separators; offsets are Unicode characters, not UTF-8 bytes.
    text = "\n".join(" ".join(line.split()) for line in main.get_text("\n", strip=True).splitlines() if line.strip())
    return title, published_at, text, soup.original_encoding or "utf-8"


def _preview(archive, ref, maximum, resources, reused):
    body, metadata, sections = archive.read(ref)
    if ref.status != "ready":
        return {"url": ref.url, "snapshot_id": ref.snapshot_id, "status": ref.status,
                "error": metadata["detail"] or "Source is not readable."}
    count = min(maximum, resources.record.limits["preview_chars"]) if resources else maximum
    if resources:
        count = resources.reserve_chars(count)
    start = 0
    if resources:
        query = " ".join((resources.task.question, resources.task.title))
        terms = sorted(set(re.findall(r"[a-zA-Z0-9_]{5,}|[\u4e00-\u9fff]{2,8}", query)), key=len, reverse=True)
        for term in terms[:16]:
            match = re.search(re.escape(term), body, re.I)
            if match:
                start = max(0, match.start() - count // 4)
                break
    end = min(len(body), start + count)
    text = body[start:end]
    row = resources.record_span(ref, start, end, text, "fetch_preview") if resources and text else None
    return {"url": ref.url, "requested_url": ref.requested_url, "title": ref.title,
            "published_at": metadata["published_at"], "snapshot_id": ref.snapshot_id, "status": "ready",
            "representation": metadata["representation"], "text": text, "start_char": start, "end_char": end,
            "body_chars": len(body), "body_hash": ref.body_hash, "reused": reused,
            "offset_unit": "python_unicode_characters", "preview_only": end - start < len(body),
            "next_start_char": end if end < len(body) else None,
            "reading_sequence": row.sequence if row else None, "section_count": len(sections),
            "instruction": "Only this preview was read. Search saved sources and read_source for other ranges."}


@tool
def fetch_webpage(url: str, max_chars: int = DEFAULT_MAX_CHARS, refresh: bool = False) -> str:
    """Archive successfully retrieved HTML and return a bounded preview plus snapshot_id. Use read_source for more text; refresh creates a new immutable version when content changes."""
    resources = active_resources()
    cfg = resources.run.settings if resources else get_settings()
    archive = resources.run.archive if resources else SourceArchive(cfg.source_archive_path, "standalone_" + uuid4().hex,
        max_bytes=cfg.max_run_archive_bytes, max_snapshots=cfg.max_run_archive_snapshots)
    try:
        parsed = urlparse(url)
        if (len(url) > 2000 or parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password
                or set(key.lower() for key in parse_qs(parsed.query)) & {"api_key", "apikey", "token", "password", "authorization"}):
            raise ValueError("URL must use http/https without embedded authentication.")
        _ = parsed.port
        if max_chars < 1:
            raise ValueError("max_chars must be positive.")
        maximum = min(max_chars, MAX_ALLOWED_CHARS)
        if resources and not refresh and (known := resources.cached(url)):
            result = _preview(archive, known, maximum, resources, True)
            return json.dumps(result, ensure_ascii=False)
    except (ValueError, ArchiveIntegrityError, ResearchBudgetExceeded) as error:
        return json.dumps({"url": url, "status": "invalid_request", "error": str(error)})

    candidates, errors, attempted_urls = readable_source_urls(url), [], []
    last_status = "network_error"
    for candidate in candidates:
        if len(attempted_urls) >= 2:
            break
        attempted_urls.append(candidate)
        if urlparse(candidate).path.lower().endswith(".pdf"):
            errors.append("PDF parsing is not supported; use an HTML or abstract page")
            last_status = "unsupported"
            continue
        response = None
        try:
            limit = resources.reserve_download() if resources else cfg.max_source_response_bytes
            response = requests.get(candidate, headers={"User-Agent": _USER_AGENT},
                                    timeout=bounded_request_timeout(15), stream=True, allow_redirects=False)
            response.raise_for_status()
            code = getattr(response, "status_code", 200)
            if isinstance(code, int) and 300 <= code < 400:
                from urllib.parse import urljoin
                target = urljoin(candidate, response.headers.get("Location", ""))
                parsed_target = urlparse(target)
                if (len(target) > 2000 or parsed_target.scheme not in {"http", "https"} or not parsed_target.hostname
                        or parsed_target.username or parsed_target.password
                        or set(key.lower() for key in parse_qs(parsed_target.query)) & {"api_key", "apikey", "token", "password", "authorization"}):
                    raise ArchiveIntegrityError("Redirect target is not an authorized HTTP(S) source URL.")
                if target not in candidates:
                    candidates.append(target)
                errors.append("Redirect encountered; following only within the two-request fetch limit.")
                last_status = "redirect_limit"
                continue
            raw = _body(response, limit)
            if resources:
                resources.check_open()
                with resources.lock:
                    resources.record.downloaded_bytes += len(raw)
            final_url = response.url
            content_type = response.headers.get("Content-Type", "").lower()
            status, detail, title, published, text, encoding = "ready", "", "", None, "", "utf-8"
            if "application/pdf" in content_type or urlparse(final_url).path.lower().endswith(".pdf"):
                status, detail = "unsupported", "PDF parsing is not supported; use an HTML or abstract page"
                for alternative in readable_source_urls(final_url):
                    if alternative not in candidates:
                        candidates.append(alternative)
            elif content_type and "html" not in content_type:
                status, detail = "unsupported", "Unsupported content type: " + content_type[:100]
            else:
                try:
                    title, published, text, encoding = _parse(raw)
                    if issue := page_body_error(text):
                        status, detail = "parse_failed", issue
                except Exception:
                    status, detail, text = "parse_failed", "HTML body extraction failed.", ""
            representation = "abstract" if "/abs/" in final_url or "abstract" in urlparse(final_url).path else "html_page"
            ref = archive.put(requested_url=url, url=final_url, title=title, raw=raw, text=text,
                              task_id=resources.task.id if resources else "standalone", content_type=content_type,
                              encoding=encoding, status=status, detail=detail, published_at=published,
                              representation=representation)
            if resources:
                resources.remember(ref)
            if status == "ready":
                result = _preview(archive, ref, maximum, resources, False)
                result["attempted_urls"] = attempted_urls
                return json.dumps(result, ensure_ascii=False)
            last_status = status
            errors.append(detail)
        except requests.RequestException as error:
            errors.append(f"fetch failed: {type(error).__name__}: {error}")
            last_status = "network_error"
        except ArchiveLimitError as error:
            errors.append(str(error))
            last_status = "download_or_storage_limit"
            break
        except (ArchiveIntegrityError, ResearchBudgetExceeded) as error:
            errors.append(str(error))
            last_status = "resource_error"
            break
        finally:
            if response is not None and callable(getattr(response, "close", None)):
                response.close()
    return json.dumps({"url": url, "status": last_status, "error": "; ".join(errors)[:800] or "No readable HTML source",
                       "attempted_urls": attempted_urls}, ensure_ascii=False)
