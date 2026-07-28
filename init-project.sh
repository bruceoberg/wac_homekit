#!/usr/bin/env bash
# init-project.sh — run once after cloning the template.
#
# Usage:
#   bash init-project.sh <package-name>
#
# <package-name> must be a valid Python identifier (letters, digits, underscores;
# no hyphens — use underscores instead, e.g. my_tool not my-tool).
# The GitHub repo can use hyphens; only the Python package name uses underscores.
#
# What it does:
#   1. Renames src/PKGNAME/ → src/<name>/
#   2. Replaces every occurrence of PKGNAME in tracked files with <name>
#   3. Deletes itself (it's only needed once)

set -euo pipefail

PKGNAME="${1:-}"
if [[ -z "$PKGNAME" ]]; then
  echo "Usage: bash init-project.sh <package-name>" >&2
  exit 1
fi

if [[ ! "$PKGNAME" =~ ^[A-Za-z][A-Za-z0-9_]*$ ]]; then
  echo "Error: '$PKGNAME' is not a valid Python identifier." >&2
  echo "Use underscores instead of hyphens (e.g. my_tool, not my-tool)." >&2
  exit 1
fi

echo "Initialising project as '$PKGNAME'..."

# Rename the package directory
mv "src/PKGNAME" "src/$PKGNAME"

# Replace PKGNAME in all tracked text files (safe across macOS/Linux sed)
git grep -l 'PKGNAME' | while read -r f; do
  # Skip this script itself — we'll remove it below
  [[ "$f" == "init-project.sh" ]] && continue
  sed -i.bak "s/PKGNAME/$PKGNAME/g" "$f"
  rm -f "${f}.bak"
done

# Stage the renames + edits so the repo is clean after init
git add -A

# Self-destruct
git rm -f init-project.sh

# Resolve devenv.yaml → devenv.lock
echo "Running devenv update..."
devenv update

# Whitelist the .envrc so direnv will activate the env on next cd.
# This doesn't require the hook to be active in the current shell —
# it just sets the trust so the next shell entry triggers uv sync.
echo "Running direnv allow..."
direnv allow

echo ""
echo "Done. Next steps:"
echo "  1. Edit pyproject.toml — fill in 'description' and add dependencies."
echo "  2. Edit README.md."
echo "  3. Commit: git commit -m 'init: $PKGNAME'"
