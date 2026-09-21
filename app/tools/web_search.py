import json
from urllib.parse import urldefrag

from ddgs import DDGS
from ddgs.exceptions import DDGSException
from langchain_core.tools import tool


MAX_RESULTS = 8
MAX_SNIPPET_CHARS = 500


@tool
def web_search(query: str, max_results: int = 5) -> str:
    """Search the public web when a research task needs current external facts or source URLs; return titles, URLs, and snippets without summarizing them."""
    if not query.strip():
        return json.dumps({"error": "web search failed: query must not be empty"})
    if max_results < 1:
        return json.dumps({"error": "web search failed: max_results must be positive"})
    effective_max = min(max_results, MAX_RESULTS)

    try:
        results = DDGS().text(query, max_results=effective_max, backend="duckduckgo")
    except DDGSException as exc:
        if str(exc) == "No results found.":
            results = []
        else:
            return json.dumps({"error": f"web search failed: {type(exc).__name__}: {exc}"}, ensure_ascii=False)
    except Exception as exc:
        return json.dumps({"error": f"web search failed: {type(exc).__name__}: {exc}"}, ensure_ascii=False)

    sources: list[dict[str, str]] = []
    seen_urls: set[str] = set()
    for result in results:
        url = (result.get("href") or result.get("url") or "").strip()
        key = urldefrag(url).url.rstrip("/")
        if not key or key in seen_urls:
            continue
        seen_urls.add(key)
        sources.append({
            "title": result.get("title", ""),
            "url": url,
            "snippet": (result.get("body") or result.get("snippet") or "")[:MAX_SNIPPET_CHARS],
        })
        if len(sources) >= effective_max:
            break

    response: dict[str, object] = {"results": sources}
    if not sources:
        response["message"] = "No search results found."
    return json.dumps(response, ensure_ascii=False)
