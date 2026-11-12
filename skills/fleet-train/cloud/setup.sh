#!/usr/bin/env bash
# Cloud builder setup TEMPLATE (Ubuntu 24.04 sandbox). Copy to <repo>/tools/cloud/setup.sh and fill in the marked hook.
# Installs the system packages the workspace needs, writes /opt/harness/env.sh (sourced before every cargo call) and
# runs one smoke build. Prints SETUP OK, or SETUP FAILED and exits non-zero (the builder then claims nothing).
# Usage, from the repo root:  bash tools/cloud/setup.sh && source /opt/harness/env.sh
set -euo pipefail
SUDO=""; [ "$(id -u)" = 0 ] || SUDO="sudo"
O=/opt/harness
# Smoke build: the cheapest command that proves the toolchain and native deps work (override with SMOKE_CMD).
SMOKE_CMD=${SMOKE_CMD:-"cargo check -q --workspace"}

# ---------------------------------------------------------------------------------------------------------------
# PER-PROJECT HOOK: extra system deps. Install native libraries, compilers or SDKs the workspace links against, and
# append any variables its build.rs needs to $O/env.sh. Must be idempotent (a session may run setup twice).
# Example:
#   $SUDO apt-get install -y -qq libfoo-dev >/dev/null
#   echo "export FOO_DIR=/usr/lib/x86_64-linux-gnu" >> "$O/env.sh"
extra_system_deps() {
  :
}
# ---------------------------------------------------------------------------------------------------------------

$SUDO mkdir -p "$O"
$SUDO chown -R "$(id -u):$(id -g)" "$O"
if ! command -v cc >/dev/null 2>&1 || ! command -v pkg-config >/dev/null 2>&1; then
  $SUDO apt-get update -qq
  DEBIAN_FRONTEND=noninteractive $SUDO apt-get install -y -qq build-essential pkg-config >/dev/null
fi
command -v cargo >/dev/null 2>&1 || { curl -sSf https://sh.rustup.rs | sh -s -- -y -q --profile minimal -c clippy,rustfmt; }
cat > "$O/env.sh" <<ENV
export PATH=\$HOME/.cargo/bin:\$PATH
export CARGO_BUILD_JOBS=\${CARGO_BUILD_JOBS:-4}
ENV
extra_system_deps
# shellcheck disable=SC1091
source "$O/env.sh"

cd "$(git rev-parse --show-toplevel)"
log=$(mktemp)
if ! $SMOKE_CMD > "$log" 2>&1; then
  grep -E "^error" -A5 "$log" | head -30
  echo "SETUP FAILED: smoke build failed ($SMOKE_CMD)" >&2
  exit 1
fi
echo "SETUP OK. In every shell before cargo:"
echo "source $O/env.sh"
