"""auth-hermes-cloudflare - Cloudflare Workers AI model-provider plugin for Hermes Agent.

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

This provider is registered as ``auth-cloudflare-workers-ai`` with the
display name **Auth Cloudflare Workers AI**. The Python-side direct catalog
discovery in ``fetch_models`` is TEMPORARY: when the ``auth-cloudflare``
executable is present and compatible it takes precedence through the JSON
CLI protocol, and the direct HTTP fetch must be removed once that bridge is
stable (``TODO(auth-hermes-cloudflare#1)``).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import urllib.request
from pathlib import Path

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

# Executable bridge (feedback 04/05/06): the auth-cloudflare CLI owns catalog
# and policy; the plugin only discovers it, handshakes, and consumes JSON.
# Discovery NEVER downloads at import - downloading is the installer's job
# (download.sh) and happens only when Hermes activates the provider.
BINARY_NAME = "auth-cloudflare"
_PLUGIN_DIR = Path(__file__).resolve().parent


def _read_protocol_version() -> int:
    """The JSON CLI protocol version from the sibling PROTOCOL_VERSION file.

    Guarded: any read/parse failure falls back to 1 so plugin import never
    breaks on a missing or malformed file.
    """
    try:
        raw = (_PLUGIN_DIR / "PROTOCOL_VERSION").read_text(encoding="utf-8").strip()
        if raw:
            return int(raw)
    except Exception:
        pass
    return 1


PROTOCOL_VERSION = _read_protocol_version()

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


def runtime_cache_binary_path() -> Path:
    """Plugin-local runtime cache: where download.sh installs the binary."""
    return _PLUGIN_DIR / "binaries" / BINARY_NAME


def locate_auth_cloudflare_binary() -> str | None:
    """Locate the auth-cloudflare executable; NEVER downloads at import.

    Order: ``AUTH_CLOUDFLARE_BIN`` env > ``auth-cloudflare`` on PATH >
    ``~/.hermes/bin/auth-cloudflare`` > plugin ``bin/`` > plugin runtime
    cache (``binaries/``). Downloading belongs to the installer
    (``download.sh``) and only happens when Hermes activates the provider
    or the user runs setup - never during module import.
    """
    explicit = os.getenv("AUTH_CLOUDFLARE_BIN", "").strip()
    if explicit:
        candidate = Path(explicit).expanduser()
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    on_path = shutil.which(BINARY_NAME)
    if on_path:
        return on_path
    for candidate in (
        Path.home() / ".hermes" / "bin" / BINARY_NAME,
        _PLUGIN_DIR / "bin" / BINARY_NAME,
        runtime_cache_binary_path(),
    ):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def _valid_semver(value: object) -> bool:
    """True when *value* is a semver-shaped ``x.y.z`` string (optional prerelease)."""
    return isinstance(value, str) and bool(
        re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:[-.][0-9A-Za-z.]+)?", value.strip())
    )


def check_binary_compatibility(bin_path: str) -> tuple[bool, str]:
    """Version handshake: run ``auth-cloudflare version --format json``.

    Validates executable presence, JSON validity, ``name == BINARY_NAME``,
    ``protocol_version >= PROTOCOL_VERSION`` and a parseable
    ``package_version``. Returns ``(ok, detail)``; ``detail`` is actionable
    and token-free - stderr is NEVER echoed verbatim because a misconfigured
    binary could print secrets to it (only the JSON parse error and the
    command name are surfaced).
    """
    try:
        proc = subprocess.run(
            [bin_path, "version", "--format", "json"],
            capture_output=True,
            text=True,
            timeout=5.0,
        )
    except FileNotFoundError:
        return False, f"auth-cloudflare binary not found at {bin_path!r}"
    except subprocess.TimeoutExpired:
        return False, "auth-cloudflare version --format json timed out after 5s"
    except OSError as exc:
        return False, f"auth-cloudflare binary could not be executed: {exc}"
    if proc.returncode != 0:
        return False, (
            f"auth-cloudflare version exited with code {proc.returncode} - run "
            "`auth-hermes-cloudflare install --upgrade` or "
            "`cargo install auth-cloudflare --locked --force`"
        )
    try:
        info = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError as exc:
        return False, (
            f"auth-cloudflare version returned invalid JSON ({exc}); expected "
            "`version --format json` output"
        )
    if not isinstance(info, dict):
        return False, "auth-cloudflare version JSON is not an object"
    name = info.get("name") or info.get("binary")
    if name != BINARY_NAME:
        return False, f"unexpected binary name {name!r}; expected {BINARY_NAME!r}"
    try:
        protocol = int(info.get("protocol_version", 0))
    except (TypeError, ValueError):
        return False, (
            f"auth-cloudflare protocol_version {info.get('protocol_version')!r} "
            "is not an integer"
        )
    if protocol < PROTOCOL_VERSION:
        return False, (
            f"auth-cloudflare protocol {protocol} is older than the required "
            f"{PROTOCOL_VERSION} - run `auth-hermes-cloudflare install --upgrade`"
        )
    package_version = info.get("package_version")
    if not _valid_semver(package_version):
        return False, (
            f"auth-cloudflare package_version {package_version!r} is not valid semver"
        )
    return True, f"auth-cloudflare {package_version} (protocol {protocol}) compatible"


def _fetch_models_via_binary(bin_path: str, timeout: float = 15.0) -> list[str] | None:
    """``auth-cloudflare catalog get --format json`` -> eligible model ids.

    The executable is the single source of truth for policy: only
    ``primary_agent_eligible`` models are returned, ``hidden`` status is
    excluded, and ordering is recommended first, experimental after (stable
    within a group). Returns None on any failure so fetch_models falls back
    to the direct HTTP catalog.
    """
    try:
        proc = subprocess.run(
            [bin_path, "catalog", "get", "--format", "json"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        from providers.base import logger

        logger.debug(
            "fetch_models(%s): auth-cloudflare catalog get: %s", BINARY_NAME, exc
        )
        return None
    if proc.returncode != 0:
        return None
    try:
        data = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        return None
    models = data.get("models") if isinstance(data, dict) else data
    if not isinstance(models, list):
        return None
    status_rank = {"recommended": 0, "experimental": 1}
    eligible: list[tuple[int, int, str]] = []
    for index, item in enumerate(models):
        if not isinstance(item, dict):
            continue
        mid = item.get("id")
        if not isinstance(mid, str) or not mid:
            continue
        if not item.get("primary_agent_eligible"):
            continue
        if item.get("status") == "hidden":
            continue
        status = item.get("status")
        rank = status_rank.get(status, 2) if isinstance(status, str) else 2
        eligible.append((rank, index, mid))
    eligible.sort()
    return [mid for _, _, mid in eligible] or None


class CloudflareProfile(ProviderProfile):
    """Cloudflare Workers AI profile with LAZY, account-aware URLs.

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
        """Live catalog: auth-cloudflare binary first, direct HTTP fallback.

        When the auth-cloudflare executable is present and compatible, the
        catalog comes from ``catalog get --format json`` (policy-ordered,
        primary-agent-eligible ids only). The direct in-process fetch of
        ``/ai/models/search`` stays as the verified default and the fallback
        on any binary error; it is temporary and must be removed once the
        executable bridge is stable (TODO below).
        """
        bin_path = locate_auth_cloudflare_binary()
        if bin_path is not None:
            ok, detail = check_binary_compatibility(bin_path)
            if ok:
                models = _fetch_models_via_binary(bin_path, timeout=15.0)
                if models is not None:
                    return models
                from providers.base import logger

                logger.debug(
                    "fetch_models(%s): binary catalog failed; direct HTTP fallback",
                    self.name,
                )
            else:
                from providers.base import logger

                logger.debug(
                    "fetch_models(%s): binary incompatible - %s", self.name, detail
                )
        # TODO(auth-hermes-cloudflare#1): remove direct Python catalog fetching
        # after the auth-cloudflare executable bridge is stable.
        return self._fetch_models_direct(api_key=api_key, timeout=timeout)

    def _fetch_models_direct(
        self,
        *,
        api_key: str | None = None,
        timeout: float = 8.0,
    ) -> list[str] | None:
        """Direct HTTP ``/ai/models/search`` (OpenRouter format) - TEMPORARY.

        The base implementation probes ``{base_url}/models`` which does not
        exist on Cloudflare; this override hits the real search endpoint and
        filters to chat-capable ``@cf/`` models. Returns None on any failure
        so callers fall back to ``fallback_models``. The Rust core owns the
        canonical catalog logic; this is a thin in-process fallback that
        ``TODO(auth-hermes-cloudflare#1)`` removes after the executable
        bridge is stable.
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
    name="auth-cloudflare-workers-ai",
    aliases=(
        "auth-cloudflare",
        "cloudflare",
        "cloudflare-workers-ai",
        "workers-ai",
        "cf-workers-ai",
        "cf",
        "cloudflare-ai",
    ),
    display_name="Auth Cloudflare Workers AI",
    description=(
        "Auth Cloudflare Workers AI - direct OpenAI-compatible access to "
        "Cloudflare-hosted Workers AI models, with account-aware discovery"
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
