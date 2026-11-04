#!/usr/bin/env bash
# Update everything: this PC (the Setup Manager app) and the agent on every setup PC.
# Every setup PC gets a NEW unique random token, a pinned TLS certificate and the encrypted
# USB/IP tunnel. At the end, "require encryption" is turned on.
#
#   ./update-lab.sh                  update this PC, then all the setup PCs (asks before it starts)
#   ./update-lab.sh --dry-run        only show the plan
#   ./update-lab.sh --ssh-user dev   SSH user for setups that have no SSH target yet
#   ./update-lab.sh --only bench-1   only some setups (repeatable)
#   ./update-lab.sh --skip-manager   do not update this PC
#   ./update-lab.sh --keep-tokens    do not change the tokens
#
# Run it as your normal user. It asks for sudo on this PC (install-app.sh) and, once, for sudo on
# the setup PCs (if they need a password). SSH keys make it easier: ssh-copy-id USER@HOST.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKIP=0
ARGS=()
for a in "$@"; do
  case "$a" in
    --skip-manager) SKIP=1;;
    -h|--help) sed -n '2,14p' "$0"; exit 0;;
    *) ARGS+=("$a");;
  esac
done
if [[ $EUID -eq 0 ]]; then echo "Run this as your normal user (not with sudo)." >&2; exit 1; fi

if [[ $SKIP -eq 0 ]]; then
  echo "=== 1. Update this PC ==="
  cd "$HERE"
  if git rev-parse --is-inside-work-tree >/dev/null 2>&1 && git rev-parse --abbrev-ref '@{u}' >/dev/null 2>&1; then
    if [[ -n "$(git status --porcelain --untracked-files=no)" ]]; then
      echo "!! you have local changes in $HERE: not pulling (commit or stash them first)"
    else
      git pull --ff-only
    fi
  else
    echo "(not a git clone with an upstream branch: using the files as they are)"
  fi
  ./install-app.sh
fi

SM="$HOME/.local/bin/setup-manager"
[[ -x "$SM" ]] || SM="$(command -v setup-manager || true)"
[[ -n "$SM" ]] || { echo "setup-manager is not installed: run ./install-app.sh" >&2; exit 1; }

echo
echo "=== 2. Update the setup PCs ==="
exec "$SM" rollout "${ARGS[@]+"${ARGS[@]}"}"
