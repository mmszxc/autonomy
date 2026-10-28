#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
OUT=${1:-"$ROOT/dist/hermes-autonomy-source.tar.gz"}
mkdir -p "$(dirname "$OUT")"

tar \
  --exclude='.git' \
  --exclude='__pycache__' \
  --exclude='*.pyc' \
  --exclude='_vendor' \
  --exclude='_vendor_ml' \
  --exclude='seed_src/hermes/model' \
  --exclude='*.db' \
  --exclude='*.log' \
  --exclude='dist' \
  -czf "$OUT" -C "$ROOT" \
  README.md .gitignore plugins tests tools docs deploy

printf 'Created %s\n' "$OUT"
sha256sum "$OUT"
