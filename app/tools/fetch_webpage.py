import json
import re
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from langchain_core.tools import tool

from app.research.runtime import bounded_request_timeout
from app.research.discovery_guard import readable_source_urls


_USER_AGENT = "Mozilla/5.0 (compatible; DeepResearchAgent/0.1; research tool)"
DEFAULT_MAX_CHARS = 5000
MAX_ALLOWED_CHARS = 8000


@tool
def fetch_webpage(url: str, max_chars: int = DEFAULT_MAX_CHARS) -> str:
    """Read a source's HTML title and text. Known arXiv/PMLR/OpenReview PDFs use HTML/abstract alternatives; PDFs themselves are not parsed."""
    try:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return json.dumps({"url": url, "error": "fetch failed: URL must use http or https with a host"})
        _ = parsed.port  # Validate a malformed explicit port before any request.
    except ValueError:
        return json.dumps({"url": url, "error": "fetch failed: malformed URL"})
    if max_chars < 1:
        return json.dumps({"url": url, "error": "fetch failed: max_chars must be positive"})
    effective_max = min(max_chars, MAX_ALLOWED_CHARS)

    candidates = readable_source_urls(url)
    errors = []
    attempted_urls = []
    # At most two HTTP attempts per logical fetch, all under the task deadline.
    for candidate in candidates:
        if len(attempted_urls) >= 2:
            break
        attempted_urls.append(candidate)
        if urlparse(candidate).path.lower().endswith(".pdf"):
            errors.append("PDF parsing is not supported; use an HTML or abstract page")
            continue
        try:
            response = requests.get(candidate, headers={"User-Agent": _USER_AGENT}, timeout=bounded_request_timeout(15))
            response.raise_for_status()
        except requests.RequestException as exc:
            errors.append(f"fetch failed: {type(exc).__name__}: {exc}")
            continue

        final_url = response.url
        content_type = response.headers.get("Content-Type", "").lower()
        if "application/pdf" in content_type or urlparse(final_url).path.lower().endswith(".pdf"):
            errors.append("PDF parsing is not supported; use an HTML or abstract page")
            for alternative in readable_source_urls(final_url):
                if alternative not in candidates:
                    candidates.append(alternative)
            continue
        if content_type and "html" not in content_type:
            errors.append(f"Unsupported content type: {content_type}")
            continue

        soup = BeautifulSoup(response.content, "html.parser")
        title_tag = soup.select_one('meta[name="citation_title"]')
        title = title_tag.get("content", "") if title_tag else (soup.title.get_text(" ", strip=True) if soup.title else "")
        date_tag = soup.select_one('meta[property="article:published_time"], meta[name="citation_date"], meta[name="date"]')
        published_at = date_tag.get("content", "") if date_tag else ""
        for element in soup(["script", "style", "noscript", "nav", "footer", "header", "aside"]):
            element.decompose()
        for element in soup.select(".references, .ref-list, #references, .cookie-banner, #cookie-banner, [id*='cookie-consent']"):
            element.decompose()
        main_content = soup.find("main") or soup.find("article") or soup.body or soup
        text = re.sub(r"\s+", " ", main_content.get_text(" ", strip=True)).strip()
        if not text:
            errors.append("fetch failed: empty HTML body")
            continue
        return json.dumps({"url": final_url, "requested_url": url, "title": title,
                           "published_at": published_at, "text": text[:effective_max],
                           "attempted_urls": attempted_urls}, ensure_ascii=False)
    return json.dumps({"url": url, "error": "; ".join(errors)[:800] or "No readable HTML source",
                       "attempted_urls": attempted_urls}, ensure_ascii=False)
