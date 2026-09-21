import importlib
import json

import requests


fetch_module = importlib.import_module("app.tools.fetch_webpage")


def test_html_title_and_main_text_are_extracted(monkeypatch) -> None:
    class Response:
        url = "https://arxiv.org/abs/example"
        headers = {"Content-Type": "text/html; charset=utf-8"}
        content = b"<html><head><title>Paper title</title><meta name='citation_date' content='2026-04-01'></head><body><header>Site menu</header><main><blockquote class='abstract'>Abstract: Useful finding.</blockquote><script>bad code</script><style>bad style</style><p>Another   finding.</p></main><footer>Footer</footer></body></html>"

        def raise_for_status(self) -> None:
            pass

    def fake_get(url: str, headers: dict[str, str], timeout: int) -> Response:
        assert timeout == 15
        assert "User-Agent" in headers
        return Response()

    monkeypatch.setattr(fetch_module.requests, "get", fake_get)
    result = json.loads(fetch_module.fetch_webpage.invoke({"url": "https://arxiv.org/abs/example"}))
    assert result["title"] == "Paper title"
    assert result["published_at"] == "2026-04-01"
    assert "Abstract: Useful finding." in result["text"]
    assert "Another finding." in result["text"]
    assert "bad code" not in result["text"]
    assert "bad style" not in result["text"]
    assert "Site menu" not in result["text"]


def test_http_error_is_clear(monkeypatch) -> None:
    def fake_get(url: str, headers: dict[str, str], timeout: int) -> None:
        raise requests.HTTPError("503 Service Unavailable")

    monkeypatch.setattr(fetch_module.requests, "get", fake_get)
    result = json.loads(fetch_module.fetch_webpage.invoke({"url": "https://example.org/fail"}))
    assert "503 Service Unavailable" in result["error"]


def test_max_chars_limits_extracted_text(monkeypatch) -> None:
    class Response:
        url = "https://example.org/page"
        headers = {"Content-Type": "text/html"}
        content = b"<html><head><title>Title</title></head><body><main>Long paragraph here</main></body></html>"

        def raise_for_status(self) -> None:
            pass

    monkeypatch.setattr(fetch_module.requests, "get", lambda *args, **kwargs: Response())
    result = json.loads(fetch_module.fetch_webpage.invoke({"url": "https://example.org/page", "max_chars": 4}))
    assert result["text"] == "Long"


def test_default_and_hard_cap_clean_long_html(monkeypatch) -> None:
    class Response:
        url = "https://example.org/page"
        headers = {"Content-Type": "text/html"}
        content = ("<html><head><title>Paper</title></head><body><main>"
                   + "A" * 12000
                   + "<aside>SIDEBAR</aside><section class='references'>REFERENCES</section>"
                   + "<div class='cookie-banner'>COOKIES</div></main></body></html>").encode()

        def raise_for_status(self) -> None:
            pass

    monkeypatch.setattr(fetch_module.requests, "get", lambda *args, **kwargs: Response())
    default = json.loads(fetch_module.fetch_webpage.invoke({"url": "https://example.org/page"}))
    oversized = json.loads(fetch_module.fetch_webpage.invoke({"url": "https://example.org/page", "max_chars": 50000}))
    assert len(default["text"]) == 5000
    assert len(oversized["text"]) == 8000
    assert "SIDEBAR" not in oversized["text"]
    assert "REFERENCES" not in oversized["text"]
    assert "COOKIES" not in oversized["text"]
