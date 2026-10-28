"""dashscope gte-rerank-v2 重排（HTTP API）。

降级策略：未配置 key / 网络失败 / 响应异常时原序返回 fused 结果并记 warning——
重排是精度增强项，不应阻断检索链路。
"""
from __future__ import annotations

import time

from loguru import logger

TIMEOUT_SECONDS = 15.0
CONNECT_TIMEOUT_SECONDS = 5.0   # 挂在建连是最常见的一种"请求不动"，单独收紧
RETRY_DELAY_SECONDS = 0.5       # 指数退避基数；只重试一次，检索链路不能被拖成秒级等待
MAX_ATTEMPTS = 2


def _post_with_retry(url: str, *, headers: dict, payload: dict) -> dict:
    """POST 一次，网络类故障/5xx/429 退避重试一次；仍失败则抛出交由调用方降级。

    4xx（鉴权失败、报文不合法）重试没有意义，直接抛出，避免把配置错误伪装成网络抖动。
    """
    import httpx

    timeout = httpx.Timeout(TIMEOUT_SECONDS, connect=CONNECT_TIMEOUT_SECONDS)
    last_error: Exception | None = None
    for attempt in range(MAX_ATTEMPTS):
        if attempt:
            time.sleep(RETRY_DELAY_SECONDS * (2 ** (attempt - 1)))
        try:
            resp = httpx.post(url, headers=headers, json=payload, timeout=timeout)
            resp.raise_for_status()
            return resp.json()
        except httpx.TransportError as e:      # 含超时、连接重置、DNS
            last_error = e
        except httpx.HTTPStatusError as e:
            code = e.response.status_code
            if code != 429 and code < 500:
                raise
            last_error = e
        logger.warning("rerank 请求失败（第 {}/{} 次）: {}", attempt + 1, MAX_ATTEMPTS, last_error)
    if last_error is None:                     # 理论不可达，压 mypy 的空值
        raise RuntimeError("rerank 请求失败")
    raise last_error


def rerank(query: str, documents: list[str], top_n: int) -> list[tuple[int, float]] | None:
    """返回 [(原始下标, 相关性分数)] 降序；None 表示跳过重排。"""
    try:
        from app.core.models.manager import get_rerank_config, resolve_name
        from app.services import usage

        conf = get_rerank_config()
        if not documents:
            return None
        # 只包外呼那一次：请求失败照常上抛给外层 except，保持"降级返回 None"的行为不变，
        # 但流水里会留下一条 status=error——运维才看得到重排一直在跳过。
        with usage.track("rerank", provider=resolve_name(), model=conf["model"]) as st:
            st["inputs"] = [query, *documents]
            data = _post_with_retry(
                conf["base_url"],
                headers={"Authorization": f"Bearer {conf['api_key']}",
                         "Content-Type": "application/json"},
                payload={"model": conf["model"],
                         "input": {"query": query, "documents": documents},
                         "parameters": {"return_documents": False,
                                        "top_n": min(top_n, len(documents))}})
        results = data["output"]["results"]
        return [(r["index"], float(r["relevance_score"])) for r in results]
    except Exception as e:
        logger.warning("rerank 跳过（降级为 RRF 原序）: {}", e)
        return None
