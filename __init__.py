"""cloudflare - Cloudflare Workers AI model-provider plugin for Hermes Agent.

Registers the ``cloudflare`` provider against Cloudflare's OpenAI-compatible
Workers AI surface:

- Inference:  ``POST /client/v4/accounts/<ACCOUNT_ID>/ai/v1/chat/completions``
- Catalog:    ``GET  /client/v4/accounts/<ACCOUNT_ID>/ai/models/search``
              (OpenRouter-compatible format; NOT OpenAI's ``/models``)
- Verify:     ``GET  /client/v4/user/tokens/verify``

The account ID is injected from the environment (``CLOUDFLARE_ACCOUNT_ID``,
loaded from the profile .env before plugin discovery); the API token
(``CLOUDFLARE_API_TOKEN``) is a secret and is only ever sent as a Bearer
header - never logged, never echoed.
"""

from __future__ import annotations

import os

from providers import register_provider
from providers.base import ProviderProfile

API_BASE = "https://api.cloudflare.com/client/v4"
TOKEN_ENV = "CLOUDFLARE_API_TOKEN"
ACCOUNT_ENV = "CLOUDFLARE_ACCOUNT_ID"
DEFAULT_MODEL = "@cf/zai-org/glm-5.3-flash"

# Account-verified 27 Workers AI chat models - safety/classifier models
# (llama-guard-3-8b) and non-chat modalities (embedding, image, audio, video)
# are excluded from the primary picker. Order = recommended coding order.
FALLBACK_MODELS: tuple[str, ...] = (
    "@cf/zai-org/glm-5.3-flash",
    "@cf/moonshotai/kimi-k2.7-code",
    "@cf/deepseek-ai/deepseek-v4-flash-0731",
    "@cf/deepseek-ai/deepseek-v4-pro-0813",
    "@cf/zai-org/glm-5.3",
    "@cf/openai/gpt-oss-120b",
    "@cf/openai/gpt-oss-20b",
    "@cf/qwen/qwen3.8-27b",
    "@cf/qwen/qwen3-30b-a3b-fp8",
    "@cf/qwen/qwen2.5-coder-32b-instruct",
    "@cf/meta/llama-4-scout-17b-16e-instruct",
    "@cf/meta/llama-3.3-70b-instruct-fp8-fast",
    "@cf/mistralai/mistral-small-3.1-24b-instruct",
    "@cf/nvidia/nemotron-3-120b-a12b",
    "@cf/ibm-granite/granite-4.0-h-micro",
    "@cf/zai-org/glm-5.2",
    "@cf/zai-org/glm-4.7-flash",
    "@cf/moonshotai/kimi-k2.6",
    "@cf/deepseek-ai/deepseek-r1-distill-qwen-32b",
    "@cf/meta/llama-3.1-8b-instruct-fp8",
    "@cf/meta/llama-3.2-1b-instruct",
    "@cf/meta/llama-3.2-3b-instruct",
    "@cf/meta/llama-3.2-11b-vision-instruct",
    "@cf/qwen/qwq-32b",
)


def account_id() -> str | None:
    """The configured account id, or None."""
    value = os.getenv(ACCOUNT_ENV, "").strip()
    return value or None


def base_url() -> str:
    """OpenAI-compatible inference base URL for the configured account."""
    return f"{API_BASE}/accounts/{account_id() or '<ACCOUNT_ID>'}/ai/v1"


def _models_url() -> str | None:
    """Catalog endpoint; None when no account is configured."""
    aid = account_id()
    if not aid:
        return None
    return f"{API_BASE}/accounts/{aid}/ai/models/search?format=openrouter&per_page=1000"


# Module-level instance + registration - the exact contract every bundled
# provider follows (import side effect: profile joins the registry, so
# list_providers()/the model picker see it immediately). URLs are built at
# import from CLOUDFLARE_ACCOUNT_ID; the <ACCOUNT_ID> placeholder fallback
# keeps base_url non-empty and actionable when the env var is unset.
cloudflare = ProviderProfile(
    name="cloudflare",
    aliases=("cloudflare-workers-ai", "workers-ai", "cf"),
    display_name="Cloudflare Workers AI",
    description=(
        "Cloudflare Workers AI - direct OpenAI-compatible inference with live, "
        "account-aware catalog discovery"
    ),
    signup_url="https://dash.cloudflare.com/profile/api-tokens",
    env_vars=(TOKEN_ENV, ACCOUNT_ENV),
    base_url=base_url(),
    models_url=_models_url() or "",
    api_mode="chat_completions",
    auth_type="api_key",
    default_aux_model=DEFAULT_MODEL,
    fallback_models=FALLBACK_MODELS,
    # Cloudflare's /ai/v1 has no /models endpoint; health probing is disabled
    # and the prebuilt catalog is authoritative.
    supports_health_check=False,
)

register_provider(cloudflare)
