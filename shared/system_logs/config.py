"""Fixed operational bounds; retention and enablement come from environment."""

import json
from pathlib import Path

CONFIG = json.loads(Path(__file__).with_name("config.json").read_text())
