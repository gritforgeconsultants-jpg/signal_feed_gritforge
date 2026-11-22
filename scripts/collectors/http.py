"""HTTP for collectors. Standard library only.

- One request per MIN_INTERVAL_S seconds per host, every collector, every call.
- robots.txt is honored for crawled pages (agenda RSS/HTML). Documented APIs (SAM.gov,
  Socrata, CKAN, Legistar Web API) are governed by their API terms instead, which is
  what terms_checked in the allowlist records.
- API keys never reach logs, alerts, errors or fixtures: every URL is redacted first.
- RecordingFetcher saves real responses as fixtures; ReplayFetcher serves them with no
  network. CI only ever uses ReplayFetcher.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib import error, request, robotparser
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

USER_AGENT = "GritForgeSignalFeed/0.1 (public-records collector)"
MIN_INTERVAL_S = 5.0
TIMEOUT_S = 30
MAX_BYTES = 10 * 1024 * 1024
SECRET_PARAMS = {"api_key", "apikey", "key", "token", "app_token", "$$app_token", "access_token"}


class FetchError(Exception):
    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.status = status


class RobotsDisallowed(FetchError):
    pass


def build_url(url: str, params: Optional[Dict[str, Any]] = None) -> str:
    if not params:
        return url
    return url + ("&" if "?" in url else "?") + urlencode(params)


def canonical(url: str) -> str:
    """Redacted URL with sorted query params: the key fixtures are stored under."""
    p = urlparse(url)
    q = sorted((k, "***" if k.lower() in SECRET_PARAMS else v)
               for k, v in parse_qsl(p.query, keep_blank_values=True))
    return urlunparse(p._replace(query=urlencode(q)))


redact = canonical


def host_ok(url: str, domain: str) -> bool:
    """True if url is https on domain or a subdomain of it."""
    try:
        p = urlparse(url or "")
    except ValueError:
        return False
    h = (p.hostname or "").lower()
    return p.scheme == "https" and (h == domain or h.endswith("." + domain))


class Fetcher:
    def __init__(self, min_interval: float = MIN_INTERVAL_S, timeout: float = TIMEOUT_S,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic):
        self.min_interval = min_interval
        self.timeout = timeout
        self.sleep = sleep
        self.clock = clock
        self._last: Dict[str, float] = {}
        self._robots: Dict[str, robotparser.RobotFileParser] = {}
        self.requests = 0

    # ── public ───────────────────────────────────────────────────────────
    def get_text(self, url: str, params: Optional[Dict[str, Any]] = None, *,
                 robots: bool = False) -> str:
        full = build_url(url, params)
        if robots and not self.allowed(full):
            raise RobotsDisallowed("robots.txt disallows %s" % redact(full))
        status, body = self._get(full)
        if status >= 400:
            raise FetchError("HTTP %d from %s" % (status, redact(full)), status)
        return body

    def get_json(self, url: str, params: Optional[Dict[str, Any]] = None, *,
                 robots: bool = False) -> Any:
        body = self.get_text(url, params, robots=robots)
        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise FetchError("not JSON from %s: %s" % (redact(build_url(url, params)), exc))

    def allowed(self, url: str) -> bool:
        """RFC 9309: 4xx robots.txt means allow all; 5xx or unreachable means disallow all."""
        p = urlparse(url)
        base = "%s://%s" % (p.scheme, p.netloc)
        rp = self._robots.get(base)
        if rp is None:
            rp = robotparser.RobotFileParser()
            try:
                status, body = self._get(base + "/robots.txt")
            except FetchError:
                status, body = 503, ""
            if status >= 500:
                rp.disallow_all = True
            elif status >= 400:
                rp.allow_all = True
            else:
                rp.parse(body.splitlines())
            rp.modified()
            self._robots[base] = rp
        return rp.can_fetch(USER_AGENT, url)

    # ── internals ────────────────────────────────────────────────────────
    def _get(self, url: str) -> Tuple[int, str]:
        self._throttle(urlparse(url).hostname or "")
        self.requests += 1
        return self._raw(url)

    def _throttle(self, host: str) -> None:
        last = self._last.get(host)
        if last is not None:
            wait = self.min_interval - (self.clock() - last)
            if wait > 0:
                self.sleep(wait)
        self._last[host] = self.clock()

    def _raw(self, url: str) -> Tuple[int, str]:
        req = request.Request(url, headers={"User-Agent": USER_AGENT,
                                            "Accept": "application/json, application/xml, text/*"})
        try:
            with request.urlopen(req, timeout=self.timeout) as resp:
                return resp.status, resp.read(MAX_BYTES).decode("utf-8", "replace")
        except error.HTTPError as exc:
            return exc.code, exc.read(64 * 1024).decode("utf-8", "replace")
        except (error.URLError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise FetchError("request to %s failed: %s" % (redact(url), reason))


class RecordingFetcher(Fetcher):
    """Live fetcher that also keeps every response, redacted, for use as a fixture."""

    def __init__(self, **kw: Any):
        super().__init__(**kw)
        self.records: List[Dict[str, Any]] = []

    def _raw(self, url: str) -> Tuple[int, str]:
        status, body = super()._raw(url)
        try:
            stored: Any = json.loads(body)
        except json.JSONDecodeError:
            stored = body
        self.records.append({"url": canonical(url), "status": status, "body": stored})
        return status, body

    def save(self, path: Any) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps({"requests": self.records}, indent=1, ensure_ascii=False),
                              encoding="utf-8")


class ReplayFetcher(Fetcher):
    """No network. Serves responses recorded by RecordingFetcher (or written by hand)."""

    def __init__(self, fixture: Any, **kw: Any):
        kw.setdefault("min_interval", 0)
        kw.setdefault("sleep", lambda s: None)
        super().__init__(**kw)
        if not isinstance(fixture, dict):
            fixture = json.loads(Path(fixture).read_text(encoding="utf-8"))
        self.routes = {canonical(r["url"]): r for r in fixture["requests"]}
        self.calls: List[str] = []

    def _raw(self, url: str) -> Tuple[int, str]:
        key = canonical(url)
        self.calls.append(key)
        if key not in self.routes:
            raise FetchError("no recorded response for %s" % key)
        r = self.routes[key]
        body = r.get("body", "")
        return int(r.get("status", 200)), body if isinstance(body, str) else json.dumps(body)
