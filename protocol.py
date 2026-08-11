"""Claude Code 2.1.224 wire protocol for Hermes.

Port of pi-black's claude-code-protocol.ts. Pure functions only — no I/O
beyond optional identity discovery, no global state, fully unit-testable.

The legacy Hermes OAuth envelope ("You are Claude Code, Anthropic's official
CLI for Claude.") is stripped here, so it never reaches the wire. Hermes'
own system prompt, tools, and messages are passed through untouched.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------- constants

CLAUDE_CODE_VERSION_FALLBACK = "2.1.224"
CLAUDE_CODE_ENTRYPOINT = "sdk-cli"

CCH_PLACEHOLDER = "cch=00000"
CCH_SEED = 0x4D659218E32A3268

AGENT_SDK_SYSTEM_PROMPT = (
    "You are a Claude agent, built on Anthropic's Claude Agent SDK."
)
LEGACY_HERMES_OAUTH_SYSTEM_PROMPT = (
    "You are Claude Code, Anthropic's official CLI for Claude."
)
BILLING_PREFIX = "x-anthropic-billing-header: "

_MASK64 = 0xFFFFFFFFFFFFFFFF
_P1 = 0x9E3779B185EBCA87
_P2 = 0xC2B2AE3D27D4EB4F
_P3 = 0x165667B19E3779F9
_P4 = 0x85EBCA77C2B2AE63
_P5 = 0x27D4EB2F165667C5

_CCH_DONE_RE = re.compile(r"; cch=[0-9a-f]{5};$")

# Claude Code's canonical tool names. Mirrors pi's list exactly. Hermes tools
# (read_file, write_file, patch, shell, memory, ...) do not collide with any of
# these, so in practice this mapping is a verified no-op — kept for parity with
# pi and so third-party Hermes tools named e.g. "bash" still normalize.
CLAUDE_CODE_TOOLS = (
    "Task", "Bash", "Glob", "Grep", "Read", "Edit", "MultiEdit", "Write",
    "NotebookEdit", "WebFetch", "WebSearch", "BashOutput", "KillShell",
    "ExitPlanMode", "EnterPlanMode", "AskUserQuestion", "Skill", "TaskOutput",
    "TodoWrite",
)
_CC_TOOL_LOOKUP = {name.lower(): name for name in CLAUDE_CODE_TOOLS}


def to_claude_code_name(name: str) -> str:
    """Normalize a tool name to Claude Code casing when it matches."""
    return _CC_TOOL_LOOKUP.get(name.lower(), name)


# ------------------------------------------------------------------- xxhash


def _rotl(value: int, bits: int) -> int:
    value &= _MASK64
    return ((value << bits) | (value >> (64 - bits))) & _MASK64


def _u32(data: bytes, off: int) -> int:
    return int.from_bytes(data[off:off + 4], "little")


def _u64(data: bytes, off: int) -> int:
    return int.from_bytes(data[off:off + 8], "little")


def _round(acc: int, inp: int) -> int:
    return (_rotl((acc + inp * _P2) & _MASK64, 31) * _P1) & _MASK64


def _merge_round(acc: int, val: int) -> int:
    return ((acc ^ _round(0, val)) * _P1 + _P4) & _MASK64


def xxhash64_pure(data: bytes, seed: int = 0) -> int:
    """Seeded XXH64. Byte-for-byte equivalent to pi-black's xxHash64()."""
    length = len(data)
    off = 0

    if length >= 32:
        v1 = (seed + _P1 + _P2) & _MASK64
        v2 = (seed + _P2) & _MASK64
        v3 = seed & _MASK64
        v4 = (seed - _P1) & _MASK64
        while off <= length - 32:
            v1 = _round(v1, _u64(data, off))
            v2 = _round(v2, _u64(data, off + 8))
            v3 = _round(v3, _u64(data, off + 16))
            v4 = _round(v4, _u64(data, off + 24))
            off += 32
        h = (_rotl(v1, 1) + _rotl(v2, 7) + _rotl(v3, 12) + _rotl(v4, 18)) & _MASK64
        h = _merge_round(h, v1)
        h = _merge_round(h, v2)
        h = _merge_round(h, v3)
        h = _merge_round(h, v4)
    else:
        h = (seed + _P5) & _MASK64

    h = (h + length) & _MASK64

    while off <= length - 8:
        lane = _round(0, _u64(data, off))
        h = (_rotl(h ^ lane, 27) * _P1 + _P4) & _MASK64
        off += 8
    if off <= length - 4:
        h = (h ^ ((_u32(data, off) * _P1) & _MASK64)) & _MASK64
        h = (_rotl(h, 23) * _P2 + _P3) & _MASK64
        off += 4
    while off < length:
        h = (h ^ ((data[off] * _P5) & _MASK64)) & _MASK64
        h = (_rotl(h, 11) * _P1) & _MASK64
        off += 1

    h ^= h >> 33
    h = (h * _P2) & _MASK64
    h ^= h >> 29
    h = (h * _P3) & _MASK64
    h ^= h >> 32
    return h & _MASK64


def _select_xxhash():
    """Use the C xxhash module only if it proves byte-identical at import.

    The checksum must match a JS implementation exactly, so an accelerator is
    only trusted after it reproduces known vectors. Any divergence (or a
    missing module) silently falls back to the pure-Python path.
    """
    try:
        import xxhash as _c
    except Exception:
        return xxhash64_pure
    try:
        for data, seed in ((b"", 0), (b"abc", 0), (b"y" * 32, CCH_SEED),
                           ("日本語 ✓".encode("utf-8"), CCH_SEED)):
            if _c.xxh64_intdigest(data, seed=seed) != xxhash64_pure(data, seed):
                return xxhash64_pure
    except Exception:
        return xxhash64_pure

    def _fast(data: bytes, seed: int = 0) -> int:
        return _c.xxh64_intdigest(data, seed=seed)

    return _fast


# Public entry point. ~20x faster when the optional `xxhash` module is present
# and verified; otherwise identical pure-Python behaviour with zero deps.
xxhash64 = _select_xxhash()


# --------------------------------------------------- JS-compatible JSON dump


def _js_numbers(obj: Any) -> Any:
    """Coerce integral floats to int so output matches JS number formatting.

    Python renders 1.0 as "1.0"; JavaScript renders it as "1". The cch is
    recomputed server-side from a JS serializer, so the byte sequences must
    agree exactly.
    """
    if isinstance(obj, bool):
        return obj
    if isinstance(obj, float) and obj.is_integer():
        return int(obj)
    if isinstance(obj, dict):
        return {k: _js_numbers(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_js_numbers(v) for v in obj]
    return obj


def js_json_dumps(obj: Any) -> str:
    """Serialize like JavaScript's JSON.stringify (compact, non-ASCII raw)."""
    return json.dumps(
        _js_numbers(obj),
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


# ------------------------------------------------------------------ identity


def detect_claude_code_version() -> str:
    """Return the installed Claude Code version, or the pinned fallback."""
    for cmd in ("claude", "claude-code"):
        try:
            out = subprocess.run(
                [cmd, "--version"], capture_output=True, text=True, timeout=5
            )
        except Exception:
            continue
        if out.returncode != 0:
            continue
        m = re.search(r"(\d+\.\d+\.\d+)", out.stdout or "")
        if m:
            return m.group(1)
    return CLAUDE_CODE_VERSION_FALLBACK


_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.I,
)


def parse_identity(data: Any) -> Optional[Dict[str, str]]:
    """Extract {device_id, account_uuid} from Claude Code state, else None."""
    if not isinstance(data, dict):
        return None
    user_id = data.get("userID")
    account = data.get("oauthAccount")
    if not isinstance(user_id, str) or not isinstance(account, dict):
        return None
    account_uuid = account.get("accountUuid")
    if not re.fullmatch(r"[0-9a-f]{64}", user_id):
        return None
    if not isinstance(account_uuid, str) or not _UUID_RE.match(account_uuid):
        return None
    return {"device_id": user_id, "account_uuid": account_uuid}


def discover_identity(env: Optional[Dict[str, str]] = None,
                      config_path: Optional[Path] = None) -> Optional[Dict[str, str]]:
    """Read Claude Code identity in memory. Never copied, printed, or stored."""
    import os

    env = env if env is not None else dict(os.environ)
    device_id = env.get("CLAUDE_CODE_DEVICE_ID")
    account_uuid = env.get("CLAUDE_CODE_ACCOUNT_UUID")
    if device_id and account_uuid:
        found = parse_identity(
            {"userID": device_id, "oauthAccount": {"accountUuid": account_uuid}}
        )
        if found:
            return found

    if config_path is None:
        base = env.get("CLAUDE_CONFIG_DIR") or str(Path.home())
        config_path = Path(base) / ".claude.json"
    try:
        return parse_identity(json.loads(Path(config_path).read_text("utf-8")))
    except Exception:
        return None


# ------------------------------------------------------------ billing header


def _first_user_prompt(messages: Any) -> str:
    if not isinstance(messages, list):
        return ""
    for msg in messages:
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "".join(
                b.get("text", "")
                for b in content
                if isinstance(b, dict) and b.get("type") == "text"
            )
        return ""
    return ""


def version_fingerprint(messages: Any, version: str) -> str:
    """Prompt-dependent 3-hex cc_version suffix."""
    prompt = _first_user_prompt(messages)
    selected = "".join(
        (prompt[i] if i < len(prompt) and prompt[i] else "0") for i in (4, 7, 20)
    )
    payload = f"59cf53e54c78{selected}{version}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:3]


def billing_header(messages: Any, version: str) -> str:
    fp = version_fingerprint(messages, version)
    return (
        f"{BILLING_PREFIX}cc_version={version}.{fp}; "
        f"cc_entrypoint={CLAUDE_CODE_ENTRYPOINT}; {CCH_PLACEHOLDER};"
    )


# --------------------------------------------------------------- transform


def _normalize_system(system: Any) -> List[Dict[str, Any]]:
    """Coerce Anthropic's `system` (str | list | absent) into block form."""
    if system is None:
        return []
    if isinstance(system, str):
        return [{"type": "text", "text": system}]
    if isinstance(system, list):
        return list(system)
    return []


def _block_text(block: Any) -> Optional[str]:
    return block.get("text") if isinstance(block, dict) else None


def strip_legacy_and_prepend(system: Any, header: str) -> List[Dict[str, Any]]:
    """Remove the legacy/previous envelope, then prepend the CC 2.1.224 one."""
    blocks = _normalize_system(system)
    first = _block_text(blocks[0]) if len(blocks) >= 1 else None
    second = _block_text(blocks[1]) if len(blocks) >= 2 else None

    if (
        isinstance(first, str)
        and first.startswith(BILLING_PREFIX)
        and second == AGENT_SDK_SYSTEM_PROMPT
    ):
        remaining = blocks[2:]           # already transformed — re-transform
    elif first == LEGACY_HERMES_OAUTH_SYSTEM_PROMPT:
        remaining = blocks[1:]           # drop Hermes' legacy Claude Code block
    else:
        remaining = blocks

    return [
        {"type": "text", "text": header},
        {"type": "text", "text": AGENT_SDK_SYSTEM_PROMPT},
        *remaining,
    ]


def _map_tool_names(body: Dict[str, Any]) -> None:
    """Apply Claude Code casing to tool definitions and assistant tool_use."""
    tools = body.get("tools")
    if isinstance(tools, list):
        for tool in tools:
            if isinstance(tool, dict) and isinstance(tool.get("name"), str):
                tool["name"] = to_claude_code_name(tool["name"])
    messages = body.get("messages")
    if isinstance(messages, list):
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if (
                    isinstance(block, dict)
                    and block.get("type") == "tool_use"
                    and isinstance(block.get("name"), str)
                ):
                    block["name"] = to_claude_code_name(block["name"])


def transform_body(
    body: Dict[str, Any],
    *,
    version: str,
    session_id: Optional[str] = None,
    identity: Optional[Dict[str, str]] = None,
    map_tool_names: bool = True,
) -> Dict[str, Any]:
    """Apply the full Claude Code request envelope to an Anthropic body."""
    if not isinstance(body, dict):
        raise TypeError("hermes-black expected an Anthropic JSON request object")

    out = dict(body)
    header = billing_header(out.get("messages"), version)
    out["system"] = strip_legacy_and_prepend(out.get("system"), header)

    if identity and session_id:
        out["metadata"] = {
            "user_id": js_json_dumps(
                {
                    "device_id": identity["device_id"],
                    "account_uuid": identity["account_uuid"],
                    "session_id": session_id,
                }
            )
        }

    if map_tool_names:
        _map_tool_names(out)
    return out


def patch_cch(serialized: str) -> str:
    """Replace the cch placeholder with the seeded XXH64 of the final body."""
    try:
        body = json.loads(serialized)
    except Exception as exc:
        raise ValueError("hermes-black expected a JSON object body") from exc
    if not isinstance(body, dict):
        raise ValueError("hermes-black expected a JSON object body")

    system = body.get("system")
    if (
        not isinstance(system, list)
        or not system
        or not isinstance(system[0], dict)
        or not isinstance(system[0].get("text"), str)
    ):
        raise ValueError("hermes-black request is missing the billing system block")

    billing_text = system[0]["text"]
    if not billing_text.startswith(BILLING_PREFIX):
        raise ValueError("hermes-black request has an invalid billing block")
    if CCH_PLACEHOLDER not in billing_text:
        if _CCH_DONE_RE.search(billing_text):
            return serialized
        raise ValueError("hermes-black request has an invalid cch billing value")
    if not isinstance(body.get("model"), str) or "max_tokens" not in body:
        raise ValueError("hermes-black request is missing model or max_tokens")

    normalized = json.loads(serialized)
    normalized["model"] = ""
    normalized.pop("max_tokens", None)

    digest = xxhash64(js_json_dumps(normalized).encode("utf-8"), CCH_SEED)
    cch = format(digest & 0xFFFFF, "05x")
    body["system"][0]["text"] = billing_text.replace(CCH_PLACEHOLDER, f"cch={cch}")
    return js_json_dumps(body)


def is_oauth_token(value: Optional[str]) -> bool:
    """True only for Anthropic OAuth access tokens."""
    return bool(value) and "sk-ant-oat" in value


def claude_code_headers(version: str, session_id: Optional[str]) -> Dict[str, str]:
    headers = {
        "user-agent": f"claude-cli/{version} (external, {CLAUDE_CODE_ENTRYPOINT})",
        "x-app": "cli",
    }
    if session_id:
        headers["x-claude-code-session-id"] = session_id
    return headers
