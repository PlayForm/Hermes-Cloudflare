"""auth-hermes-cloudflare - Cloudflare AI model-provider plugin for Hermes Agent.

Registers the ``auth-cloudflare-workers-ai`` provider against Cloudflare's
OpenAI-compatible Workers AI surface:

- Inference:  ``POST /client/v4/accounts/<ACCOUNT_ID>/ai/v1/chat/completions``
- Catalog:    ``GET  /client/v4/accounts/<ACCOUNT_ID>/ai/models/search``
              (OpenRouter-compatible format; NOT OpenAI's ``/models``)
- Verify:     ``GET  /client/v4/user/tokens/verify``

The account ID is injected from the environment (``CLOUDFLARE_ACCOUNT_ID`` /
``AUTH_CLOUDFLARE_ACCOUNT_ID``); the API token (``CLOUDFLARE_API_TOKEN`` /
``AUTH_CLOUDFLARE_API_TOKEN``) is a secret and is only ever sent as a Bearer
header - never logged, never echoed.

The base URL is DERIVED from the account ID, so the provider declares
``fixed_base_url=True``: the Hermes setup wizard never asks the user for a
Base URL override (hermes-agent ``_model_flow_api_key_provider`` honors this
flag and reads the profile's live URL instead).
"""

from __future__ import annotations

import json
import os
import urllib.request

from providers import register_provider
from providers.base import ProviderProfile

API_BASE = "https://api.cloudflare.com/client/v4"
# Legacy Hermes-compatible env names (primary), Auth-Cloudflare canonical
# names (fallback) - the Rust core owns the resolution precedence.
TOKEN_ENV = "CLOUDFLARE_API_TOKEN"
ACCOUNT_ENV = "CLOUDFLARE_ACCOUNT_ID"
AUTH_TOKEN_ENV = "AUTH_CLOUDFLARE_API_TOKEN"
AUTH_ACCOUNT_ENV = "AUTH_CLOUDFLARE_ACCOUNT_ID"
DEFAULT_MODEL = "@cf/deepseek-ai/deepseek-v4-flash-0731"

# Account-verified Workers AI chat models - safety/classifier models
# (llama-guard-3-8b) and non-chat modalities (embedding, image, audio, video)
# are excluded from the primary picker. Order = recommended coding order.
# DeepSeek V4 Flash is the development default (feedback 01/02); GLM-5.3 Flash
# is experimental and stays out of the default position until conformance
# thresholds are met.
FALLBACK_MODELS: tuple[str, ...] = (
    "@cf/deepseek-ai/deepseek-v4-flash-0731",
    "@cf/moonshotai/kimi-k2.7-code",
    "@cf/deepseek-ai/deepseek-v4-pro-0813",
    "@cf/openai/gpt-oss-120b",
    "@cf/openai/gpt-oss-20b",
    "@cf/zai-org/glm-5.3",
    "@cf/qwen/qwen3.8-27b",
    "@cf/qwen/qwen3-30b-a3b-fp8",
    "@cf/qwen/qwen2.5-coder-32b-instruct",
    "@cf/meta/llama-4-scout-17b-16e-instruct",
    "@cf/meta/llama-3.3-70b-instruct-fp8-fast",
    "@cf/mistralai/mistral-small-3.1-24b-instruct",
    "@cf/nvidia/nemotron-3-120b-a12b",
    "@cf/ibm-granite/granite-4.0-h-micro",
    "@cf/zai-org/glm-4.7-flash",
    "@cf/moonshotai/kimi-k2.6",
    "@cf/deepseek-ai/deepseek-r1-distill-qwen-32b",
    "@cf/meta/llama-3.1-8b-instruct-fp8",
    "@cf/meta/llama-3.2-1b-instruct",
    "@cf/meta/llama-3.2-3b-instruct",
    "@cf/meta/llama-3.2-11b-vision-instruct",
    "@cf/qwen/qwq-32b",
)

# Non-chat modalities + safety classifiers filtered from the primary picker.
_NON_CHAT_FRAGMENTS = (
    "embed",
    "image",
    "audio",
    "video",
    "speech",
    "tts",
    "rerank",
    "guard",
    "classifier",
    "segment",
    "whisper",
    "translation",
    "m2m",
    "imagen",
    "flux",
    "stable-diffusion",
)


def _env(*names: str) -> str | None:
    """First non-empty env var among *names* (canonical then legacy order)."""
    for name in names:
        value = os.getenv(name, "").strip()
        if value:
            return value
    return None


def account_id() -> str | None:
    """The configured account id (AUTH_CLOUDFLARE_* then CLOUDFLARE_*), or None."""
    return _env(AUTH_ACCOUNT_ENV, ACCOUNT_ENV)


def api_token() -> str | None:
    """The configured API token (never printed), or None."""
    return _env(AUTH_TOKEN_ENV, TOKEN_ENV)


def inference_base_url() -> str:
    """OpenAI-compatible inference base URL for the configured account."""
    return f"{API_BASE}/accounts/{account_id() or '<ACCOUNT_ID>'}/ai/v1"


def catalog_url() -> str | None:
    """Catalog endpoint; None when no account is configured."""
    aid = account_id()
    if not aid:
        return None
    return f"{API_BASE}/accounts/{aid}/ai/models/search?format=openrouter&per_page=1000"


class CloudflareProfile(ProviderProfile):
    """Cloudflare AI profile with LAZY, account-aware URLs.

    Plugin discovery runs before the profile .env is loaded into os.environ,
    so eager ``base_url=compute()`` at import time bakes the ``<ACCOUNT_ID>``
    placeholder and every runtime read 404s. These properties compute from
    ``os.environ`` at ACCESS time; the absorbing setters swallow the parent
    dataclass ``__init__`` assignments.
    """

    @property
    def base_url(self) -> str:
        return inference_base_url()

    @base_url.setter
    def base_url(self, value: str) -> None:
        self._base_url_override = value

    @property
    def models_url(self) -> str:
        return catalog_url() or ""

    @models_url.setter
    def models_url(self, value: str) -> None:
        self._models_url_override = value

    def fetch_models(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float = 8.0,
    ) -> list[str] | None:
        """Live account-aware catalog: ``/ai/models/search`` (OpenRouter format).

        The base implementation probes ``{base_url}/models`` which does not
        exist on Cloudflare; this override hits the real search endpoint and
        filters to chat-capable ``@cf/`` models. Returns None on any failure
        so callers fall back to ``fallback_models``. The Rust core owns the
        canonical catalog logic; this is a thin in-process fallback for
        wizard/picker paths before the binary contract is wired in.
        """
        url = catalog_url()
        if not url:
            return None
        token = api_key or api_token()
        if not token:
            return None
        req = urllib.request.Request(url)
        req.add_header("Authorization", f"Bearer {token}")
        req.add_header("Accept", "application/json")
        try:
            from hermes_cli.urllib_security import open_credentialed_url
            from providers.base import _profile_user_agent

            req.add_header("User-Agent", _profile_user_agent())
            with open_credentialed_url(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode())
        except Exception as exc:  # network / auth / malformed payload
            from providers.base import logger

            logger.debug("fetch_models(%s): %s", self.name, exc)
            return None
        items = data if isinstance(data, list) else data.get("data", [])
        ids = [m.get("id") for m in items if isinstance(m, dict) and m.get("id")]
        chat = [
            mid
            for mid in ids
            if isinstance(mid, str)
            and mid.startswith("@cf/")
            and not any(frag in mid for frag in _NON_CHAT_FRAGMENTS)
        ]
        return chat or None


# Module-level instance + registration - the exact contract every bundled
# provider follows (import side effect: profile joins the registry, so
# list_providers()/the model picker see it immediately). URLs are LAZY (see
# CloudflareProfile) - the <ACCOUNT_ID> placeholder only appears in contexts
# that have not loaded the profile .env yet.
cloudflare = CloudflareProfile(
    name="auth-cloudflare-ai",
    aliases=(
        "cloudflare",
        "cloudflare-ai",
        "auth-cloudflare-workers-ai",
        "cloudflare-workers-ai",
        "workers-ai",
        "cf-workers-ai",
        "cf",
    ),
    display_name="Cloudflare AI",
    description=(
        "Cloudflare AI - direct OpenAI-compatible inference with live, "
        "account-aware catalog discovery"
    ),
    signup_url="https://dash.cloudflare.com/profile/api-tokens",
    env_vars=(TOKEN_ENV, ACCOUNT_ENV),
    api_mode="chat_completions",
    auth_type="api_key",
    default_aux_model=DEFAULT_MODEL,
    fallback_models=FALLBACK_MODELS,
    # Cloudflare's /ai/v1 has no /models endpoint; health probing is disabled
    # and the prebuilt catalog is authoritative. The base URL is derived from
    # the account ID, so the setup wizard must never prompt for an override.
    supports_health_check=False,
    fixed_base_url=True,
)

register_provider(cloudflare)
