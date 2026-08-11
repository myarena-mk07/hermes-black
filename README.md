# Hermes Black

Use your Claude Pro/Max subscription with [Hermes Agent](https://github.com/NousResearch/hermes-agent).

Hermes Black is an unofficial Hermes plugin that routes Anthropic **OAuth**
requests through your existing Claude subscription by applying Claude Code
2.1.224 request conventions.

**Hermes keeps its own harness.** Its agent loop, tools, skills, memory, MCP
servers, prompt caching, streaming and usage accounting are untouched. Only the
request envelope changes.

It is a Python port of [pi-black](https://github.com/paoloanzn/pi-black), and
is verified byte-for-byte against that implementation.

## Requirements

- Hermes Agent with `agent/anthropic_adapter.py` (v0.20.x tested)
- A Claude Pro or Max subscription, already logged in via Claude Code
- Python 3.9+ (no extra packages)

## Install

```sh
git clone https://github.com/myarena-mk07/hermes-black.git ~/.hermes/plugins/hermes-black
hermes plugins enable hermes-black
```

That's it. There are **no credentials to configure** — Hermes already resolves
your Claude Code OAuth token (macOS Keychain or `~/.claude/.credentials.json`).

Verify:

```sh
HERMES_BLACK_TRACE=/tmp/hb.jsonl hermes -z "say OK" \
  -m claude-haiku-4-5-20251001 --provider anthropic
cat /tmp/hb.jsonl
```

A record with a non-zero `cch` means the envelope was applied:

```json
{"model":"claude-haiku-4-5-20251001","cc_version":"2.1.224.f19","cch":"84159",
 "system_blocks":4,"tools":32,"has_metadata":true,"bytes":94798}
```

## Confirming you are on plan billing

The trace also records Anthropic's rate-limit headers:

```
anthropic-ratelimit-unified-5h-status           allowed
anthropic-ratelimit-unified-representative-claim five_hour
```

`representative-claim: five_hour` means the request was claimed against your
subscription's 5-hour window rather than pay-per-token extra usage.

## Troubleshooting

### Anthropic is missing from the Desktop model picker (but works in the CLI)

Expected, and not caused by this plugin. Hermes Desktop always requests the
model list with `explicit_only=1`, and that filter keeps only providers you
**explicitly** configured:

```python
# hermes_cli/auth.py
def is_provider_explicitly_configured(provider_id):
    """...used to gate auto-discovery of external credentials
    (e.g. Claude Code's ~/.claude/.credentials.json) so they are
    never used without the user's explicit choice."""
```

If your Anthropic access was auto-discovered from Claude Code, Desktop hides it
by design. The backend still has it — querying the picker API without that flag
returns `anthropic | authenticated: True | total_models: 12`.

Make the choice explicit, then restart Desktop:

```sh
hermes model      # select Anthropic, then a Claude model
```

That writes `model.provider: anthropic` into `config.yaml`. Setting
`active_provider: anthropic` in `auth.json`, or a MoA slot with
`provider: anthropic`, works too.

> **Do not set `ANTHROPIC_API_KEY` to make it appear.** It satisfies the check,
> but it sits *above* your OAuth token in Hermes' resolution order, so requests
> would silently switch from your Claude plan to pay-per-token API billing.

Note this makes Anthropic your default provider. There is no supported way to
keep a different default while showing Anthropic in the picker — the visibility
gate *is* the explicit-choice gate.

### `~/.claude/.credentials.json` looks expired

Harmless. Claude Code 2.1.114+ stores live credentials in the macOS Keychain
and may leave the JSON file stale. Hermes reads both and prefers whichever is
valid. This plugin never reads, writes, or refreshes either.

### The trace file is empty

The envelope only applies to Anthropic **OAuth** requests on `anthropic.com`.
An API key, a proxy provider (for example a `ce-claude`-style gateway), or any
other provider is passed through untouched and is never traced.

## Update / uninstall

```sh
git -C ~/.hermes/plugins/hermes-black pull      # update
hermes plugins disable hermes-black             # turn off
rm -rf ~/.hermes/plugins/hermes-black           # remove
```

Nothing in the Hermes installation is modified, so `hermes update` can never
break or revert the plugin.

## What it changes

For Anthropic OAuth requests only:

- replaces Hermes' legacy `"You are Claude Code…"` system block with the
  billing header block and the Agent SDK block
- computes the structure-aware `cch` checksum (seeded XXH64) over the exact
  bytes sent
- adds Claude Code identity metadata when `~/.claude.json` is present
- sets `user-agent`, `x-app`, `x-client-request-id`, `x-claude-code-session-id`

Untouched: your messages, tools, tool results, thinking/effort settings,
`max_tokens`, prompt-cache breakpoints, and every non-Anthropic provider.

## How it works

The checksum covers the exact bytes on the wire, so the component that computes
it must also be the component that serializes them. The plugin therefore wraps
the **httpx transport** rather than rewriting kwargs higher up:

```
hermes agent → anthropic SDK → [hermes-black transport] → api.anthropic.com
```

One interception point, and it owns the final bytes.

Python's `json.dumps` does not match JavaScript's `JSON.stringify` (spacing,
`\uXXXX` escaping, and `1.0` vs `1`), so `js_json_dumps()` reproduces JS
semantics exactly. Getting this wrong produces a valid-looking but wrong
checksum.

## Verify it yourself

```sh
python3 -m unittest discover -s tests -v
```

23 tests. No network, no credentials, no Hermes required. `tests/vectors.json`
holds 40 golden vectors generated by running pi-black's TypeScript, so any
drift from the reference fails.

## Performance

~3.3 ms per request in pure Python (~0.7 ms with the optional `xxhash` module,
used only after a runtime parity self-check). Against a 1–3 s model call that
is well under 1% overhead.

## Security

Read [SECURITY.md](SECURITY.md). Short version: no credential access, no writes
to `~/.claude*`, no telemetry, no dependencies, one patched symbol, and
non-OAuth traffic is untouched.

## Known limitations

- Wraps private Anthropic SDK internals (`_client._transport`). An SDK rename
  would stop it applying — loudly, via HTTP 400, not silently.
- Hermes itself rewrites `"Hermes Agent"` → `"Claude Code"` in its own system
  prompt on the OAuth path. That happens upstream of this plugin.
- The Claude Code version is auto-detected, defaulting to 2.1.224. The
  convention is version-specific and needs revalidation over time.

## Status

Unofficial. Not affiliated with or endorsed by Anthropic, Nous Research, or the
Hermes or pi projects. See [SECURITY.md](SECURITY.md#terms).

## Credits

- [pi-black](https://github.com/paoloanzn/pi-black) by paoloanzn — the original
  and the reference this port is verified against
- [pi](https://github.com/earendil-works/pi-mono) by Mario Zechner
