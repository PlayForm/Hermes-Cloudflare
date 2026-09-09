"""Token redaction tests (feedback 05/06, binding).

Contract under test: the API token never appears in ``repr``/``str`` of the
profile or in the error/log path of catalog fetching. The token's only
intentional exposure is the ``Authorization`` header sent to Cloudflare.

All tokens used here are synthetic (``cfut_test_...``) - never real
credentials.
"""

from __future__ import annotations

import importlib
import logging
import os
import unittest
import urllib.error

from helpers import load_plugin

plugin = load_plugin()

ACCOUNT = "testacct0001"
TOKEN = "cfut_test_" + "x" * 32  # synthetic, never a real credential


class _FakeResponse:
	def __init__(self, payload: bytes):
		self._payload = payload

	def read(self) -> bytes:
		return self._payload

	def __enter__(self):
		return self

	def __exit__(self, *exc):
		return False


class TokenRedactionTest(unittest.TestCase):
	def setUp(self):
		self._saved_env = dict(os.environ)
		os.environ[plugin.AUTH_ACCOUNT_ENV] = ACCOUNT
		os.environ[plugin.AUTH_TOKEN_ENV] = TOKEN
		self._urllib_security = importlib.import_module("hermes_cli.urllib_security")
		self._orig_open = self._urllib_security.open_credentialed_url

	def tearDown(self):
		self._urllib_security.open_credentialed_url = self._orig_open
		os.environ.clear()
		os.environ.update(self._saved_env)

	def _fresh_profile(self):
		# A standalone instance with a unique name so nothing from the
		# registered module-level profile interferes with the assertions.
		return plugin.CloudflareProfile(
			name="auth-cloudflare-workers-ai-redaction-test",
			display_name="Auth Cloudflare Workers AI",
			env_vars=(plugin.TOKEN_ENV, plugin.ACCOUNT_ENV),
		)

	def test_repr_hides_token(self):
		profile = self._fresh_profile()
		self.assertNotIn(TOKEN, repr(profile))

	def test_str_hides_token(self):
		profile = self._fresh_profile()
		self.assertNotIn(TOKEN, str(profile))

	def test_module_profile_repr_hides_token(self):
		# The registered module-level instance must also never expose it.
		self.assertNotIn(TOKEN, repr(plugin.cloudflare))
		self.assertNotIn(TOKEN, str(plugin.cloudflare))

	def test_no_profile_attribute_holds_token(self):
		profile = self._fresh_profile()
		for field_name, value in vars(profile).items():
			if isinstance(value, str):
				self.assertNotIn(TOKEN, value, f"field {field_name} leaked the token")

	def test_token_env_var_is_not_in_profile_repr(self):
		# The canonical env var NAME may appear (setup guidance) but never the
		# value. Assert the literal value is absent from every printable form.
		profile = self._fresh_profile()
		rendered = repr(profile) + str(profile)
		self.assertNotIn(TOKEN, rendered)

	def test_fetch_error_path_logs_no_token(self):
		# Realistic failure: an HTTP error whose message contains only the
		# catalog URL - never the header. Capture the plugin logger and prove
		# the synthetic token does not reach any formatted record.
		records = []
		handler = logging.Handler()
		handler.emit = lambda record: records.append(record)
		logger = logging.getLogger("providers.base")
		logger.addHandler(handler)
		old_level = logger.level
		logger.setLevel(logging.DEBUG)

		def fake_open(request, *, timeout=8.0, **kwargs):
			raise urllib.error.HTTPError(
				request.full_url, 401, "Unauthorized", {}, None
			)

		self._urllib_security.open_credentialed_url = fake_open
		try:
			result = plugin.cloudflare.fetch_models()
		finally:
			logger.removeHandler(handler)
			logger.setLevel(old_level)
		self.assertIsNone(result)
		for record in records:
			formatted = record.getMessage()
			self.assertNotIn(TOKEN, formatted)
			self.assertNotIn("Bearer " + TOKEN, formatted)

	def test_http_error_repr_does_not_contain_token(self):
		# The only place the token travels is the Authorization header, so an
		# error raised from the request object must be token-free.
		req = plugin.urllib.request.Request(plugin.catalog_url())
		req.add_header("Authorization", f"Bearer {TOKEN}")
		exc = urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, None)
		self.assertNotIn(TOKEN, repr(exc))
		self.assertNotIn(TOKEN, str(exc))

	def test_request_repr_does_not_contain_token(self):
		req = plugin.urllib.request.Request(plugin.catalog_url())
		req.add_header("Authorization", f"Bearer {TOKEN}")
		self.assertNotIn(TOKEN, repr(req))

	def test_api_token_accessor_is_the_intentional_exposure(self):
		# The accessor must return the configured value - it feeds the Bearer
		# header. This is the single sanctioned path; everything printable
		# around the profile stays clean.
		self.assertEqual(plugin.api_token(), TOKEN)

	def test_fetch_success_keeps_token_out_of_result(self):
		# Even on success the returned model list is token-free.
		def fake_open(request, *, timeout=8.0, **kwargs):
			return _FakeResponse(
				b'{"data": [{"id": "@cf/deepseek-ai/deepseek-v4-flash-0731"}]}'
			)

		self._urllib_security.open_credentialed_url = fake_open
		result = plugin.cloudflare.fetch_models()
		self.assertEqual(result, ["@cf/deepseek-ai/deepseek-v4-flash-0731"])
		for model_id in result:
			self.assertNotIn(TOKEN, model_id)


if __name__ == "__main__":
	unittest.main(verbosity=2)
