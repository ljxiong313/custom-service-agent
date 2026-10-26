"""全局配置加载。

设计说明：
- 参考 AgentChat 的做法，使用 YAML 而不是 .env，方便多模型/多模块的结构化配置。
- 优先级：环境变量 CS_CONFIG 指向的文件 > 后端根目录下 config.simple.yaml。
- 配置为懒加载单例（get_settings），启动时读取一次，测试时可 clear_settings 重载。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

# 后端根目录：src/backend/
BACKEND_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_FILE = BACKEND_ROOT / "config.simple.yaml"


class Settings(dict):
    """点号访问的只读配置字典包装。

    用法：settings.get_dotted("multi_models.dashscope.api_key")
    """

    def get_dotted(self, path: str, default: Any = None) -> Any:
        node: Any = self
        for key in path.split("."):
            if not isinstance(node, dict) or key not in node:
                return default
            node = node[key]
        return node


_settings: Settings | None = None


def _resolve_config_file() -> Path:
    """按优先级解析配置文件路径。CS_CONFIG 允许相对路径（相对后端根目录解析）。"""
    env_file = os.environ.get("CS_CONFIG")
    if env_file:
        p = Path(env_file)
        if not p.is_absolute():
            p = BACKEND_ROOT / p
        if not p.exists():
            raise FileNotFoundError(f"CS_CONFIG 指向的文件不存在: {p}")
        return p
    if not DEFAULT_CONFIG_FILE.exists():
        raise FileNotFoundError(f"缺少默认配置文件: {DEFAULT_CONFIG_FILE}")
    return DEFAULT_CONFIG_FILE


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        path = _resolve_config_file()
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        _settings = Settings(raw)
        _settings["_config_file"] = str(path)
    return _settings


def clear_settings() -> None:
    """测试/热重载时清空缓存。"""
    global _settings
    _settings = None
