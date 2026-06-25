#!/usr/bin/env bash
# generate-manifest.sh — produce manifest.xml and public/functions.json from
# their templates, both substituting the deploy base URL.
#
# Usage:
#   bash scripts/generate-manifest.sh https://cloud.tessallite.io
#   bash scripts/generate-manifest.sh https://localhost:3443
#   PLUGIN_BASE_URL=https://cloud.tessallite.io bash scripts/generate-manifest.sh
#
# Outputs:
#   tessallite/excel-plugin/manifest.xml         (from manifest.xml.template)
#   tessallite/excel-plugin/public/functions.json (from public/functions.json.template)
#
# F-025-22: functions.json's `helpUrl` fields used to hard-code
# https://localhost:3443, so every cloud user's "Help on this function" link was
# broken. They are now templated in the same step as the manifest.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
TEMPLATE="${PLUGIN_DIR}/manifest.xml.template"
OUTPUT="${PLUGIN_DIR}/manifest.xml"
FN_TEMPLATE="${PLUGIN_DIR}/public/functions.json.template"
FN_OUTPUT="${PLUGIN_DIR}/public/functions.json"

PLUGIN_BASE_URL="${1:-${PLUGIN_BASE_URL:-https://localhost:3443}}"

if [[ ! -f "$TEMPLATE" ]]; then
  echo "ERROR: template not found: ${TEMPLATE}" >&2
  exit 1
fi

export PLUGIN_BASE_URL
envsubst '$PLUGIN_BASE_URL' < "$TEMPLATE" > "$OUTPUT"
echo "manifest.xml written for ${PLUGIN_BASE_URL}"

if [[ -f "$FN_TEMPLATE" ]]; then
  envsubst '$PLUGIN_BASE_URL' < "$FN_TEMPLATE" > "$FN_OUTPUT"
  echo "public/functions.json written for ${PLUGIN_BASE_URL}"
else
  echo "WARN: functions template not found: ${FN_TEMPLATE} (functions.json left unchanged)" >&2
fi
