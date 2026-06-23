#!/usr/bin/env bash
# Fill missing i18n translations from en into every other locale.
# Linux/macOS twin of translate-i18n.bat. Re-run whenever en gains new keys.
#   ./translate-i18n.sh --dry-run
#   ./translate-i18n.sh --provider zai --model glm-4.6 --yes
set -euo pipefail
cd "$(dirname "$0")"
exec python3 translate_i18n.py "$@"
