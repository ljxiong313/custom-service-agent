"""进程内令牌桶限流（B3-7）。

为什么在内存里做：本项目的部署形态是单进程（见 docs/运维手册.md），多副本要换 Redis
或网关层限流——这里刻意不做假分布式。每个进程各数各的，配置里写的是"每进程每键"。

规则匹配按路径前缀，先到先得；键默认取登录用户 id，未登录取客户端 IP
（`X-Forwarded-For` 只有在 server.trust_proxy_headers=true 时才可信，否则伪造这个头
就能绕过限流）。
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from app.config import get_settings

DEFAULT_WINDOW_SECONDS = 60.0


@dataclass
class _Bucket:
    tokens: float
    updated_at: float


class Limiter:
    """按 (scope, key) 维护令牌桶。"""

    def __init__(self) -> None:
        self._buckets: dict[tuple[str, str], _Bucket] = {}

    def allow(self, scope: str, key: str, *, per_minute: float, burst: int | None = None,
              now: float | None = None) -> tuple[bool, float]:
        """返回 (是否放行, 建议重试秒数)。per_minute<=0 表示不限流。"""
        if per_minute <= 0:
            return True, 0.0
        capacity = float(burst if burst is not None and burst > 0 else max(1.0, per_minute))
        rate = per_minute / 60.0
        moment = now if now is not None else time.monotonic()
        ident = (scope, key)
        bucket = self._buckets.get(ident)
        if bucket is None:
            bucket = _Bucket(tokens=capacity, updated_at=moment)
            self._buckets[ident] = bucket
        bucket.tokens = min(capacity, bucket.tokens + (moment - bucket.updated_at) * rate)
        bucket.updated_at = moment
        if bucket.tokens >= 1.0:
            bucket.tokens -= 1.0
            return True, 0.0
        return False, max(0.5, (1.0 - bucket.tokens) / rate)

    def prune(self, max_age_seconds: float = 600.0, now: float | None = None) -> int:
        """清掉长期空闲的桶，防止按 IP 计数被慢速耗尽内存。"""
        moment = now if now is not None else time.monotonic()
        stale = [k for k, b in self._buckets.items() if moment - b.updated_at > max_age_seconds]
        for k in stale:
            self._buckets.pop(k, None)
        return len(stale)

    def size(self) -> int:
        return len(self._buckets)


limiter = Limiter()


def _rules() -> list[dict]:
    raw = get_settings().get_dotted("security.rate_limit.rules", None) or []
    return [r for r in raw if isinstance(r, dict)]


def _default_rule() -> dict:
    d = get_settings().get_dotted("security.rate_limit.default", None)
    return d if isinstance(d, dict) else {}


def match_rule(path: str) -> tuple[str, dict] | None:
    """按配置顺序找第一条 path_prefix 命中的规则。"""
    for rule in _rules():
        prefix = str(rule.get("path_prefix", ""))
        if prefix and path.startswith(prefix):
            return prefix, rule
    return None


def client_key(request: Request) -> tuple[str, str]:
    """限流键：有 Bearer 就按用户，否则按 IP（仅在显式信任反代头时取 XFF）。"""
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return "user", auth[7:39]        # token 前 32 位当键：不必验签也能稳定区分调用方
    if bool(get_settings().get_dotted("server.trust_proxy_headers", False)):
        xff = request.headers.get("x-forwarded-for", "")
        if xff:
            return "ip", xff.split(",")[0].strip()
    return "ip", (request.client.host if request.client else "unknown")


def enabled() -> bool:
    return bool(get_settings().get_dotted("security.rate_limit.enabled", True))


class RateLimitMiddleware(BaseHTTPMiddleware):
    """只给写路径与外部回调加限流：GET 轮询（运营台 4s）不该被打限。"""

    MUTATED_PREFIXES = ("/api/v1/chat", "/api/v1/console", "/api/v1/auth/login",
                        "/api/v1/auth/register", "/api/v1/knowledge/files",
                        "/api/platform", "/api/wecom")

    async def dispatch(self, request: Request, call_next):  # noqa: ANN001 - starlette 签名
        if request.method == "GET" or not enabled() or not self._applies(request.url.path):
            return await call_next(request)

        scope, key = client_key(request)
        matched = match_rule(request.url.path)
        rule = matched[1] if matched else _default_rule()
        per_minute = float(rule.get("per_minute", _default_rule().get("per_minute", 60)) or 0)
        burst = rule.get("burst")
        ok, retry_after = limiter.allow(matched[0] if matched else "default", f"{scope}:{key}",
                                        per_minute=per_minute,
                                        burst=int(burst) if burst else None)
        if not ok:
            return JSONResponse(
                {"detail": f"请求过于频繁，请 {int(retry_after)} 秒后再试"},
                status_code=429,
                headers={"Retry-After": str(int(retry_after) + 1)})
        return await call_next(request)

    @classmethod
    def _applies(cls, path: str) -> bool:
        return any(path.startswith(p) for p in cls.MUTATED_PREFIXES)
