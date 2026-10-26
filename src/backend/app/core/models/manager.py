"""LLM 模型管理器（参考 AgentChat core/models/manager.py 的简化重建）。

统一以 OpenAI 兼容协议接入：
- get_chat_model(): 对话模型（ChatOpenAI），流式/工具调用共用
- get_embeddings(): 向量化模型（P1 RAG 使用）
模型参数来自配置 multi_models.<name>，默认取 default_model。
"""
from __future__ import annotations

from functools import lru_cache

from loguru import logger

from app.config import get_settings

# 请求必须有超时：openai SDK 不设 timeout 时会一直挂在连接上（现场日志见 data/logs/），
# 一个卡死的 embedding/llm 调用能把整条 SSE 流拖住
DEFAULT_REQUEST_TIMEOUT_SECONDS = 60
DEFAULT_MAX_RETRIES = 2


class ModelNotConfiguredError(RuntimeError):
    """未配置 api_key 时给出明确的用户可读错误（API 层转 400）。"""


def _llm_int_setting(path: str, default: int, *, minimum: int = 0) -> int:
    """读 llm.* 下的整数配置（llm 段缺失时回落默认值）。

    get_dotted 只在"键不存在"时返回 default，yaml 里写成空值/空串会原样返回，数值型必须
    在这里兜底（同 rag/vectorstore.py 的 or 回落教训）。max_retries 允许配 0（关掉重试），
    所以不能用 `or` 回落。
    """
    raw = get_settings().get_dotted(path, default)
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return default
    try:
        value = int(float(raw))
    except (TypeError, ValueError):
        logger.warning("配置 {}={!r} 不是数字，按默认值 {} 处理", path, raw, default)
        return default
    if value < minimum:
        logger.warning("配置 {}={} 小于下限 {}，按默认值 {} 处理", path, value, minimum, default)
        return default
    return value


def _client_kwargs() -> dict:
    """chat / embedding / 视觉模型共用的超时与重试参数。"""
    return {
        "timeout": _llm_int_setting("llm.request_timeout_seconds",
                                    DEFAULT_REQUEST_TIMEOUT_SECONDS, minimum=1),
        "max_retries": _llm_int_setting("llm.max_retries", DEFAULT_MAX_RETRIES),
    }


def _model_config(name: str | None) -> dict:
    s = get_settings()
    name = name or s.get_dotted("default_model", "dashscope")
    conf = s.get_dotted(f"multi_models.{name}")
    if not conf:
        raise ModelNotConfiguredError(f"配置中不存在模型 '{name}'，请检查 multi_models")
    if not conf.get("api_key"):
        raise ModelNotConfiguredError(
            f"模型 '{name}' 未配置 api_key。请在 config.local.yaml 的 "
            f"multi_models.{name}.api_key 中填写（该文件勿提交版本库）。")
    return conf


def is_configured(name: str | None = None) -> bool:
    """设置页能力总览用：只判断 api_key 是否存在，不暴露密钥内容。"""
    try:
        _model_config(name)
        return True
    except ModelNotConfiguredError:
        return False


def get_chat_model(name: str | None = None):
    from langchain_openai import ChatOpenAI

    conf = _model_config(name)
    return ChatOpenAI(
        model=conf.get("chat_model", "qwen-plus"),
        api_key=conf["api_key"],
        base_url=conf.get("base_url"),
        temperature=float(conf.get("temperature", 0.3)),
        streaming=True,
        **_client_kwargs(),
    )


@lru_cache
def get_embeddings(name: str | None = None):
    """embedding 单例缓存——Chroma 写入/查询共用同一个向量化器。"""
    from langchain_openai import OpenAIEmbeddings

    conf = _model_config(name)
    return OpenAIEmbeddings(
        model=conf.get("embedding_model", "text-embedding-v4"),
        api_key=conf["api_key"],
        base_url=conf.get("base_url"),
        # 必须关掉 tiktoken 预切片：开启时 langchain 会把 input 发成 token id 数组，
        # dashscope 兼容模式只接受字符串，会报 "contents is neither str nor list of str"
        check_embedding_ctx_length=False,
        **_client_kwargs(),
    )


def get_vision_model(name: str | None = None):
    """视觉模型（P1 图片 image2text）。vl_model 未配置时给出明确错误。"""
    from langchain_openai import ChatOpenAI

    conf = _model_config(name)
    if not conf.get("vl_model"):
        raise ModelNotConfiguredError(
            f"模型 '{name}' 未配置 vl_model（如 qwen-vl-plus），无法解析图片知识库文件。")
    return ChatOpenAI(
        model=conf["vl_model"],
        api_key=conf["api_key"],
        base_url=conf.get("base_url"),
        temperature=0.1,
        **_client_kwargs(),
    )


def get_rerank_config(name: str | None = None) -> dict:
    """P1 检索重排使用的 dashscope rerank HTTP API 参数。"""
    conf = _model_config(name)
    return {
        "api_key": conf["api_key"],
        "model": conf.get("rerank_model", "gte-rerank-v2"),
        "base_url": conf.get(
            "rerank_base_url",
            "https://dashscope.aliyuncs.com/api/v1/services/rerank/text-rerank/text-rerank"),
    }
