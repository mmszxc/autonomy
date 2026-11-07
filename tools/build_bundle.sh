#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
OUT=${1:-"$ROOT/dist/hermes-autonomy-source.tar.gz"}
mkdir -p "$(dirname "$OUT")"

tar \
  --exclude='__pycache__' \
  --exclude='*.pyc' \
  -czf "$OUT" -C "$ROOT" \
  README.md .gitignore plugins tests tools docs deploy

# Generate the deploy manifest from the immutable archive payload, never from
# the mutable source tree.  This keeps archive, staging and canonical counts
# comparable and prevents silently omitted vendor/model subtrees.
STAGING="$(mktemp -d)"
trap 'rm -rf "$STAGING"' EXIT
tar -xzf "$OUT" -C "$STAGING"
MANIFEST="${OUT}.manifest.sha256"
(
  cd "$STAGING"
  find . -type f -print0 | LC_ALL=C sort -z | xargs -0 sha256sum
) > "$MANIFEST"

ARCHIVE_FILES="$(tar -tvzf "$OUT" | awk 'substr($1,1,1)=="-"{n++} END{print n+0}')"
MANIFEST_FILES="$(wc -l < "$MANIFEST" | tr -d ' ')"
if [[ "$ARCHIVE_FILES" != "$MANIFEST_FILES" ]]; then
  echo "archive/manifest file-count mismatch: archive=$ARCHIVE_FILES manifest=$MANIFEST_FILES" >&2
  exit 1
fi

printf 'Created %s\n' "$OUT"
sha256sum "$OUT"
printf 'Manifest %s (%s files)\n' "$MANIFEST" "$MANIFEST_FILES"
