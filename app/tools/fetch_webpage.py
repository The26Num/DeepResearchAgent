import json
import re
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from langchain_core.tools import tool


_USER_AGENT = "Mozilla/5.0 (compatible; DeepResearchAgent/0.1; research tool)"
DEFAULT_MAX_CHARS = 5000
MAX_ALLOWED_CHARS = 8000


@tool
def fetch_webpage(url: str, max_chars: int = DEFAULT_MAX_CHARS) -> str:
    """Read an important HTML source found by web_search to inspect its page title and body text before citing or summarizing it."""
    if urlparse(url).scheme not in {"http", "https"}:
        return json.dumps({"url": url, "error": "fetch failed: URL must use http or https"})
    if max_chars < 1:
        return json.dumps({"url": url, "error": "fetch failed: max_chars must be positive"})
    effective_max = min(max_chars, MAX_ALLOWED_CHARS)

    try:
        response = requests.get(url, headers={"User-Agent": _USER_AGENT}, timeout=15)
        response.raise_for_status()
    except requests.RequestException as exc:
        return json.dumps({"url": url, "error": f"fetch failed: {type(exc).__name__}: {exc}"}, ensure_ascii=False)

    final_url = response.url
    content_type = response.headers.get("Content-Type", "").lower()
    if "application/pdf" in content_type or final_url.lower().endswith(".pdf"):
        return json.dumps({"url": final_url, "error": "PDF content is not supported in Phase 1."}, ensure_ascii=False)
    if content_type and "html" not in content_type:
        return json.dumps({"url": final_url, "error": f"Unsupported content type: {content_type}"}, ensure_ascii=False)

    soup = BeautifulSoup(response.content, "html.parser")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    date_tag = soup.select_one('meta[property="article:published_time"], meta[name="citation_date"], meta[name="date"]')
    published_at = date_tag.get("content", "") if date_tag else ""
    for element in soup(["script", "style", "noscript", "nav", "footer", "header", "aside"]):
        element.decompose()
    for element in soup.select(".references, .ref-list, #references, .cookie-banner, #cookie-banner, [id*='cookie-consent']"):
        element.decompose()
    main_content = soup.find("main") or soup.find("article") or soup.body or soup
    text = re.sub(r"\s+", " ", main_content.get_text(" ", strip=True)).strip()
    return json.dumps(
        {"url": final_url, "title": title, "published_at": published_at, "text": text[:effective_max]},
        ensure_ascii=False,
    )
