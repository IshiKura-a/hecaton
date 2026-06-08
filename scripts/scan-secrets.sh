#!/usr/bin/env bash
#
# Refuse to commit if any tracked or staged file contains values that
# look like Tailscale auth keys, fleet host names, or operator logins.
#
# Run before `git push`:
#   bash scripts/scan-secrets.sh
#
# Add to .git/hooks/pre-commit to make it automatic:
#   ln -s ../../scripts/scan-secrets.sh .git/hooks/pre-commit
#
# This is a guardrail, not a complete DLP solution. Anything obviously
# personal must already live in .env / config/ (gitignored).

set -euo pipefail

# Patterns to flag. Each must match a REAL value, not a placeholder or
# variable reference, to avoid false positives in docs/examples.
patterns=(
  'tskey-auth-[A-Za-z0-9]'    # Tailscale auth key (skips 'tskey-auth-...')
  'tskey-api-[A-Za-z0-9]'
  '@gmail\.com'
  'tail[0-9a-f]{6,}\.ts\.net' # tailnet suffix
  '@microsoft\.com'
  'tangzihao'
  'gcrazgdl[0-9]'             # actual host hostname (skips bare 'gcrazgdl')
  'GCRAZGDL[0-9]'
  '\.westus3\.cloudapp\.azure\.com'
  '^HECATON_TOKEN=[a-f0-9]{32}'
)

# This script literally lists each pattern, so skip it when scanning.
SELF_REL="scripts/scan-secrets.sh"

# Files git would actually push. We treat the union of:
#   (a) already-tracked files                    -> git ls-files
#   (b) staged-but-not-yet-committed files       -> git diff --cached --name-only
#   (c) untracked files that are NOT gitignored  -> git ls-files --others --exclude-standard
# Anything matching .gitignore is skipped automatically because (c)
# already excludes it.
files="$(
  { git ls-files
    git diff --cached --name-only
    git ls-files --others --exclude-standard
  } | sort -u | grep -v '^$' || true
)"

[[ -z "$files" ]] && { echo "scan-secrets: nothing to scan"; exit 0; }

hits=0
while IFS= read -r f; do
  [[ -f "$f" ]] || continue
  [[ "$f" == "$SELF_REL" ]] && continue
  for pat in "${patterns[@]}"; do
    if grep -nE "$pat" -- "$f" >/dev/null 2>&1; then
      echo "  hit  $f"
      grep -nE "$pat" -- "$f" | sed 's/^/       /'
      hits=$((hits + 1))
    fi
  done
done <<< "$files"

if (( hits > 0 )); then
  echo
  echo "scan-secrets: $hits leaked-looking pattern(s) found in files git would push."
  echo "fix or move into .env / config/ (both are gitignored)."
  exit 1
fi
echo "scan-secrets: clean"
