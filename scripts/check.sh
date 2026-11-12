#!/usr/bin/env bash
# The stack seam (pnpm + uv preset). The Architect adjusts this in Phase 0 if the stack differs.
# No argument: lint, typecheck, unit tests. `setup`: install dependencies in a fresh worktree.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

mode="${1:-check}"

case "$mode" in
  setup)
    if [ -f package.json ]; then
      pnpm install --frozen-lockfile --prefer-offline || pnpm install --no-frozen-lockfile --prefer-offline
    fi
    if [ -f pyproject.toml ]; then
      uv sync --frozen || uv sync
    fi
    ;;
  check)
    if [ -f package.json ]; then
      pnpm -r --if-present run lint
      pnpm -r --if-present run typecheck
      pnpm -r --if-present run test
    fi
    if [ -f pyproject.toml ]; then
      if uv run --no-sync ruff --version >/dev/null 2>&1; then uv run --no-sync ruff check .; fi
      rc=0
      uv run --no-sync pytest -q || rc=$?
      # pytest exits 5 when no tests were collected
      if [ "$rc" -ne 0 ] && [ "$rc" -ne 5 ]; then exit "$rc"; fi
    fi
    echo "check.sh: ok"
    ;;
  *)
    echo "usage: check.sh [setup]" >&2
    exit 2
    ;;
esac
