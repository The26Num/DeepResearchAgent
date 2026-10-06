import json
from urllib.parse import urldefrag

import requests
from langchain_core.tools import tool

from app.config import get_settings
from app.research.runtime import bounded_request_timeout

MAX_RESULTS = 8
MAX_SNIPPET_CHARS = 500
TAVILY_SEARCH_URL = "https://api.tavily.com/search"
SEARCH_TIMEOUT = (5, 30)


@tool
def web_search(query: str, max_results: int = 5) -> str:
    """Search the public web when a research task needs current external facts or source URLs; return titles, URLs, and snippets without summarizing them."""
    if not query.strip():
        return json.dumps({"error": "web search failed: query must not be empty"})
    if max_results < 1:
        return json.dumps({"error": "web search failed: max_results must be positive"})
    effective_max = min(max_results, MAX_RESULTS)
    api_key = (get_settings().tavily_api_key or "").strip()
    if not api_key or api_key == "replace-with-your-api-key":
        return json.dumps({"error": "web search failed: TAVILY_API_KEY is not configured"})

    try:
        response = requests.post(
            TAVILY_SEARCH_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "query": query,
                "max_results": effective_max,
                "search_depth": "basic",
                "auto_parameters": False,
                "include_answer": False,
                "include_raw_content": False,
                "include_images": False,
            },
            timeout=bounded_request_timeout(SEARCH_TIMEOUT),
        )
    except requests.Timeout:
        return json.dumps({"error": "web search failed: Tavily request timed out"})
    except requests.RequestException as exc:
        # Exception strings and provider bodies may contain credentials; do not echo them.
        return json.dumps({"error": f"web search failed: Tavily network error ({type(exc).__name__})"})

    if response.status_code != 200:
        reason = {
            400: "invalid search request",
            401: "authentication failed; check TAVILY_API_KEY",
            403: "access denied",
            422: "invalid search parameters",
            429: "rate limit exceeded",
            432: "API usage quota exceeded",
            433: "pay-as-you-go usage limit exceeded",
        }.get(response.status_code, "search service error")
        return json.dumps({"error": f"web search failed: Tavily HTTP {response.status_code}: {reason}"})

    try:
        payload = response.json()
    except ValueError:
        return json.dumps({"error": "web search failed: Tavily returned invalid JSON"})
    if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
        return json.dumps({"error": "web search failed: Tavily returned an invalid results format"})
    results = payload["results"]

    sources: list[dict[str, str]] = []
    seen_urls: set[str] = set()
    for result in results:
        if not isinstance(result, dict) or any(
            result.get(field) is not None and not isinstance(result.get(field), str)
            for field in ("url", "title", "content")
        ):
            return json.dumps({"error": "web search failed: Tavily returned an invalid search result"})
        url = (result.get("url") or "").strip()
        key = urldefrag(url).url.rstrip("/")
        if not key or key in seen_urls:
            continue
        seen_urls.add(key)
        sources.append({
            "title": result.get("title") or "",
            "url": url,
            "snippet": (result.get("content") or "")[:MAX_SNIPPET_CHARS],
        })
        if len(sources) >= effective_max:
            break

    output: dict[str, object] = {"results": sources}
    if not sources:
        output["message"] = "No search results found."
    return json.dumps(output, ensure_ascii=False)
