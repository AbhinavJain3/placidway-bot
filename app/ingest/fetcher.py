"""Polite fetcher: honours robots.txt, identifies itself, rate-limits to ~1 req/s,
and uses conditional GETs (ETag / Last-Modified) so unchanged pages cost almost nothing."""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx

log = logging.getLogger(__name__)


@dataclass
class FetchResult:
    url: str
    status: int  # 200 fetched, 304 not modified (served from cache), 0 failed
    html: str | None
    error: str | None = None


class PoliteFetcher:
    def __init__(self, user_agent: str, cache_dir: Path, delay_s: float = 1.0, retries: int = 3):
        self.user_agent = user_agent
        self.delay_s = delay_s
        self.retries = retries
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._meta_path = cache_dir / "meta.json"
        self._meta: dict[str, dict] = json.loads(self._meta_path.read_text()) if self._meta_path.exists() else {}
        self._robots: dict[str, RobotFileParser] = {}
        self._last_request = 0.0
        self._client = httpx.Client(
            headers={"User-Agent": user_agent, "Accept": "text/html"},
            timeout=20.0,
            follow_redirects=True,
        )

    def close(self) -> None:
        self._client.close()
        self._meta_path.write_text(json.dumps(self._meta, indent=2))

    def _throttle(self) -> None:
        wait = self.delay_s - (time.monotonic() - self._last_request)
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.monotonic()

    def allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self._robots:
            rp = RobotFileParser()
            self._throttle()
            resp = self._client.get(f"{origin}/robots.txt")
            rp.parse(resp.text.splitlines() if resp.status_code == 200 else [])
            self._robots[origin] = rp
        return self._robots[origin].can_fetch(self.user_agent, url)

    def _cache_file(self, url: str) -> Path:
        slug = urlsplit(url).path.strip("/").replace("/", "_")
        return self.cache_dir / f"{slug}.html"

    def fetch(self, url: str) -> FetchResult:
        if not self.allowed(url):
            return FetchResult(url, 0, None, "disallowed by robots.txt")

        cache_file = self._cache_file(url)
        meta = self._meta.get(url, {})
        headers = {}
        if cache_file.exists():
            if meta.get("etag"):
                headers["If-None-Match"] = meta["etag"]
            if meta.get("last_modified"):
                headers["If-Modified-Since"] = meta["last_modified"]

        last_err = ""
        for attempt in range(1, self.retries + 1):
            self._throttle()
            try:
                resp = self._client.get(url, headers=headers)
            except httpx.HTTPError as exc:
                last_err = f"{type(exc).__name__}: {exc}"
            else:
                if resp.status_code == 304:
                    return FetchResult(url, 304, cache_file.read_text(encoding="utf-8"))
                if resp.status_code == 200:
                    cache_file.write_text(resp.text, encoding="utf-8")
                    self._meta[url] = {
                        "etag": resp.headers.get("etag"),
                        "last_modified": resp.headers.get("last-modified"),
                        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    }
                    return FetchResult(url, 200, resp.text)
                last_err = f"HTTP {resp.status_code}"
                if resp.status_code < 500 and resp.status_code != 429:
                    break
            log.warning("fetch %s failed (attempt %d): %s", url, attempt, last_err)
            time.sleep(2 ** attempt)

        # Network/site trouble: fall back to the last good copy rather than dropping the page.
        if cache_file.exists():
            log.warning("using cached copy of %s", url)
            return FetchResult(url, 304, cache_file.read_text(encoding="utf-8"), last_err)
        return FetchResult(url, 0, None, last_err)
