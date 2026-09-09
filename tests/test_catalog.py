"""Catalog parsing, filtering, and fallback tests (feedback 05/06, binding).

Contract under test:

- ``/ai/models/search`` returns an OpenRouter-compatible
  ``{"data": [...]}`` payload; the plugin parses it and keeps only chat-style
  ``@cf/...`` IDs.
- Llama Guard (safety classifier) is excluded from the primary picker.
- API errors (401/403/429 envelopes and transport failures) return None so
  callers fall back to ``FALLBACK_MODELS`` - never an exception.
- DeepSeek V4 Flash is the sole default; GLM-5.3 Flash stays available but
  non-default.

No network: ``hermes_cli.urllib_security.open_credentialed_url`` is swapped
for an in-process fake serving the fixture payloads.
"""

from __future__ import annotations

import importlib
import json
import os
import unittest
import urllib.error
from unittest import mock

from helpers import fixture_path, load_plugin

plugin = load_plugin()

ACCOUNT = "testacct0001"
SYNTHETIC_TOKEN = "cfut_test_catalog_only"  # never a real credential
DEEPSEEK = "@cf/deepseek-ai/deepseek-v4-flash-0731"
GLM_FLASH = "@cf/zai-org/glm-5.3-flash"
KIMI_CODE = "@cf/moonshotai/kimi-k2.7-code"
LLAMA_GUARD = "@cf/meta/llama-guard-3-8b"


class _FakeResponse:
    """Minimal file-like context manager standing in for a urlopen result."""

    def __init__(self, payload: bytes):
        self._payload = payload

    def read(self) -> bytes:
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class CatalogTest(unittest.TestCase):
    def setUp(self):
        self._saved_env = dict(os.environ)
        os.environ[plugin.AUTH_ACCOUNT_ENV] = ACCOUNT
        os.environ[plugin.AUTH_TOKEN_ENV] = SYNTHETIC_TOKEN
        self.captured = {}
        self._urllib_security = importlib.import_module("hermes_cli.urllib_security")
        self._orig_open = self._urllib_security.open_credentialed_url
        self._payload = b"{}"
        self._exc = None
        # The auth-cloudflare binary is installed on dev machines; these
        # tests instrument the direct-HTTP fallback, so force the locator
        # to None (binary-first delegation has its own coverage in
        # test_binary_discovery.py).
        self._locator = mock.patch.object(
            plugin, "locate_auth_cloudflare_binary", return_value=None
        )
        self._locator.start()

        def fake_open(request, *, timeout=8.0, **kwargs):
            self.captured["url"] = request.full_url
            self.captured["headers"] = dict(request.headers)
            if self._exc is not None:
                raise self._exc
            return _FakeResponse(self._payload)

        self._urllib_security.open_credentialed_url = fake_open

    def tearDown(self):
        self._locator.stop()
        self._urllib_security.open_credentialed_url = self._orig_open
        os.environ.clear()
        os.environ.update(self._saved_env)

    def _serve_fixture(self, name: str) -> None:
        self._payload = fixture_path(name).read_bytes()

    def _serve_json(self, obj) -> None:
        self._payload = json.dumps(obj).encode("utf-8")

    def test_parses_openrouter_data_array(self):
        self._serve_fixture("workers_ai_catalog.json")
        result = plugin.cloudflare.fetch_models()
        self.assertIsInstance(result, list)
        self.assertIn(DEEPSEEK, result)
        self.assertIn(GLM_FLASH, result)
        self.assertIn(KIMI_CODE, result)

    def test_llama_guard_excluded_from_primary_picker(self):
        self._serve_fixture("workers_ai_catalog.json")
        result = plugin.cloudflare.fetch_models()
        self.assertNotIn(LLAMA_GUARD, result)

    def test_llama_guard_not_in_fallback_models(self):
        self.assertNotIn(LLAMA_GUARD, plugin.FALLBACK_MODELS)
        self.assertNotIn(LLAMA_GUARD, plugin.cloudflare.fallback_models)

    def test_guard_fragment_is_filtered(self):
        self.assertIn("guard", plugin._NON_CHAT_FRAGMENTS)

    def test_raw_list_response_accepted(self):
        # The endpoint can also return a bare array; both shapes must parse.
        self._serve_json(
            [
                {"id": DEEPSEEK},
                {"id": GLM_FLASH},
                {"id": LLAMA_GUARD},
            ]
        )
        result = plugin.cloudflare.fetch_models()
        self.assertEqual(result, [DEEPSEEK, GLM_FLASH])

    def test_non_cf_ids_filtered(self):
        data = json.loads(fixture_path("workers_ai_catalog.json").read_text())
        data["data"].append({"id": "@zai-org/glm-5.3-flash"})
        self._serve_json(data)
        result = plugin.cloudflare.fetch_models()
        self.assertNotIn("@zai-org/glm-5.3-flash", result)

    def test_embedding_model_filtered(self):
        data = json.loads(fixture_path("workers_ai_catalog.json").read_text())
        data["data"].append({"id": "@cf/openai/text-embedding-3-small"})
        self._serve_json(data)
        result = plugin.cloudflare.fetch_models()
        self.assertNotIn("@cf/openai/text-embedding-3-small", result)

    def test_error_envelope_401_returns_none(self):
        self._serve_fixture("catalog_401.json")
        self.assertIsNone(plugin.cloudflare.fetch_models())

    def test_error_envelope_403_returns_none(self):
        self._serve_fixture("catalog_403.json")
        self.assertIsNone(plugin.cloudflare.fetch_models())

    def test_error_envelope_429_returns_none(self):
        self._serve_fixture("catalog_429.json")
        self.assertIsNone(plugin.cloudflare.fetch_models())

    def test_network_error_returns_none(self):
        self._exc = urllib.error.URLError("connection refused")
        self.assertIsNone(plugin.cloudflare.fetch_models())

    def test_http_error_returns_none(self):
        self._exc = urllib.error.HTTPError(
            plugin.catalog_url(), 401, "Unauthorized", {}, None
        )
        self.assertIsNone(plugin.cloudflare.fetch_models())

    def test_authorization_bearer_header_sent(self):
        self._serve_fixture("workers_ai_catalog.json")
        plugin.cloudflare.fetch_models()
        self.assertEqual(
            self.captured["headers"].get("Authorization"),
            f"Bearer {SYNTHETIC_TOKEN}",
        )

    def test_catalog_request_url_exact(self):
        self._serve_fixture("workers_ai_catalog.json")
        plugin.cloudflare.fetch_models()
        self.assertEqual(self.captured["url"], plugin.catalog_url())

    def test_no_account_returns_none_without_request(self):
        os.environ.pop(plugin.AUTH_ACCOUNT_ENV, None)
        os.environ.pop(plugin.ACCOUNT_ENV, None)
        self.assertIsNone(plugin.cloudflare.fetch_models())
        self.assertEqual(self.captured, {})

    def test_missing_token_returns_none(self):
        os.environ.pop(plugin.AUTH_TOKEN_ENV, None)
        os.environ.pop(plugin.TOKEN_ENV, None)
        self.assertIsNone(plugin.cloudflare.fetch_models())
        self.assertEqual(self.captured, {})

    def test_deepseek_flash_is_the_only_default(self):
        self.assertEqual(plugin.DEFAULT_MODEL, DEEPSEEK)
        self.assertEqual(plugin.FALLBACK_MODELS[0], DEEPSEEK)
        self.assertEqual(plugin.cloudflare.default_aux_model, DEEPSEEK)

    def test_glm_flash_never_default(self):
        self.assertNotEqual(plugin.DEFAULT_MODEL, GLM_FLASH)
        self.assertNotEqual(plugin.FALLBACK_MODELS[0], GLM_FLASH)

    def test_fallback_list_survives_catalog_failure(self):
        self._serve_fixture("catalog_401.json")
        self.assertIsNone(plugin.cloudflare.fetch_models())
        # The fallback list remains available to the picker, default first.
        self.assertGreaterEqual(len(plugin.FALLBACK_MODELS), 1)
        self.assertEqual(plugin.cloudflare.fallback_models, plugin.FALLBACK_MODELS)
        self.assertEqual(plugin.cloudflare.fallback_models[0], DEEPSEEK)


if __name__ == "__main__":
    unittest.main(verbosity=2)
