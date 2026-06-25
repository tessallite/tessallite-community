import json
import os
from typing import Dict, Any, Optional

# Cached registry state
_REGISTRY: Dict[str, Any] = {}

def _load_registry() -> None:
    """Loads the JSON registry into memory if it hasn't been loaded yet."""
    global _REGISTRY
    if not _REGISTRY:
        current_dir = os.path.dirname(os.path.abspath(__file__))
        json_path = os.path.join(current_dir, "sql_functions.json")
        with open(json_path, "r", encoding="utf-8") as f:
            _REGISTRY = json.load(f)

def get_aggregate_func(name: str) -> Optional[Dict[str, Any]]:
    """Returns routing config for an aggregate function (e.g. {'routing': 'mappable', 'literal_aware': True})."""
    _load_registry()
    return _REGISTRY.get("aggregate", {}).get(name.lower())

def get_scalar_func(name: str) -> Optional[str]:
    """Returns routing behavior for a scalar function (e.g., 'transparent')."""
    _load_registry()
    return _REGISTRY.get("scalar", {}).get(name.lower())

def is_deterministic_literal(name: str) -> bool:
    """Returns True if the function is a deterministic literal like current_date."""
    _load_registry()
    return name.lower() in _REGISTRY.get("deterministic_literal", [])

def is_pseudo_literal(value: str) -> str:
    """Returns 'row_marker' for '1', '*', or any numeric literal; otherwise returns 'constant'."""
    _load_registry()
    val_str = str(value).lower()
    if val_str == "*" or val_str.replace("-", "").replace(".", "").isdigit():
        return "row_marker"
    return _REGISTRY.get("pseudo_literals", {}).get(val_str, "constant")

def is_known_function(name: str) -> bool:
    """Returns True if the function exists in either the aggregate or scalar registry."""
    _load_registry()
    name_lower = name.lower()
    return name_lower in _REGISTRY.get("aggregate", {}) or name_lower in _REGISTRY.get("scalar", {})