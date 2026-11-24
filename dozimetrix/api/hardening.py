"""Web hardening: security headers, a same-origin check for writes, request size caps,
an optional Host allowlist, and a small in-memory login throttle.

Pure ASGI middleware, so streamed responses (/api/live, behind lighttpd with
stream-response-body = 2) pass through untouched.
"""

from __future__ import annotations

import json
import threading
import time
from collections import deque
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request

CSP = ("default-src 'self'; script-src 'self'; "
       # ECharts and a few components set style attributes; no inline scripts anywhere
       "style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; font-src 'self'; "
       "connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; "
       "frame-ancestors 'none'")

SECURITY_HEADERS = [
    (b"content-security-policy", CSP.encode()),
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"same-origin"),
    (b"cross-origin-opener-policy", b"same-origin"),
    (b"permissions-policy", b"camera=(), microphone=(), geolocation=()"),
]
HSTS = (b"strict-transport-security", b"max-age=31536000")
UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}


def _reject(status: int, message: str):
    body = json.dumps({"error": message}).encode()

    async def send_it(send):
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"application/json"),
                                (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})
    return send_it


class HardeningMiddleware:
    def __init__(self, app, max_body: int, allowed_hosts: list[str] | None = None):
        self.app = app
        self.max_body = max_body
        self.allowed_hosts = {h.lower() for h in (allowed_hosts or []) if "*" not in h}

    def _same_origin(self, source: str, headers: dict) -> bool:
        """The page's origin is this server as the browser sees it: the Host header, or the
        host a reverse proxy forwarded, or one of [web] allowed_hosts."""
        netloc = urlsplit(source).netloc.lower()
        if not netloc:
            return False
        seen = {headers.get("host", "").lower(),
                *(h.strip().lower() for h in headers.get("x-forwarded-host", "").split(","))}
        return netloc in seen or netloc.split(":")[0] in self.allowed_hosts

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = {k.decode("latin-1").lower(): v.decode("latin-1")
                   for k, v in scope.get("headers", [])}
        path, method = scope.get("path", ""), scope.get("method", "GET")

        if method in UNSAFE and path.startswith("/api/"):
            # a browser always sends Origin on cross-origin writes: it must be this host
            source = headers.get("origin") or headers.get("referer")
            if source and not self._same_origin(source, headers):
                return await _reject(403, "cross-origin request refused")(send)

        if path.startswith("/api/"):
            length = headers.get("content-length")
            if length and length.isdigit() and int(length) > self.max_body:
                return await _reject(413, "request body too large")(send)
            seen = 0
            inner_receive = receive

            async def receive():
                nonlocal seen
                message = await inner_receive()
                if message["type"] == "http.request":
                    seen += len(message.get("body", b""))
                    if seen > self.max_body:
                        raise HTTPException(413, "request body too large")
                return message

        https = scope.get("scheme") == "https"

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                have = {k.lower() for k, _ in message.get("headers", [])}
                extra = [h for h in SECURITY_HEADERS if h[0] not in have]
                if https and HSTS[0] not in have:
                    extra.append(HSTS)
                message = {**message, "headers": [*message.get("headers", []), *extra]}
            await send(message)

        return await self.app(scope, receive, send_with_headers)


def install_hardening(app: FastAPI, settings) -> None:
    app.add_middleware(HardeningMiddleware, max_body=int(settings.max_upload_mb * 1024 * 1024),
                       allowed_hosts=list(settings.web_allowed_hosts))
    if settings.web_allowed_hosts:
        from starlette.middleware.trustedhost import TrustedHostMiddleware

        app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(settings.web_allowed_hosts))


class LoginThrottle:
    """Failed attempts per client address and per username in a sliding window.

    In memory, per process: enough for one uvicorn worker, and it resets on restart.
    """

    def __init__(self, max_failures: int = 5, window_s: float = 900.0,
                 max_per_ip: int = 20, clock=time.monotonic):
        self.max_failures = max_failures
        self.max_per_ip = max_per_ip
        self.window_s = window_s
        self.clock = clock
        self._fails: dict[str, deque] = {}
        self._lock = threading.Lock()

    def _keys(self, request: Request, kind: str, username: str | None):
        ip = request.client.host if request.client else "?"
        keys = [(f"{kind}:ip:{ip}", self.max_per_ip)]
        if username is not None:
            keys.append((f"{kind}:user:{username.strip().lower()[:150]}", self.max_failures))
        return keys

    def _recent(self, key: str, now: float) -> deque:
        q = self._fails.setdefault(key, deque())
        while q and q[0] <= now - self.window_s:
            q.popleft()
        return q

    def check(self, request: Request, kind: str, username: str | None = None) -> None:
        now = self.clock()
        with self._lock:
            for key, limit in self._keys(request, kind, username):
                q = self._recent(key, now)
                if len(q) >= limit:
                    wait = int(q[0] + self.window_s - now) + 1
                    raise HTTPException(429, "too many failed attempts, try again later",
                                        headers={"Retry-After": str(wait)})
            if len(self._fails) > 10000:  # drop idle keys
                for k in [k for k, q in self._fails.items() if not self._recent(k, now)]:
                    del self._fails[k]

    def fail(self, request: Request, kind: str, username: str | None = None) -> None:
        now = self.clock()
        with self._lock:
            for key, _ in self._keys(request, kind, username):
                self._recent(key, now).append(now)

    def clear(self, request: Request, kind: str, username: str | None = None) -> None:
        with self._lock:
            for key, _ in self._keys(request, kind, username)[1:]:  # keep the per-IP count
                self._fails.pop(key, None)
