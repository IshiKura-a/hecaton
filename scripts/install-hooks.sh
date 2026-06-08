#!/usr/bin/env bash
#
# Point git at scripts/git-hooks/ so the pre-commit hook (and any
# future hooks) live in the repo. One-time install per clone.
#
#   bash scripts/install-hooks.sh

set -euo pipefail

repo_root="$(git rev-parse --show-toplevel)"
hooks_dir="$repo_root/scripts/git-hooks"

chmod +x "$hooks_dir"/* 2>/dev/null || true
git -C "$repo_root" config core.hooksPath "scripts/git-hooks"

echo "hooks installed:"
ls -1 "$hooks_dir"
