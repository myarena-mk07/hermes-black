"""httpx transport that applies the Claude Code envelope to OAuth requests.

Transport level is deliberate: the cch is a checksum of the exact bytes on
the wire, so the component that computes it must also be the component that
serializes them. Anything higher (middleware, kwargs rewriting) would have to
guess how the Anthropic SDK serializes, which is fragile.

Non-OAuth requests, non-Anthropic hosts, and non-/v1/messages paths are passed
through completely untouched.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from typing import Optional

import httpx

from . import protocol as P

logger = logging.getLogger(__name__)

_MESSAGES_SUFFIXES = ("/v1/messages", "/v1/messages?beta=true")


def _allowed_host(host: str) -> bool:
    if host.endswith("anthropic.com"):
        return True
    # Test-only escape hatch for the offline wire harness.
    allow = os.environ.get("HERMES_BLACK_ALLOW_HOST", "")
    return bool(allow) and host == allow


def _should_transform(request: httpx.Request) -> bool:
    if request.method.upper() != "POST":
        return False
    if not any(str(request.url.path).endswith(s.split("?")[0])
               for s in _MESSAGES_SUFFIXES):
        return False
    if not _allowed_host(request.url.host):
        return False
    auth = request.headers.get("authorization", "")
    return P.is_oauth_token(auth)


# Only non-identifying rate-limit telemetry is recorded. Account and
# organization identifiers are deliberately excluded so a trace file can be
# shared or attached to a bug report without leaking who you are.
_RESP_PREFIX = "anthropic-ratelimit"
_RESP_DENY = ("anthropic-organization-id", "request-id", "x-request-id")


def _trace_response(response) -> None:
    """Record non-secret Anthropic response headers when tracing is enabled."""
    path = os.environ.get("HERMES_BLACK_TRACE")
    if not path:
        return
    try:
        picked = {k: v for k, v in response.headers.items()
                  if k.lower().startswith(_RESP_PREFIX)
                  and k.lower() not in _RESP_DENY}
        if not picked:
            picked = {"note": "no anthropic-ratelimit headers"}
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"response_status": response.status_code,
                                 "headers": picked}) + "\n")
    except Exception:
        pass


def _trace(body: dict, payload: bytes, version: str) -> None:
    """Opt-in diagnostics via HERMES_BLACK_TRACE=<path>. Never logs secrets."""
    path = os.environ.get("HERMES_BLACK_TRACE")
    if not path:
        return
    try:
        import datetime
        blocks = body.get("system") or []
        billing = blocks[0].get("text", "") if blocks else ""
        record = {
            "ts": datetime.datetime.now().isoformat(timespec="seconds"),
            "model": body.get("model"),
            "cc_version": (billing.split("cc_version=")[-1].split(";")[0]
                           if "cc_version=" in billing else None),
            "cch": (billing.split("cch=")[-1].rstrip(";") if "cch=" in billing else None),
            "system_blocks": len(blocks),
            "tools": len(body.get("tools") or []),
            "has_metadata": "metadata" in body,
            "bytes": len(payload),
        }
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
    except Exception:
        pass


def _rewrite(request: httpx.Request, version: str, session_id: str,
             identity: Optional[dict], map_tool_names: bool) -> httpx.Request:
    raw = request.read()
    if not raw:
        return request
    body = json.loads(raw.decode("utf-8"))

    transformed = P.transform_body(
        body,
        version=version,
        session_id=session_id,
        identity=identity,
        map_tool_names=map_tool_names,
    )
    payload = P.patch_cch(P.js_json_dumps(transformed)).encode("utf-8")
    _trace(json.loads(payload.decode("utf-8")), payload, version)

    headers = httpx.Headers(request.headers)
    for key, value in P.claude_code_headers(version, session_id).items():
        headers[key] = value
    headers["x-client-request-id"] = str(uuid.uuid4())
    headers["content-length"] = str(len(payload))

    return httpx.Request(
        method=request.method,
        url=request.url,
        headers=headers,
        content=payload,
        extensions=request.extensions,
    )


class ClaudeCodeTransport(httpx.BaseTransport):
    """Sync transport wrapper."""

    def __init__(self, inner, version, session_id, identity, map_tool_names=True):
        self._inner = inner
        self._version = version
        self._session_id = session_id
        self._identity = identity
        self._map_tool_names = map_tool_names

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        if _should_transform(request):
            try:
                request = _rewrite(request, self._version, self._session_id,
                                   self._identity, self._map_tool_names)
            except Exception:
                # Fail open: a broken envelope must never break Hermes.
                logger.warning("hermes-black: envelope skipped", exc_info=True)
            response = self._inner.handle_request(request)
            _trace_response(response)
            return response
        return self._inner.handle_request(request)

    def close(self) -> None:
        close = getattr(self._inner, "close", None)
        if close:
            close()


class AsyncClaudeCodeTransport(httpx.AsyncBaseTransport):
    """Async transport wrapper."""

    def __init__(self, inner, version, session_id, identity, map_tool_names=True):
        self._inner = inner
        self._version = version
        self._session_id = session_id
        self._identity = identity
        self._map_tool_names = map_tool_names

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if _should_transform(request):
            try:
                request = _rewrite(request, self._version, self._session_id,
                                   self._identity, self._map_tool_names)
            except Exception:
                logger.warning("hermes-black: envelope skipped", exc_info=True)
        return await self._inner.handle_async_request(request)

    async def aclose(self) -> None:
        aclose = getattr(self._inner, "aclose", None)
        if aclose:
            await aclose()
