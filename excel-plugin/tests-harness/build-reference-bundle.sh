#!/usr/bin/env bash
#
# Build a functions bundle from an ARBITRARY commit, so the harness can be
# pointed at a known-bad build and shown to fail.
#
# This is what proves the harness has teeth rather than merely being green:
#
#   ./tests-harness/build-reference-bundle.sh d6e81e759^ /tmp/pre-9876
#   TESS_HARNESS_BUNDLE=/tmp/pre-9876/tessallite/excel-plugin/dist/functions.iife.js \
#     npm run harness:functions:run
#
# d6e81e759 is the Bug-9876 fix ("measure values reach Excel as numbers"); its
# parent is the last build that handed Excel numeric TEXT.
#
# node_modules is symlinked from the working tree, never reinstalled.
set -euo pipefail

REF="${1:?usage: build-reference-bundle.sh <git-ref> <out-dir>}"
OUT="${2:?usage: build-reference-bundle.sh <git-ref> <out-dir>}"

REPO_ROOT="$(git rev-parse --show-toplevel)"
PLUGIN_REL="tessallite/excel-plugin"

mkdir -p "$OUT"
git -C "$REPO_ROOT" archive "$REF" "$PLUGIN_REL" | tar -x -C "$OUT"

ln -sfn "$REPO_ROOT/$PLUGIN_REL/node_modules" "$OUT/$PLUGIN_REL/node_modules"

cd "$OUT/$PLUGIN_REL"
npx vite build --config vite.config.functions.ts

echo
echo "Bundle: $OUT/$PLUGIN_REL/dist/functions.iife.js"
