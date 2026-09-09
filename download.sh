#!/usr/bin/env bash
# auth-hermes-cloudflare - download prebuilt auth-cloudflare binary from GitHub Releases
# Usage: bash download.sh [version] [target-triple]
#   version: auto-detected from BINARY_VERSION, then Cargo.toml (monorepo), then GitHub API
#   target:  auto-detected from uname -sm
#
# NOTE (v0.0.1 scaffold): release-asset checksum verification (SHA256SUMS),
# temp-extract + atomic install + binary self-validation land in the Phase 4
# rewrite per .hermes/STEP_ENGINE.md (feedback 04 contract). This version
# keeps the proven dylib flow working against the renamed paths.

set -euo pipefail

# Resolve relative to this script (never $PWD) so invocations from anywhere
# write binaries into plugins/auth-hermes-cloudflare/binaries/.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BINARY_DIR="${BINARY_DIR:-$SCRIPT_DIR/binaries}"
REPO="${REPO:-PlayForm/Cloudflare}"
BIN_VERSION="${1:-}"
TARGET="${2:-}"

VERSION_RE='^[0-9]+\.[0-9]+\.[0-9]+([-.][0-9A-Za-z.]+)?$'
validate_version() {
	local v="$1"
	case "$v" in
	'' | . | v)
		echo "ERROR: empty/invalid BINARY_VERSION '$v'"
		return 1
		;;
	esac
	if [[ ! "$v" =~ $VERSION_RE ]]; then
		echo "ERROR: BINARY_VERSION '$v' does not match the required pattern"
		echo "  expected: ^[0-9]+.[0-9]+.[0-9]+([-.][0-9A-Za-z.]+)?$"
		return 1
	fi
	return 0
}

if [[ -z "$BIN_VERSION" ]]; then
	# 1. BINARY_VERSION file - deployed with the plugin, always correct
	if [[ -f "$SCRIPT_DIR/BINARY_VERSION" ]]; then
		BIN_VERSION=$(head -1 "$SCRIPT_DIR/BINARY_VERSION" | tr -d '[:space:]')
	fi
fi
if [[ -z "$BIN_VERSION" ]]; then
	# 2. Cargo.toml - for developers with the full monorepo
	for f in "$SCRIPT_DIR/../../crates/auth-cloudflare/Cargo.toml" \
		"$SCRIPT_DIR/../../crates/auth-hermes-cloudflare/Cargo.toml"; do
		if [[ -f "$f" ]]; then
			BIN_VERSION=$(grep '^version' "$f" | head -1 | awk -F'"' '{print $2}')
			[[ -n "$BIN_VERSION" ]] && break
		fi
	done
fi
if [[ -z "$BIN_VERSION" ]]; then
	# 3. GitHub API - query latest release tag (needs network, but reliable)
	if command -v curl &>/dev/null; then
		BIN_VERSION=$(curl -fsS --connect-timeout 10 --max-time 30 \
			"https://api.github.com/repos/${REPO}/releases/latest" 2>/dev/null |
			grep '"tag_name":' | head -1 |
			sed 's/.*"tag_name": *"Cloudflare\/v\([^"]*\)".*/\1/')
	fi
fi
validate_version "$BIN_VERSION" || {
	echo "ERROR: no valid version found - pass one explicitly: bash download.sh <version>"
	exit 1
}

if [[ -z "$TARGET" ]]; then
	OS=$(uname -s)
	ARCH=$(uname -m)
	case "$ARCH" in
	arm64 | aarch64) ARCH="aarch64" ;;
	x86_64) ARCH="x86_64" ;;
	esac
	case "$OS" in
	Darwin) TARGET="${ARCH}-apple-darwin" ;;
	Linux) TARGET="${ARCH}-unknown-linux-gnu" ;;
	MINGW* | MSYS*) TARGET="${ARCH}-pc-windows-msvc" ;;
	*)
		echo "ERROR: unsupported OS '$OS' - pass target-triple explicitly"
		exit 1
		;;
	esac
fi

ASSET="auth-cloudflare-hermes-${TARGET}.tar.gz"
URL="https://github.com/${REPO}/releases/download/Cloudflare/v${BIN_VERSION}/${ASSET}"

echo "Downloading ${ASSET} (v${BIN_VERSION})"
mkdir -p "$BINARY_DIR"
TMP_FILE=$(mktemp)
trap 'rm -f "$TMP_FILE"' EXIT

curl -fL --connect-timeout 10 --max-time 300 -o "$TMP_FILE" "$URL" || {
	echo "ERROR: download failed - ${URL}"
	echo "  verify the release exists: https://github.com/${REPO}/releases/tag/Cloudflare/v${BIN_VERSION}"
	exit 1
}

tar -xzf "$TMP_FILE" -C "$BINARY_DIR"
DYLIB="$BINARY_DIR/libauth_cloudflare_hermes.dylib"
[[ "$(uname -s)" == "Linux" ]] && DYLIB="$BINARY_DIR/libauth_cloudflare_hermes.so"
[[ "$(uname -s)" == MINGW* || "$(uname -s)" == MSYS* ]] && DYLIB="$BINARY_DIR/auth_cloudflare_hermes.dll"
if [[ ! -f "$DYLIB" ]]; then
	echo "ERROR: archive did not contain the expected dylib"
	exit 1
fi
printf '%s\n' "$BIN_VERSION" >"$SCRIPT_DIR/BINARY_VERSION"
echo "OK - installed to $BINARY_DIR"
