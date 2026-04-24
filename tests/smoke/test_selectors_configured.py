from __future__ import annotations

from pathlib import Path

from src.utils.config_loader import load_yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _walk(node, prefix: str = ""):
    if isinstance(node, dict):
        for k, v in node.items():
            yield from _walk(v, f"{prefix}.{k}" if prefix else k)
    elif isinstance(node, str):
        yield prefix, node


def test_no_todo_selectors_remain() -> None:
    selectors = load_yaml(PROJECT_ROOT / "config" / "selectors.yml")
    todos = [path for path, value in _walk(selectors) if value == "TODO"]
    assert not todos, f"Selectors still set to TODO: {todos}"
