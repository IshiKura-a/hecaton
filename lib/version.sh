# Git + content-hash helpers for dev iteration.
#
# - git_sha / git_sha_short: current commit
# - git_dirty: '1' if the working tree has uncommitted changes, empty otherwise
# - hash_paths PATH ...: deterministic content hash of regular files under
#   the given paths, used to gate "has X changed since last deploy?"
#
# Source this file after lib/common.sh.

# shellcheck source=common.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"

# ---- git -----------------------------------------------------------------

git_sha() {
  ( cd "$HECATON_ROOT" && git rev-parse HEAD 2>/dev/null ) || echo "unknown"
}

git_sha_short() {
  ( cd "$HECATON_ROOT" && git rev-parse --short=12 HEAD 2>/dev/null ) || echo "unknown"
}

# Prints "1" if the working tree has uncommitted changes, empty otherwise.
git_dirty() {
  ( cd "$HECATON_ROOT" && \
    if ! git diff --quiet HEAD 2>/dev/null || \
       [[ -n "$(git status --porcelain 2>/dev/null)" ]]; then
      printf '1'
    fi )
}

# ---- content hash --------------------------------------------------------

# Resolve once: prefer sha256sum (Linux), fall back to BSD shasum (macOS).
if command -v sha256sum >/dev/null 2>&1; then
  _SHA256_CMD='sha256sum'
else
  _SHA256_CMD='shasum -a 256'
fi

_sha256() { $_SHA256_CMD; }

# hash_paths PATH [PATH ...]
#
# Deterministic 64-char hex of every regular file under the given paths
# (relative to $HECATON_ROOT). Used to detect "did the broker source
# change since last deploy?" without relying on git (so a dirty tree
# still gets a stable per-state hash).
#
# Skips __pycache__/, .ruff_cache/, and .pyc files — build artifacts.
hash_paths() {
  ( cd "$HECATON_ROOT" && \
    find "$@" \
      \( -type d \( -name __pycache__ -o -name .ruff_cache \) -prune \) \
      -o -type f ! -name '*.pyc' -print 2>/dev/null \
  ) | LC_ALL=C sort | (
    cd "$HECATON_ROOT" && \
    while IFS= read -r f; do
      [[ -n "$f" ]] && $_SHA256_CMD "$f"
    done
  ) | $_SHA256_CMD | awk '{print $1}'
}
