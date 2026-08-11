"""hermes-black — route Hermes' Anthropic OAuth traffic as Claude Code 2.1.224.

Hermes keeps its own harness end to end: its agent loop, tools, skills, memory,
MCP servers, prompt caching, streaming and usage accounting are untouched. Only
the request envelope is rewritten, and only for Anthropic OAuth requests.

Hermes' legacy OAuth envelope is stripped before the request leaves the process,
so from Anthropic's side it never existed.

Design mirrors pi-black: wrap, never fork. Nothing in ~/.hermes/hermes-agent is
modified, so `hermes update` cannot revert or break this plugin.
"""

from __future__ import annotations

import logging
import uuid
from typing import Optional

from . import protocol as P
from .transport import AsyncClaudeCodeTransport, ClaudeCodeTransport

logger = logging.getLogger(__name__)

_STATE = {"installed": False, "version": None, "identity": None,
          "session_id": None, "wrapped": 0}


def _session_id() -> str:
    if not _STATE["session_id"]:
        _STATE["session_id"] = str(uuid.uuid4())
    return _STATE["session_id"]


def _wrap_client(client, api_key: Optional[str]) -> None:
    """Swap the SDK's httpx transport for the envelope-applying wrapper."""
    if not P.is_oauth_token(api_key or ""):
        return
    inner_http = getattr(client, "_client", None)
    if inner_http is None:
        return
    current = getattr(inner_http, "_transport", None)
    if current is None or isinstance(
        current, (ClaudeCodeTransport, AsyncClaudeCodeTransport)
    ):
        return

    version = _STATE["version"] or P.detect_claude_code_version()
    _STATE["version"] = version
    identity = _STATE["identity"]
    sid = _session_id()

    if isinstance(current, __import__("httpx").AsyncBaseTransport):
        inner_http._transport = AsyncClaudeCodeTransport(
            current, version, sid, identity
        )
    else:
        inner_http._transport = ClaudeCodeTransport(current, version, sid, identity)

    # Per-host mounts bypass the default transport; wrap those too.
    for key, mounted in list(getattr(inner_http, "_mounts", {}).items() or []):
        if mounted is None or isinstance(
            mounted, (ClaudeCodeTransport, AsyncClaudeCodeTransport)
        ):
            continue
        if isinstance(mounted, __import__("httpx").AsyncBaseTransport):
            inner_http._mounts[key] = AsyncClaudeCodeTransport(
                mounted, version, sid, identity
            )
        else:
            inner_http._mounts[key] = ClaudeCodeTransport(
                mounted, version, sid, identity
            )

    _STATE["wrapped"] += 1


def install() -> bool:
    """Monkeypatch Hermes' Anthropic client factory. Idempotent."""
    if _STATE["installed"]:
        return True
    try:
        from agent import anthropic_adapter as A
    except Exception:
        logger.warning("hermes-black: anthropic_adapter unavailable", exc_info=True)
        return False

    original = getattr(A, "build_anthropic_client", None)
    if original is None or getattr(original, "_hermes_black", False):
        return True

    _STATE["identity"] = P.discover_identity()
    _STATE["version"] = P.detect_claude_code_version()

    def patched(api_key, base_url=None, timeout=None, **kwargs):
        client = original(api_key, base_url=base_url, timeout=timeout, **kwargs)
        try:
            key = api_key if isinstance(api_key, str) else None
            _wrap_client(client, key)
        except Exception:
            logger.warning("hermes-black: client wrap failed", exc_info=True)
        return client

    patched._hermes_black = True
    patched._hermes_black_original = original
    A.build_anthropic_client = patched
    _STATE["installed"] = True
    logger.info(
        "hermes-black active (claude-code %s, identity=%s)",
        _STATE["version"],
        "yes" if _STATE["identity"] else "no",
    )
    return True


def uninstall() -> bool:
    """Restore Hermes' original factory."""
    try:
        from agent import anthropic_adapter as A
    except Exception:
        return False
    current = getattr(A, "build_anthropic_client", None)
    original = getattr(current, "_hermes_black_original", None)
    if original is not None:
        A.build_anthropic_client = original
    _STATE["installed"] = False
    return True


def status() -> dict:
    return dict(_STATE)


def register(ctx):
    """Hermes plugin entry point."""
    install()
