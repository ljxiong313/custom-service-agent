"""启动期安全自检：把"漏配即裸奔"的项在进程起来时就暴露出来。

口径：
- `server.env != production`（默认）只告警，演示/单测要能开箱跑；
- `server.env = production` 时致命项直接拒绝启动——容器 crash-loop 比静默裸奔好，
  因为裸奔的代价是数据泄漏，而 crash 的代价是部署失败，后者可回滚。
- 结果同时被 GET /api/v1/system/status 消费，运维不必翻日志就能看到当前形态缺什么。

每一项都是 (level, key, 文案)：level ∈ {"fatal", "warn", "ok"}。
"""
from __future__ import annotations

from typing import Any

from app.config import get_settings

DEFAULT_JWT_SECRETS = {"", "dev-secret-change-me", "change-me", "secret", "changeme"}
MIN_SECRET_LEN = 32


def _jwt_secret(s: Any) -> str:
    return str(s.get_dotted("security.jwt_secret", "") or "")


def run_checks() -> list[dict[str, str]]:
    s = get_settings()
    prod = s.is_production()
    out: list[dict[str, str]] = []

    secret = _jwt_secret(s)
    if secret in DEFAULT_JWT_SECRETS or len(secret) < MIN_SECRET_LEN:
        out.append({
            "level": "fatal" if prod else "warn",
            "key": "jwt_secret",
            "message": f"security.jwt_secret 仍是默认值/短于 {MIN_SECRET_LEN} 位，"
                       "任何人都能伪造登录 token（生产必须覆盖，可用 ${CS_JWT_SECRET} 注入）"})
    else:
        out.append({"level": "ok", "key": "jwt_secret", "message": "JWT 密钥已配置"})

    if not str(s.get_dotted("security.media_url_secret", "") or ""):
        out.append({
            "level": "ok", "key": "media_url_secret",
            "message": "媒体签名 URL 复用 security.jwt_secret（可在 security.media_url_secret 单独指定）"})

    # 平台回调免 JWT，唯一防线是验签；callback_verify=none 等于把门拆了
    platforms = s.get_dotted("platforms", {}) or {}
    real_outbound: list[str] = []
    for name, conf in platforms.items():
        if not isinstance(conf, dict) or not conf.get("enabled", False):
            continue
        if str(conf.get("callback_verify", "sign")).lower() == "none":
            out.append({
                "level": "fatal" if prod else "warn",
                "key": f"platform.{name}.callback_verify",
                "message": f"平台 {name} 已启用却把 callback_verify 配成 none："
                           "回调不验签，任何人可伪造买家消息驱动 Agent 与外发"})
        if conf.get("mock"):
            out.append({
                "level": "warn", "key": f"platform.{name}.mock",
                "message": f"平台 {name} mock=true：回复只进外发箱不会送达真实买家"})
        else:
            real_outbound.append(name)

    if (s.get_dotted("wecom.mock", False)) and not platforms:
        out.append({"level": "warn", "key": "wecom.mock",
                    "message": "企微 mock=true：转人工卡片只进外发箱，不会真的通知客服"})

    origins = s.get_dotted("server.cors_origins", None)
    if origins is None:
        out.append({"level": "ok", "key": "cors",
                    "message": "CORS 用内置开发白名单（生产请在 server.cors_origins 指定域名，"
                               "或走同源反代后留空列表）"})
    elif any(str(o).strip() in ("*", "") for o in (origins or [])) and prod:
        out.append({"level": "fatal", "key": "cors",
                    "message": "生产环境 CORS 含 * ：任意站点可带凭域发起跨域请求"})

    # 附件链接的可达性：媒体链接按"绝对地址 + 签名"拼（app/utils/media_url.py），
    # public_base_url 留空就只能给出 /api/files/… 的相对路径——买家在平台客户端里点不开。
    # 有平台真实外呼时这是"发出去的东西客户用不了"，生产直接拒绝启动：相对链接不报错、
    # 不落 failed，唯一症状是买家说"链接打不开"，比一次 crash-loop 难查得多。
    if not str(s.get_dotted("server.public_base_url", "") or "").strip():
        if real_outbound:
            out.append({
                "level": "fatal" if prod else "warn", "key": "public_base_url",
                "message": f"平台 {'、'.join(sorted(real_outbound))} 已开真实外呼（mock=false），"
                           "但 server.public_base_url 未配置：买家收到的附件链接是相对路径，"
                           "点不开（媒体出站会记 skipped）。请填公网可达地址（末尾不带 /）"})
        elif prod:
            out.append({
                "level": "warn", "key": "public_base_url",
                "message": "server.public_base_url 未配置：平台买家收到的附件链接是相对路径，点不开"
                           "（当前平台都是 mock 演示，切真实外呼前必须配上）"})

    return out


def fatal_findings(checks: list[dict[str, str]]) -> list[dict[str, str]]:
    return [c for c in checks if c["level"] == "fatal"]
