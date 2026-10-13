"""A polite HTTP client for public job-board APIs and careers pages.

It identifies itself honestly, waits between requests to the same host, and never tries to look
like something it is not (rule 3: no fingerprint spoofing, no IP rotation).
"""

from __future__ import annotations

import logging
import time
from typing import Any
from urllib.parse import urlparse

import httpx

USER_AGENT = "nyc-startup-applier/0.1 (personal job-search tool; reads public job boards)"
log = logging.getLogger(__name__)


class FetchError(Exception):
    pass


class PoliteClient:
    def __init__(self, min_interval: float = 0.6, timeout: float = 20.0, client: httpx.Client | None = None):
        self._client = client or httpx.Client(
            headers={"User-Agent": USER_AGENT}, timeout=timeout, follow_redirects=True
        )
        self._min_interval = min_interval
        self._last: dict[str, float] = {}

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "PoliteClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _wait(self, url: str) -> None:
        host = urlparse(url).netloc
        elapsed = time.monotonic() - self._last.get(host, 0.0)
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)
        self._last[host] = time.monotonic()

    def get(self, url: str, params: dict | None = None, retries: int = 2) -> httpx.Response | None:
        """GET a URL. Returns None for 404/410; raises FetchError after repeated failures."""
        for attempt in range(retries + 1):
            self._wait(url)
            try:
                response = self._client.get(url, params=params)
            except httpx.HTTPError as exc:
                if attempt == retries:
                    raise FetchError(f"{url}: {exc}") from exc
                time.sleep(2 * (attempt + 1))
                continue
            if response.status_code in (404, 410):
                return None
            if response.status_code in (429, 500, 502, 503, 504) and attempt < retries:
                time.sleep(3 * (attempt + 1))
                continue
            if response.status_code >= 400:
                raise FetchError(f"{url}: HTTP {response.status_code}")
            return response
        return None

    def get_json(self, url: str, params: dict | None = None) -> Any | None:
        response = self.get(url, params=params)
        if response is None:
            return None
        try:
            return response.json()
        except ValueError as exc:
            raise FetchError(f"{url}: response was not JSON") from exc

    def get_text(self, url: str) -> str | None:
        response = self.get(url)
        return None if response is None else response.text
