#!/usr/bin/env bash
# Fetch the exact upstream commits the graph is pinned to.
#
# The two source repositories are read-only inputs: this project never
# modifies them, and they are not vendored into the repo. Pin them here so
# `make build` is reproducible on any machine.
set -euo pipefail

DIR="${1:-sources}"
FRAPPE_DOCKER_COMMIT="${FRAPPE_DOCKER_COMMIT:-2b26e9a}"
ERPNEXT_COMMIT="${ERPNEXT_COMMIT:-7474d9e}"   # v16.50.0

clone_pinned() {
  local url="$1" dest="$2" commit="$3"
  if [ -d "$dest/.git" ]; then
    echo "ok: $dest already present"
  else
    echo "cloning $url -> $dest"
    git clone --filter=blob:none "$url" "$dest"
  fi
  git -C "$dest" fetch --quiet origin "$commit" 2>/dev/null || true
  git -C "$dest" checkout --quiet "$commit"
  echo "pinned $dest @ $(git -C "$dest" rev-parse --short HEAD)"
}

mkdir -p "$DIR"
clone_pinned "https://github.com/frappe/frappe_docker" "$DIR/frappe_docker" "$FRAPPE_DOCKER_COMMIT"
clone_pinned "https://github.com/frappe/erpnext" "$DIR/erpnext" "$ERPNEXT_COMMIT"
echo "done — run: make build"
