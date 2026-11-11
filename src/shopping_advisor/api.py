"""Authenticated local-only shopping API; polling never schedules a model turn."""
from __future__ import annotations

import asyncio
import json
import math
import re
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from .schemas import (
    Command,
    DomainError,
    MessageCommand,
    Preferences,
    PreferencesCommand,
    ProductCommand,
    ProposalCommand,
    ReconcileCommand,
    VersionedCommand,
)
from .service import Service

MAX_BODY_BYTES = 128 * 1024
BODY_TIMEOUT = 5.0
_SECURITY_HEADERS = {
    "content-security-policy": "default-src 'none'; script-src 'self'; style-src 'self'; "
    "connect-src 'self'; img-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
    "x-content-type-options": "nosniff", "x-frame-options": "DENY",
    "cache-control": "no-store", "referrer-policy": "no-referrer",
    "permissions-policy": "camera=(), microphone=(), geolocation=()",
    "cross-origin-opener-policy": "same-origin", "cross-origin-resource-policy": "same-origin",
}
_SENSITIVE = {
    b"host", b"authorization", b"origin", b"content-type", b"content-length", b"content-encoding",
    b"transfer-encoding", b"sec-fetch-site", b"sec-fetch-mode", b"sec-fetch-dest", b"cookie",
}
_PROXY_HEADERS = {b"forwarded", b"x-real-ip", b"x-original-url", b"x-rewrite-url",
                  b"proxy-authorization", b"proxy-connection"}


def _error(status, code, message):
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)


def _origin_host(origin):
    try:
        parsed = urlsplit(origin)
        port = parsed.port
        host = "127.0.0.1" + (f":{port}" if port is not None else "")
        if (origin != "http://" + host or parsed.hostname != "127.0.0.1"
                or (port is not None and not 1 <= port <= 65535)):
            raise ValueError
        return "127.0.0.1" if port == 80 else host
    except (ValueError, TypeError, AttributeError):
        raise ValueError("origin必须为规范的127.0.0.1 HTTP源，不含路径或凭据") from None


def _json_object(raw):
    def pairs(entries):
        result = {}
        for key, value in entries:
            if key in result:
                raise ValueError
            result[key] = value
        return result

    def reject(value):
        raise ValueError

    value = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs, parse_constant=reject)
    nodes = 0

    def visit(item, depth=0):
        nonlocal nodes
        nodes += 1
        if depth > 20 or nodes > 4096:
            raise ValueError
        if isinstance(item, dict):
            for key, child in item.items():
                key.encode("utf-8")
                visit(child, depth + 1)
        elif isinstance(item, list):
            for child in item:
                visit(child, depth + 1)
        elif isinstance(item, str):
            item.encode("utf-8")
        elif isinstance(item, float) and not math.isfinite(item):
            raise ValueError

    visit(value)
    if type(value) is not dict:
        raise ValueError
    return value


class LocalBoundary:
    """Validate raw ASGI headers and consume a bounded stream before any mutation."""

    def __init__(self, app, *, origin):
        host = _origin_host(origin)
        self.app, self.origin, self.host = app, ("http://" + host).encode("ascii"), host.encode("ascii")

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            else:
                await self.app(scope, receive, send)
            return
        started = False

        async def secured_send(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                additions = [(key.encode(), value.encode()) for key, value in _SECURITY_HEADERS.items()]
                blocked = {key for key, _ in additions}
                kept = [(key, value) for key, value in message.get("headers", []) if key.lower() not in blocked]
                message = {**message, "headers": kept + additions}
            await send(message)

        async def fail(status, code, message):
            await _error(status, code, message)(scope, receive, secured_send)

        headers = {}
        for name, value in scope.get("headers", []):
            name = name.lower()
            if name in _PROXY_HEADERS or name.startswith(b"x-forwarded-"):
                return await fail(400, "proxy_header", "不接受代理转发头")
            if name in _SENSITIVE and name in headers:
                return await fail(400, "duplicate_header", "敏感请求头不可重复")
            headers[name] = value
        if headers.get(b"host") != self.host:
            return await fail(400, "invalid_host", "Host必须与配置的数字回环地址一致")
        raw_path = scope.get("raw_path", b"")
        if (scope.get("query_string") or b"%" in raw_path or b"\\" in raw_path or b"//" in raw_path
                or any(part in {b".", b".."} for part in raw_path.split(b"/"))
                or not re.fullmatch(rb"/[A-Za-z0-9_./-]*", raw_path)):
            return await fail(400, "invalid_path", "不接受查询参数、编码或歧义路径")
        origin, site = headers.get(b"origin"), headers.get(b"sec-fetch-site")
        if (origin is not None and origin != self.origin) or site in {b"cross-site", b"same-site"}:
            return await fail(403, "cross_origin", "只允许严格同源请求")
        method = scope["method"]
        if method not in {"GET", "HEAD", "POST"}:
            return await fail(405, "method_not_allowed", "请求方法不受支持")
        length, transfer = headers.get(b"content-length"), headers.get(b"transfer-encoding")
        if ((length is not None and not re.fullmatch(rb"0|[1-9][0-9]{0,9}", length))
                or (length is not None and transfer is not None)
                or (transfer is not None and transfer != b"chunked")):
            return await fail(400, "invalid_framing", "请求体边界不明确")
        if method == "POST":
            if (origin != self.origin or site != b"same-origin"
                    or headers.get(b"sec-fetch-mode") not in {b"cors", b"same-origin"}
                    or headers.get(b"sec-fetch-dest") != b"empty"):
                return await fail(403, "mutation_origin", "写操作须有严格同源Origin和Fetch Metadata")
            if (headers.get(b"content-type", b"").lower() not in
                    {b"application/json", b"application/json; charset=utf-8"} or b"content-encoding" in headers):
                return await fail(415, "json_required", "只接受未压缩的UTF-8 JSON")
            if length is not None and int(length) > MAX_BODY_BYTES:
                return await fail(413, "body_limit", "请求体超过128KiB限制")
            raw = bytearray()
            try:
                async with asyncio.timeout(BODY_TIMEOUT):
                    while True:
                        event = await receive()
                        if event["type"] == "http.disconnect":
                            return
                        if event["type"] != "http.request":
                            return await fail(400, "invalid_body", "请求体不合法")
                        chunk = event.get("body", b"")
                        if len(raw) + len(chunk) > MAX_BODY_BYTES:
                            return await fail(413, "body_limit", "请求体超过128KiB限制")
                        raw.extend(chunk)
                        if not event.get("more_body", False):
                            break
            except TimeoutError:
                return await fail(408, "body_timeout", "读取请求体超时，未执行操作")
            if length is not None and len(raw) != int(length):
                return await fail(400, "invalid_framing", "请求体长度不一致")
            try:
                scope.setdefault("state", {})["command_body"] = _json_object(bytes(raw))
            except (ValueError, UnicodeError, RecursionError, OverflowError):
                return await fail(400, "invalid_json", "JSON须为有限、无重复字段且深度受限的UTF-8对象")
        elif (length is not None and length != b"0") or transfer is not None:
            return await fail(400, "unexpected_body", "只读请求不可携带请求体")
        try:
            await self.app(scope, receive, secured_send)
        except Exception:
            # Never expose the raw request or exception in responses/server logs.
            if not started:
                await fail(500, "internal_error", "处理结果不明；请保留原action_id核实并重试原请求")


def create_app(store, provider=None, *, service=None, origin="http://127.0.0.1:8765"):
    _origin_host(origin)
    if (provider is None) == (service is None):
        raise ValueError("必须互斥提供provider或service；不自动选择模型")
    if service is not None and service.store is not store:
        raise ValueError("注入service必须使用同一Store实例")
    configured_provider = provider if provider is not None else service.provider
    test_mode = getattr(configured_provider, "is_test_provider", False) is True

    @asynccontextmanager
    async def lifespan(app):
        app.state.service = service if service is not None else await run_in_threadpool(Service, store, provider)
        try:
            yield
        finally:
            if service is None:
                await run_in_threadpool(app.state.service.close)

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None, redirect_slashes=False)
    app.add_middleware(LocalBoundary, origin=origin)

    @app.exception_handler(DomainError)
    async def domain_error(request, exc):
        return _error(exc.status, exc.code, exc.message)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, exc):
        return _error(422, "invalid_command", "请求字段不符合严格结构")

    def principal(request: Request):
        authorization = request.headers.get("authorization", "")
        if not re.fullmatch(r"Bearer [A-Za-z0-9._~+/-]{1,128}={0,2}", authorization):
            raise DomainError("unauthorized", "请提供Bearer身份令牌", 401)
        return store.authenticate(authorization[7:])

    def command(request, model):
        body = request.state.command_body
        try:
            if model is PreferencesCommand:
                prefs = body.get("preferences")
                if type(prefs) is not dict or set(prefs) != set(Preferences.model_fields):
                    raise ValueError
            if model is ReconcileCommand and not str(body.get("note", "")).strip():
                raise ValueError
            return model.model_validate(body)
        except (ValueError, ValidationError):
            raise DomainError("invalid_command", "请求字段不符合严格结构；偏好须完整填写，对账须有明确依据", 422) from None

    @app.get("/api/health")
    def health():
        return {"status": "ok", "test_mode": test_mode}

    @app.get("/api/me")
    def me(user=Depends(principal)):
        return {**user.model_dump(), "test_mode": test_mode}

    @app.get("/api/sessions")
    def sessions(user=Depends(principal)):
        with store.transaction() as db:
            store.require(db, user, "customer")
            return store.list(db, "session", owner=user.user_id)

    @app.post("/api/sessions")
    def create_session(request: Request, user=Depends(principal)):
        return request.app.state.service.actions.create_session(user, command(request, Command))

    @app.get("/api/sessions/{sid}")
    def session(sid: str, request: Request, user=Depends(principal)):
        return request.app.state.service.actions.view(user, sid)

    @app.post("/api/sessions/{sid}/messages")
    def message(sid: str, request: Request, user=Depends(principal)):
        return request.app.state.service.submit(user, sid, command(request, MessageCommand))

    @app.post("/api/sessions/{sid}/cancel")
    def cancel(sid: str, request: Request, user=Depends(principal)):
        return request.app.state.service.actions.cancel(user, sid, command(request, VersionedCommand))

    @app.post("/api/sessions/{sid}/preferences")
    def preferences(sid: str, request: Request, user=Depends(principal)):
        return request.app.state.service.actions.set_preferences(user, sid, command(request, PreferencesCommand))

    @app.post("/api/proposals/{pid}/decision")
    def decision(pid: str, request: Request, user=Depends(principal)):
        return request.app.state.service.actions.decide_proposal(user, pid, command(request, ProposalCommand))

    @app.get("/api/catalog")
    def catalog(request: Request, user=Depends(principal)):
        return request.app.state.service.actions.catalog_view(user)

    @app.post("/api/catalog/{pid}")
    def update_product(pid: str, request: Request, user=Depends(principal)):
        return request.app.state.service.actions.update_product(user, pid, command(request, ProductCommand))

    @app.get("/api/usage/unknown")
    def unknown(user=Depends(principal)):
        with store.transaction() as db:
            store.require(db, user, "operator")
            rows = db.execute("SELECT id,run_id,tokens,state,usage_kind FROM calls WHERE state='unknown' "
                              "ORDER BY rowid").fetchall()
            return [dict(row) for row in rows]

    @app.post("/api/usage/reconcile")
    def reconcile(request: Request, user=Depends(principal)):
        return request.app.state.service.runner.reconcile(user, command(request, ReconcileCommand))

    assets = Path(__file__).parent

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(assets / "templates" / "index.html", media_type="text/html")

    @app.get("/static/app.js", include_in_schema=False)
    def javascript():
        return FileResponse(assets / "static" / "app.js", media_type="text/javascript")

    @app.get("/static/style.css", include_in_schema=False)
    def stylesheet():
        return FileResponse(assets / "static" / "style.css", media_type="text/css")

    return app
