"""全局配置加载。

设计说明：
- 参考 AgentChat 的做法，使用 YAML 而不是 .env，方便多模型/多模块的结构化配置。
- 优先级：环境变量 CS_CONFIG 指向的文件 > 后端根目录下 config.simple.yaml。
- 配置为懒加载单例（get_settings），启动时读取一次，测试时可 clear_settings 重载。
- 任意字符串值可写 `${VAR}` / `${VAR:-默认值}` 引用环境变量，密钥不必落文件（生产用
  secret 注入，见 docs/运维手册.md）。整值替换时若环境变量缺失且无默认值 → 替换成空串，
  与 YAML 里 `api_key: ""` 的降级口径一致；嵌在长串里则原样保留，避免悄悄改坏 URL。
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml

# 后端根目录：src/backend/
BACKEND_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_FILE = BACKEND_ROOT / "config.simple.yaml"

# ${NAME} 或 ${NAME:-default}
_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


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

    def is_production(self) -> bool:
        """生产形态判定：server.env=production 时启用一批"漏配即裸奔"的硬闸。

        刻意不做成"开关列表"，而是让 dev 保持宽松可跑（演示/单测），生产收紧：
        见 app/startup_check.py 的自检项与 api 层的 callback_verify 拒绝。
        """
        return str(self.get_dotted("server.env", "dev")).lower() in ("production", "prod")


def _interpolate(value: Any) -> Any:
    """递归替换字符串里的 `${ENV}` / `${ENV:-default}` 引用。"""
    if isinstance(value, dict):
        return {k: _interpolate(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_interpolate(v) for v in value]
    if not isinstance(value, str) or "${" not in value:
        return value

    def repl(m: re.Match[str]) -> str:
        return os.environ.get(m.group(1), m.group(2) or "")

    whole = _ENV_REF.fullmatch(value.strip())
    if whole:
        name, default = whole.group(1), whole.group(2)
        env = os.environ.get(name)
        if env is None:
            if default is None:
                # 整值是引用且环境变量缺失 → 按未配置处理（空串），与 YAML 空值同口径
                return ""
            return _ENV_REF.sub(repl, default)
        return env
    return _ENV_REF.sub(repl, value)


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


# 环境变量 → 配置点号路径。容器部署时"挂一份 yaml 只为了改端口"很笨，而 k8s/compose
# 天然是注入环境变量的；这里只放行运维真正需要覆盖的少量键，其余仍走 YAML。
# 值统一按目标类型转换（int/bool/逗号分隔列表），转换不了就保留 YAML 原值并告警。
ENV_OVERRIDES: dict[str, tuple[str, str]] = {
    "CS_SERVER_HOST": ("server.host", "str"),
    "CS_SERVER_PORT": ("server.port", "int"),
    "CS_SERVER_ENV": ("server.env", "str"),
    "CS_SERVER_WORKERS": ("server.workers", "int"),
    "CS_SERVE_FRONTEND": ("server.serve_frontend", "bool"),
    "CS_PUBLIC_BASE_URL": ("server.public_base_url", "str"),
    "CS_CORS_ORIGINS": ("server.cors_origins", "list"),
    "CS_DB_URL": ("database.url", "str"),
    "CS_JWT_SECRET": ("security.jwt_secret", "str"),
    "CS_METRICS_TOKEN": ("security.metrics_token", "str"),
    "CS_MODEL_API_KEY": ("multi_models.__default__.api_key", "str"),
}

_TRUTHY = {"1", "true", "yes", "on"}


def _coerce(raw: str, kind: str) -> Any:
    if kind == "int":
        return int(raw)
    if kind == "bool":
        return raw.strip().lower() in _TRUTHY
    if kind == "list":
        return [x.strip() for x in raw.split(",") if x.strip()]
    return raw


def _set_dotted(settings: Settings, path: str, value: Any) -> None:
    keys = path.split(".")
    node: dict = settings
    for k in keys[:-1]:
        nxt = node.get(k)
        if not isinstance(nxt, dict):
            nxt = {}
            node[k] = nxt
        node = nxt
    node[keys[-1]] = value


def _apply_env_overrides(settings: Settings) -> None:
    from loguru import logger

    for env_name, (path, kind) in ENV_OVERRIDES.items():
        raw = os.environ.get(env_name)
        if raw is None or not raw.strip():
            continue
        target = path
        if "__default__" in target:
            default = str(settings.get_dotted("default_model", "dashscope"))
            target = target.replace("__default__", default)
        try:
            _set_dotted(settings, target, _coerce(raw, kind))
        except (TypeError, ValueError):
            logger.warning("环境变量 {}={!r} 无法按 {} 解析进 {}，保留配置文件值",
                           env_name, raw, kind, target)
        else:
            logger.info("配置覆盖: {} ← 环境变量 {}", target, env_name)


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        path = _resolve_config_file()
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        _settings = Settings(_interpolate(raw))
        _settings["_config_file"] = str(path)
        _apply_env_overrides(_settings)
    return _settings


def clear_settings() -> None:
    """测试/热重载时清空缓存。"""
    global _settings
    _settings = None
