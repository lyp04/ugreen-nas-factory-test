from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml

_ENV_PATTERN = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)\}")


def _expand_env(value: Any) -> Any:
    if isinstance(value, str):
        def replace(match: re.Match[str]) -> str:
            var = match.group(1)
            return os.environ.get(var, match.group(0))
        return _ENV_PATTERN.sub(replace, value)
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    return value


def load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return _expand_env(data) if data else {}


def load_configs(project_root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    config = load_yaml(project_root / "config" / "config.yml")
    selectors = load_yaml(project_root / "config" / "selectors.yml")
    return config, selectors
