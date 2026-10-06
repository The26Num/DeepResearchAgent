import importlib
import json
from types import SimpleNamespace

import pytest
import requests

from app.config import Settings


search_module = importlib.import_module("app.tools.web_search")


@pytest.fixture(autouse=True)
def search_settings(monkeypatch):
    monkeypatch.setattr(search_module, "get_settings", lambda: SimpleNamespace(tavily_api_key="test-key"))
    # Unit tests must never spend credits or use the key from the user's .env.
    def unexpected_request(*args, **kwargs):
        pytest.fail("Unexpected live search request")
    monkeypatch.setattr(search_module.requests, "post", unexpected_request)


def mock_response(monkeypatch, payload, status=200):
    def post(url, *, headers, json, timeout):
        assert url == "https://api.tavily.com/search"
        assert headers == {"Authorization": "Bearer test-key"}
        assert timeout == (5, 30)
        return SimpleNamespace(status_code=status, json=lambda: payload)
    monkeypatch.setattr(search_module.requests, "post", post)


def search(query="offline RL", **kwargs):
    return json.loads(search_module.web_search.invoke({"query": query, **kwargs}))


def test_search_result_conversion(monkeypatch) -> None:
    def post(url, *, headers, json, timeout):
        assert json == {
            "query": "offline RL", "max_results": 5, "search_depth": "basic",
            "auto_parameters": False, "include_answer": False,
            "include_raw_content": False, "include_images": False,
        }
        assert headers == {"Authorization": "Bearer test-key"}
        return SimpleNamespace(status_code=200, json=lambda: {
            "answer": "Do not pass generated answers to the Researcher",
            "results": [{"title": "Paper", "url": "https://example.org/paper", "content": "A useful snippet", "raw_content": "Full article"}],
        })
    monkeypatch.setattr(search_module.requests, "post", post)
    assert search() == {"results": [{"title": "Paper", "url": "https://example.org/paper", "snippet": "A useful snippet"}]}


def test_empty_search_results(monkeypatch) -> None:
    mock_response(monkeypatch, {"results": []})
    assert search() == {"results": [], "message": "No search results found."}


def test_duplicate_urls_are_removed(monkeypatch) -> None:
    mock_response(monkeypatch, {"results": [
        {"title": "Paper", "url": "https://example.org/paper", "content": "First"},
        {"title": "Paper copy", "url": "https://example.org/paper#abstract", "content": "Duplicate"},
        {"url": ""},
    ]})
    assert len(search()["results"]) == 1


def test_hard_cap_and_snippet_limit(monkeypatch) -> None:
    def post(url, *, headers, json, timeout):
        assert json["max_results"] == 8
        return SimpleNamespace(status_code=200, json=lambda: {"results": [
            {"title": f"Paper {i}", "url": f"https://example.org/{i}", "content": "x" * 900}
            for i in range(15)
        ]})
    monkeypatch.setattr(search_module.requests, "post", post)
    result = search("topic", max_results=50000)
    assert len(result["results"]) == 8
    assert all(len(item["snippet"]) == 500 for item in result["results"])


@pytest.mark.parametrize("key", [None, "", "  ", "replace-with-your-api-key"])
def test_missing_key_is_reported_without_network(monkeypatch, key) -> None:
    monkeypatch.setattr(search_module, "get_settings", lambda: SimpleNamespace(tavily_api_key=key))
    assert "TAVILY_API_KEY is not configured" in search()["error"]


@pytest.mark.parametrize("query,maximum", [("  ", 5), ("topic", 0), ("topic", -1)])
def test_invalid_arguments_do_not_call_provider(query, maximum) -> None:
    assert "error" in search(query, max_results=maximum)


@pytest.mark.parametrize("status,reason", [
    (400, "invalid search request"), (401, "authentication failed"),
    (403, "access denied"), (422, "invalid search parameters"),
    (429, "rate limit exceeded"), (432, "API usage quota exceeded"),
    (433, "usage limit exceeded"), (500, "search service error"),
])
def test_provider_failures_are_errors_not_empty_results(monkeypatch, status, reason) -> None:
    mock_response(monkeypatch, {"detail": {"error": "sensitive provider body test-key"}}, status)
    result = search()
    assert set(result) == {"error"}
    assert f"HTTP {status}" in result["error"]
    assert reason in result["error"]
    assert "test-key" not in result["error"]


@pytest.mark.parametrize("exception,reason", [
    (requests.Timeout, "timed out"), (requests.ConnectionError, "network error (ConnectionError)"),
])
def test_network_failures_are_reported_without_credentials(monkeypatch, exception, reason) -> None:
    def post(*args, **kwargs):
        raise exception("sensitive test-key")
    monkeypatch.setattr(search_module.requests, "post", post)
    result = search()
    assert reason in result["error"]
    assert "test-key" not in result["error"]


def test_invalid_json_is_reported(monkeypatch) -> None:
    def invalid_json():
        raise ValueError("malformed response test-key")
    monkeypatch.setattr(search_module.requests, "post", lambda *a, **kw: SimpleNamespace(status_code=200, json=invalid_json))
    assert "invalid JSON" in search()["error"]


@pytest.mark.parametrize("payload", [
    None, [], {}, {"results": None}, {"results": {}}, {"results": [None]},
    {"results": [{"url": ["bad"]}]}, {"results": [{"url": 0}]},
    {"results": [{"url": False}]}, {"results": [{"content": {}}]},
])
def test_invalid_results_are_reported(monkeypatch, payload) -> None:
    mock_response(monkeypatch, payload)
    assert "invalid" in search()["error"]


def test_tavily_key_loads_from_env_file(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text("TAVILY_API_KEY=test-file-key\n", encoding="utf-8")
    assert Settings(_env_file=env_file).tavily_api_key == "test-file-key"
