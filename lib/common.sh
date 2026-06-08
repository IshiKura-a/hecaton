# Source this file from every script:
#     # shellcheck source=lib/common.sh
#     source "$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)/lib/common.sh"
#
# Or simply:
#     HECATON_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
#     source "$HECATON_ROOT/lib/common.sh"
#
# Provides:
#   $HECATON_ROOT  absolute path to the repo root
#   log / warn / die
#   load_env       loads .env from repo root, fails if missing
#   require_cmd    ensures a binary is on PATH
#   require_var    ensures a variable is set and non-empty

set -euo pipefail

# Resolve repo root once; callers can override before sourcing.
if [[ -z "${HECATON_ROOT:-}" ]]; then
  # This file lives at $HECATON_ROOT/lib/common.sh
  HECATON_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
fi
export HECATON_ROOT

# ---- logging --------------------------------------------------------------

_ts() { date '+%Y-%m-%dT%H:%M:%S%z'; }
log()  { printf '[%s] %s\n' "$(_ts)" "$*" >&2; }
warn() { printf '[%s] WARN: %s\n' "$(_ts)" "$*" >&2; }
die()  { printf '[%s] ERROR: %s\n' "$(_ts)" "$*" >&2; exit 1; }

# ---- env / preflight ------------------------------------------------------

load_env() {
  local env_file="${1:-$HECATON_ROOT/.env}"
  [[ -f "$env_file" ]] || die "missing $env_file (copy .env.example and fill in)"
  # shellcheck disable=SC1090
  set -a; source "$env_file"; set +a
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "required command not found: $1"
}

require_var() {
  local name="$1"
  [[ -n "${!name:-}" ]] || die "required env var is unset or empty: $name"
}
