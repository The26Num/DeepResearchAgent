"""Identity of a selected/read source, distinct from a search candidate or page body."""

import re
from typing import Literal
from urllib.parse import parse_qs, urlsplit, urlunsplit
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator


def normalize_source_url(url: str) -> str:
    """Normalize URL identity, including known arXiv representations of one paper.

    Preserve explicit versions, query parameters, scheme and general path case.
    Source retains the fetched URL; this function only supplies the identity key.
    """
    url = url.strip()
    if any(character.isspace() for character in url):
        raise ValueError("Source URL must not contain whitespace.")
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Source URL is malformed.") from exc
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"} or not host:
        raise ValueError("Source URL must be an absolute http or https URL.")
    host = host.lower()
    if ":" in host:  # Preserve brackets on IPv6 hosts.
        host = f"[{host}]"
    if port is not None and (scheme, port) not in {("http", 80), ("https", 443)}:
        host += f":{port}"
    if "@" in parsed.netloc:
        host = parsed.netloc.rsplit("@", 1)[0] + "@" + host
    path = parsed.path.rstrip("/")
    if host == "arxiv.org":
        match = re.fullmatch(r"/(?:abs|html|pdf)/((?:\d{4}\.\d{4,5}|[a-zA-Z.-]+/\d{7})(?:v\d+)?)(?:\.pdf)?", path)
        if match:
            path = "/abs/" + match.group(1)
    return urlunsplit((scheme, host, path, parsed.query, ""))


def classify_source_url(url: str) -> Literal["paper", "webpage", "repository"]:
    """Recognize paper landing pages; this does not imply a full-text reading."""
    parsed = urlsplit(url)
    host, path = (parsed.hostname or "").lower(), parsed.path.rstrip("/")
    if host in {"github.com", "gitlab.com"}:
        return "repository"
    if host in {"arxiv.org", "alphaxiv.org", "www.alphaxiv.org"} and re.fullmatch(
        r"/(?:abs|html|pdf)/((?:\d{4}\.\d{4,5}|[a-zA-Z.-]+/\d{7})(?:v\d+)?)(?:\.pdf)?", path
    ):
        return "paper"
    if host == "proceedings.mlr.press" and re.fullmatch(r"/v\d+/[^/]+(?:\.html|/[^/]+\.pdf)", path):
        return "paper"
    if host == "openreview.net" and path in {"/forum", "/pdf"} and parse_qs(parsed.query).get("id"):
        return "paper"
    if host in {"neurips.cc", "nips.cc", "iclr.cc", "icml.cc"} and re.fullmatch(
        r"/virtual/\d{4}/(?:loc/[^/]+/)?poster/\d+", path
    ):
        return "paper"
    if host == "link.springer.com" and path.startswith("/article/10."):
        return "paper"
    return "webpage"


class Source(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, frozen=True)

    source_id: str = Field(default_factory=lambda: f"src_{uuid4().hex}", min_length=1)
    url: str = Field(min_length=1)
    title: str = Field(min_length=1)
    source_type: Literal["paper", "webpage", "repository", "unknown"] = "unknown"
    task_id: str = Field(min_length=1)
    published_at: str | None = None

    @property
    def discovered_by_task(self) -> str:
        """task_id is the first discoverer, never the most recent consumer."""
        return self.task_id

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        normalize_source_url(value)
        return value  # Retain the original URL; the Store indexes its normalized key.
