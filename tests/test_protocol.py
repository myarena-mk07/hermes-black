"""Offline conformance tests. No network, no credentials, no Hermes required.

The golden vectors in vectors.json were produced by executing pi-black's
TypeScript implementation (v0.84.1-cc2.1.224.4). If Hermes Black ever drifts
from that reference by a single byte, these fail.

    python3 -m unittest discover -s tests -v
"""

import importlib.util
import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location("hb_protocol", os.path.join(ROOT, "protocol.py"))
P = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(P)

with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "vectors.json"), encoding="utf-8") as fh:
    V = json.load(fh)

VERSION = "2.1.224"


class TestXXHash(unittest.TestCase):
    def test_matches_pi_black(self):
        for case in V["xxhash"]:
            data = case["data"].encode("utf-8")
            seed = int(case["seed"])
            with self.subTest(len=len(data), seed=seed):
                self.assertEqual(format(P.xxhash64(data, seed), "x"), case["expect"])

    def test_pure_and_selected_agree(self):
        """Guards the optional C accelerator against silent divergence."""
        for case in V["xxhash"]:
            data, seed = case["data"].encode("utf-8"), int(case["seed"])
            self.assertEqual(P.xxhash64(data, seed), P.xxhash64_pure(data, seed))


class TestFingerprint(unittest.TestCase):
    def test_matches_pi_black(self):
        for case in V["fingerprint"]:
            with self.subTest(msgs=len(case["messages"])):
                self.assertEqual(P.version_fingerprint(case["messages"], VERSION), case["expect"])


class TestCch(unittest.TestCase):
    def test_matches_pi_black(self):
        for i, case in enumerate(V["cch"]):
            with self.subTest(i=i):
                self.assertEqual(P.patch_cch(P.js_json_dumps(case["body"])), case["expect"])

    def test_already_patched_is_untouched(self):
        once = P.patch_cch(P.js_json_dumps(V["cch"][0]["body"]))
        self.assertEqual(P.patch_cch(once), once)

    def test_rejects_missing_billing_block(self):
        bad = {"model": "m", "max_tokens": 1, "system": [{"type": "text", "text": "nope"}]}
        with self.assertRaises(ValueError):
            P.patch_cch(json.dumps(bad))

    def test_rejects_missing_model_or_max_tokens(self):
        body = json.loads(P.js_json_dumps(V["cch"][0]["body"]))
        body.pop("max_tokens")
        with self.assertRaises(ValueError):
            P.patch_cch(P.js_json_dumps(body))


class TestJsJson(unittest.TestCase):
    def test_compact_separators(self):
        self.assertEqual(P.js_json_dumps({"a": 1, "b": [1, 2]}), '{"a":1,"b":[1,2]}')

    def test_integral_floats_render_like_js(self):
        self.assertEqual(P.js_json_dumps({"t": 1.0}), '{"t":1}')
        self.assertEqual(P.js_json_dumps({"t": 0.7}), '{"t":0.7}')

    def test_unicode_is_raw(self):
        self.assertEqual(P.js_json_dumps({"t": "日本語"}), '{"t":"日本語"}')

    def test_booleans_not_coerced(self):
        self.assertEqual(P.js_json_dumps({"a": True, "b": False}), '{"a":true,"b":false}')


class TestEnvelope(unittest.TestCase):
    def _body(self, system):
        return {"model": "m", "max_tokens": 1,
                "messages": [{"role": "user", "content": "hi"}], "system": system}

    def test_strips_legacy_block(self):
        out = P.transform_body(
            self._body([{"type": "text", "text": P.LEGACY_HERMES_OAUTH_SYSTEM_PROMPT},
                        {"type": "text", "text": "HERMES PROMPT"}]), version=VERSION)
        texts = [b["text"] for b in out["system"]]
        self.assertNotIn(P.LEGACY_HERMES_OAUTH_SYSTEM_PROMPT, texts)
        self.assertTrue(texts[0].startswith(P.BILLING_PREFIX))
        self.assertEqual(texts[1], P.AGENT_SDK_SYSTEM_PROMPT)
        self.assertEqual(texts[2], "HERMES PROMPT")

    def test_accepts_string_system(self):
        out = P.transform_body(self._body("PLAIN STRING PROMPT"), version=VERSION)
        self.assertEqual(out["system"][2]["text"], "PLAIN STRING PROMPT")

    def test_idempotent(self):
        first = P.transform_body(self._body([{"type": "text", "text": "S"}]), version=VERSION)
        second = P.transform_body(json.loads(P.js_json_dumps(first)), version=VERSION)
        self.assertEqual(len(first["system"]), len(second["system"]))

    def test_preserves_cache_control(self):
        out = P.transform_body(
            self._body([{"type": "text", "text": "S", "cache_control": {"type": "ephemeral"}}]),
            version=VERSION)
        self.assertEqual(out["system"][2].get("cache_control"), {"type": "ephemeral"})

    def test_metadata_only_with_identity_and_session(self):
        ident = {"device_id": "a" * 64, "account_uuid": "11111111-2222-4333-8444-555555555555"}
        self.assertNotIn("metadata", P.transform_body(self._body([]), version=VERSION))
        out = P.transform_body(self._body([]), version=VERSION, session_id="s", identity=ident)
        self.assertIn("device_id", out["metadata"]["user_id"])


class TestToolNames(unittest.TestCase):
    def test_casing_normalised(self):
        self.assertEqual(P.to_claude_code_name("read"), "Read")
        self.assertEqual(P.to_claude_code_name("bash"), "Bash")

    def test_hermes_and_mcp_names_untouched(self):
        for name in ("read_file", "write_file", "patch", "mcp__github__create", "memory"):
            self.assertEqual(P.to_claude_code_name(name), name)


class TestIdentity(unittest.TestCase):
    GOOD = {"userID": "a" * 64,
            "oauthAccount": {"accountUuid": "11111111-2222-4333-8444-555555555555"}}

    def test_valid(self):
        self.assertEqual(P.parse_identity(self.GOOD)["device_id"], "a" * 64)

    def test_rejects_malformed(self):
        for bad in (None, {}, {"userID": "short", "oauthAccount": {"accountUuid": "x"}},
                    {"userID": "a" * 64, "oauthAccount": {"accountUuid": "not-a-uuid"}},
                    {"userID": "a" * 64}):
            self.assertIsNone(P.parse_identity(bad))

    def test_discovery_missing_file_is_none(self):
        self.assertIsNone(P.discover_identity(env={}, config_path="/nonexistent/.claude.json"))


class TestOAuthDetection(unittest.TestCase):
    def test_detects_oauth_tokens(self):
        self.assertTrue(P.is_oauth_token("Bearer sk-ant-oat01-abc"))
        self.assertTrue(P.is_oauth_token("sk-ant-oat01-abc"))

    def test_ignores_api_keys_and_empty(self):
        for v in ("sk-ant-api03-abc", "", None, "Bearer something-else"):
            self.assertFalse(P.is_oauth_token(v))


if __name__ == "__main__":
    unittest.main(verbosity=2)
