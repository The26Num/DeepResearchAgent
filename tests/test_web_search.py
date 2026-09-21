import importlib
import json


search_module = importlib.import_module("app.tools.web_search")


def test_search_result_conversion(monkeypatch) -> None:
    class FakeDDGS:
        def text(self, query: str, max_results: int, backend: str) -> list[dict[str, str]]:
            assert query == "offline RL"
            assert max_results == 5
            assert backend == "duckduckgo"
            return [{"title": "Paper", "href": "https://example.org/paper", "body": "A useful snippet"}]

    monkeypatch.setattr(search_module, "DDGS", FakeDDGS)
    results = json.loads(search_module.web_search.invoke({"query": "offline RL"}))
    assert results == {"results": [{"title": "Paper", "url": "https://example.org/paper", "snippet": "A useful snippet"}]}


def test_empty_search_results(monkeypatch) -> None:
    class FakeDDGS:
        def text(self, query: str, max_results: int, backend: str) -> list[dict[str, str]]:
            return []

    monkeypatch.setattr(search_module, "DDGS", FakeDDGS)
    assert json.loads(search_module.web_search.invoke({"query": "unusual query"})) == {
        "results": [], "message": "No search results found."
    }


def test_search_exception_is_reported(monkeypatch) -> None:
    class FakeDDGS:
        def text(self, query: str, max_results: int, backend: str) -> list[dict[str, str]]:
            raise RuntimeError("service unavailable")

    monkeypatch.setattr(search_module, "DDGS", FakeDDGS)
    result = json.loads(search_module.web_search.invoke({"query": "offline RL"}))
    assert "service unavailable" in result["error"]


def test_duplicate_urls_are_removed(monkeypatch) -> None:
    class FakeDDGS:
        def text(self, query: str, max_results: int, backend: str) -> list[dict[str, str]]:
            return [
                {"title": "Paper", "href": "https://example.org/paper", "body": "First"},
                {"title": "Paper copy", "href": "https://example.org/paper#abstract", "body": "Duplicate"},
            ]

    monkeypatch.setattr(search_module, "DDGS", FakeDDGS)
    result = json.loads(search_module.web_search.invoke({"query": "offline RL"}))
    assert len(result["results"]) == 1


def test_hard_cap_and_snippet_limit(monkeypatch) -> None:
    class FakeDDGS:
        def text(self, query: str, max_results: int, backend: str) -> list[dict[str, str]]:
            assert max_results == 8
            return [
                {"title": f"Paper {i}", "href": f"https://example.org/{i}", "body": "x" * 900}
                for i in range(15)
            ]

    monkeypatch.setattr(search_module, "DDGS", FakeDDGS)
    result = json.loads(search_module.web_search.invoke({"query": "topic", "max_results": 50000}))
    assert len(result["results"]) == 8
    assert all(len(item["snippet"]) == 500 for item in result["results"])
