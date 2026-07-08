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
            # 未设置的环境变量展开为空串（而不是保留 ${VAR} 字面量）：这样 config 里
            # token: "${FAULT_REPORT_TOKEN}" 在没设该变量时就是空 → 故障上报按「留空即停用」处理。
            return os.environ.get(var, "")
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


def resolve_config_path(project_root: Path, name: str = "config.yml") -> Path:
    """优先用本地真实 config.yml（不进 git）；没有就回退到 config.example.yml。
    这样全新克隆 / 公开仓库（只带示例）也能直接起得来，本地部署仍用自己的真实配置。"""
    real = project_root / "config" / name
    if real.exists():
        return real
    example = real.with_name(f"{real.stem}.example{real.suffix}")
    return example if example.exists() else real


def load_configs(project_root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    config = load_yaml(resolve_config_path(project_root, "config.yml"))
    selectors = load_yaml(project_root / "config" / "selectors.yml")
    return config, selectors
