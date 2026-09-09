# Auth-Hermes-Cloudflare ☁️ Hermes Plugin

> **Cloudflare AI model-provider plugin for Hermes Agent - pure Python provider + Rust core.**
> **Live account-aware catalog discovery, OpenAI-compatible inference, 22-model fallback.**

`auth-hermes-cloudflare` registers the `auth-cloudflare-ai` provider (display
name **Cloudflare AI**) against Cloudflare's OpenAI-compatible Workers AI
surface. The account ID is injected from the environment, the catalog is
fetched from `/ai/models/search` in the OpenRouter format, and the provider
shows up in `hermes model` with zero `custom_providers` wiring. **All
endpoint/auth logic lives in the Rust core; Python is a thin provider.**

[![plugin](https://img.shields.io/static/v1?label=plugin&message=v0.0.1&color=purple)](plugin.yaml)
[![hermes](https://img.shields.io/static/v1?label=hermes&message=%E2%89%A50.16.0&color=blue)](https://github.com/NousResearch/hermes-agent)
[![license](https://img.shields.io/static/v1?label=license&message=CC0-1.0&color=lightgrey)](https://github.com/PlayForm/Cloudflare/blob/Current/LICENSE)

---

## Install ⚡

### One-command

```bash
git clone https://github.com/PlayForm/Hermes-Cloudflare.git
ln -s "$(pwd)/Hermes-Cloudflare" ~/.hermes/plugins/auth-hermes-cloudflare
hermes plugins enable auth-hermes-cloudflare
hermes
```

Then export the two env vars the provider reads:

```bash
export CLOUDFLARE_ACCOUNT_ID="<your account id>"   # Workers & Pages → Overview
export CLOUDFLARE_API_TOKEN="<scoped token>"       # Account → Cloudflare AI → Edit
hermes model                                       # pick: Cloudflare AI
```

The provider path is **pure Python** - no Rust toolchain, no binary download,
no compiled dependencies. The optional `download.sh` flow (prebuilt Rust
dylib into `binaries/`) is only needed for the hook/tool integration, not for
using Cloudflare AI in Hermes.

### What changes after install

After installing and launching Hermes once:

```
~/.hermes/
├── plugins/
│   └── auth-hermes-cloudflare → /path/to/Hermes-Cloudflare    ← symlink to this repo
└── profiles/<name>/
    └── plugins/
        └── auth-hermes-cloudflare → ~/.hermes/plugins/auth-hermes-cloudflare
```

The plugin also adds to your Hermes config:

```yaml
# Added automatically on enable
plugins:
  enabled:
    - auth-hermes-cloudflare

# Provider block (manual or via the setup wizard)
providers:
  cloudflare:
    api_key_env: CLOUDFLARE_API_TOKEN
    base_url: https://api.cloudflare.com/client/v4/accounts/${CLOUDFLARE_ACCOUNT_ID}/ai/v1
    api_mode: chat_completions
```

No proxy processes are launched - the provider runs in-process.

### Verify it's working

```bash
# In a Hermes session:
hermes model          # pick: Cloudflare AI

# Or via CLI - token health check:
curl https://api.cloudflare.com/client/v4/user/tokens/verify \
  -H "Authorization: Bearer $CLOUDFLARE_API_TOKEN"
# → {"success":true,"result":{"id":"...","status":"active",...}}
```

### Clean uninstall

```bash
hermes plugins disable auth-hermes-cloudflare
rm ~/.hermes/plugins/auth-hermes-cloudflare
```

---

## Architecture 🏗️

```
Python (provider registration)              Rust core (single source of truth)
  __init__.py       233L                     auth-cloudflare (cdylib + rlib)
    register_provider(cloudflare)             ├─ auth:    account/token → endpoints
    lazy base_url / models_url                ├─ catalog: ModelRecord, ModelRole, CapabilityState
    fetch_models() (OpenRouter fallback)      └─ cache:   account-scoped cache slug
                                             auth-hermes-cloudflare (cdylib + rlib)
                                             └─ re-exports core types for hooks/tools
```

The Rust core owns the canonical endpoint/auth/catalog logic; the Python
provider mirrors it in-process so the picker and wizard work with or without
the dylib. URLs are computed lazily from `os.environ` at access time, because
plugin discovery runs before the profile `.env` is loaded.

---

## Provider 🛠️

| Item | Value |
| :--- | :---- |
| Provider name | `auth-cloudflare-ai` |
| Aliases | `cloudflare`, `cloudflare-ai`, `auth-cloudflare-workers-ai`, `cloudflare-workers-ai`, `workers-ai`, `cf-workers-ai`, `cf` |
| Display name | `Cloudflare AI` |
| API mode | `chat_completions` |
| Auth type | `api_key` |
| Signup | [dash.cloudflare.com/profile/api-tokens](https://dash.cloudflare.com/profile/api-tokens) |
| Health check | disabled - Cloudflare has no `/models` endpoint; token verify is used instead |
| Fixed base URL | yes - derived from the account ID, the wizard never prompts |
| Default model | `@cf/deepseek-ai/deepseek-v4-flash-0731` |
| Fallback models | 22 curated chat models compiled into the profile |

---

## Configuration ⚙️

Two environment variables drive everything. The `AUTH_CLOUDFLARE_*` names
are canonical; the `CLOUDFLARE_*` names are the legacy Hermes-compatible
aliases - the Rust core owns the resolution precedence.

| Variable | Role |
| :------- | :--- |
| `CLOUDFLARE_ACCOUNT_ID` / `AUTH_CLOUDFLARE_ACCOUNT_ID` | account ID - operational metadata, not a secret |
| `CLOUDFLARE_API_TOKEN` / `AUTH_CLOUDFLARE_API_TOKEN` | API token - a secret, only ever sent as a `Bearer` header |

```bash
export CLOUDFLARE_ACCOUNT_ID="<your account id>"
export CLOUDFLARE_API_TOKEN="<scoped token>"
```

---

## Dev Install (Rust source)

```bash
git clone https://github.com/PlayForm/Cloudflare.git
cd Cloudflare
cargo build -p auth-cloudflare -p auth-hermes-cloudflare
# Dylibs at target/debug/libauth_cloudflare.dylib and libauth_hermes_cloudflare.dylib
```

---

## Files

```
Hermes-Cloudflare/
├── __init__.py          ← 233-line Python provider (register_provider, lazy URLs)
├── plugin.yaml          ← model-provider manifest (env vars, min Hermes version)
├── download.sh          ← Prebuilt binary downloader (optional dylib flow)
├── BINARY_VERSION       ← Expected binary version
├── PROTOCOL_VERSION     ← dylib protocol version
├── README.md            ← This file
└── .gitignore
```