"""企微自建应用客户端：access_token 缓存 + 应用消息发送。

mock 模式（wecom.mock=true，默认）：不外呼企微，把"发出的卡片"记入外发箱并写日志——
本地即可完整演示转人工闭环（工作台页可见 outbox）。
正式模式需配置 corp_id / corp_secret / agent_id / touser（客服企微 userid）。
外发记录自 B1-2 起落 OutboxRecord 表（channel=wecom），重启不丢；发送失败也留痕，
但仍然只由调用方决定是否回滚（建单流程保持"推送失败不影响工单"）。
"""
from __future__ import annotations

import time

import httpx
from loguru import logger

from app.config import get_settings
from app.services import outbox

# 模块级缓存：token 只缓存 AccessToken，外发记录已落库
_token_cache: dict[str, tuple[str, float]] = {}
CHANNEL = outbox.WECOM_CHANNEL


def outbox_records(limit: int = 50) -> list[dict]:
    """转人工卡片外发记录（供 GET /handoff/outbox 读）。"""
    return outbox.recent(CHANNEL, limit)


def _conf() -> dict:
    return get_settings().get_dotted("wecom", {}) or {}


def is_mock() -> bool:
    return bool(_conf().get("mock", True))


def get_access_token() -> str:
    conf = _conf()
    corp_id = conf.get("corp_id")
    now = time.time()
    cached = _token_cache.get(corp_id or "")
    if cached and cached[1] > now + 60:
        return cached[0]
    resp = httpx.get("https://qyapi.weixin.qq.com/cgi-bin/gettoken",
                     params={"corpid": corp_id, "corpsecret": conf.get("corp_secret")},
                     timeout=15)
    data = resp.json()
    if data.get("errcode") != 0:
        raise RuntimeError(f"获取企微 access_token 失败: {data}")
    _token_cache[corp_id or ""] = (data["access_token"], now + data.get("expires_in", 7200))
    return data["access_token"]


def missing_credentials() -> str:
    """真实模式下的缺项中文名（mock 模式不需要任何企微参数）。"""
    conf = _conf()
    missing = [k for k in ("corp_id", "corp_secret", "agent_id")
               if not str(conf.get(k, "") or "").strip()]
    return "、".join(f"wecom.{k}" for k in missing)


def send_textcard(touser: str, title: str, description: str, url: str = "") -> dict:
    """发送文本卡片消息（转人工工单提醒）。返回企微响应或 mock 记录。"""
    conf = _conf()
    payload = {
        "touser": touser or conf.get("touser", "@all"),
        "msgtype": "textcard",
        "agentid": int(conf.get("agent_id", 0) or 0),
        "textcard": {"title": title, "description": description, "url": url or conf.get("workbench_url", ""),
                     "btntxt": "去处理"},
    }

    def _record(status: str, data: dict, *, error: str | None = None) -> dict:
        return outbox.record(CHANNEL, "textcard", to=str(payload["touser"]), title=title,
                             body=description, payload={"mock": status == "mocked", **data},
                             status=status, error=error)

    if is_mock():
        record = _record("mocked", payload)
        logger.info("[wecom-mock] 推送卡片: {} | {}", title, description[:80])
        return record

    missing = missing_credentials()
    if missing:
        error = (f"企微未真实推送：缺 {missing}；请按 config.simple.yaml 里 wecom 节的注释补齐"
                 "（corp_id/corp_secret/agent_id 来自企微自建应用详情页），本条只记入外发箱，客服收不到卡片")
        logger.warning(error)
        return _record("skipped", payload, error=error)

    try:
        token = get_access_token()
        resp = httpx.post("https://qyapi.weixin.qq.com/cgi-bin/message/send",
                          params={"access_token": token}, json=payload, timeout=15)
        data = resp.json()
        if data.get("errcode") != 0:
            raise RuntimeError(f"企微消息发送失败: {data}")
    except Exception as e:
        # 只留痕不改控制流：建单方仍需按原有语义"推送失败不回滚工单"
        _record("failed", payload, error=str(e))
        raise
    _record("sent", {**payload, "response": data})
    logger.info("企微卡片已发送 touser={}", payload["touser"])
    return data
