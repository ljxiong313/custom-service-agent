"""FastAPI 应用入口。

启动：
    cd src/backend
    CS_CONFIG=config.local.yaml py run.py            # 读配置里的 server.host/port
    py -m uvicorn app.main:app --port 7861 --reload  # 或手工指定端口
业务接口前缀 /api/v1；免 JWT 的外部回调挂 /api（企微/平台）；文档 /docs；健康检查 /health。

部署形态：**默认单 worker**。会话生命周期的周期扫描任务已改为按租约抢行（多进程只会
有一家真正动手），MCP 工具确认的等待状态落库（跨进程可裁决）；但 sweeper 任务本身仍是
每进程一个循环，多副本部署请核对 docs/运维手册.md 的容量与选主说明。
启动时会跑安全自检：server.env=production 下致命项（默认 jwt_secret、回调不验签等）
直接拒绝启动，开发形态只告警。
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from collections import defaultdict
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from loguru import logger

from app.config import get_settings
from app.db.session import engine, init_db
from app.utils import rate_limit
from app.utils.logging import setup_logging

_START_TS = time.time()
# HTTP 计数按 (method, 路由模板, 状态码类别) 归并——用模板而不是原始 path，
# 否则 /api/files/<uuid> 这类路径会把指标基数炸到无法收拾。
_http_counts: dict[tuple[str, str, str], int] = defaultdict(int)


def _worker_count() -> int:
    """uvicorn 的 worker 数只能从命令行/环境变量推断（lifespan 里没有 Server 对象）。"""
    argv = " ".join(sys.argv)
    if "--workers" in argv:
        try:
            return int(argv.split("--workers")[1].strip().split()[0].lstrip("=").strip())
        except (IndexError, ValueError):
            return 1
    return int(os.environ.get("WEBCONCURRENCY", "1") or 1)


@asynccontextmanager
async def lifespan(_: FastAPI):
    setup_logging()
    init_db()
    s = get_settings()
    logger.info("配置加载完成: {}", s.get("_config_file"))
    logger.info("数据库初始化完成")
    if _worker_count() > 1:
        logger.info("检测到 --workers {}：确认表/记忆任务/生命周期扫描已落库并带租约选主，"
                    "多进程可跑；但 BM25 索引与 MCP 工具发现缓存仍是每进程各一份，"
                    "多副本部署前先读 docs/运维手册.md 的容量与一致性说明。", _worker_count())

    from app import startup_check

    findings = startup_check.run_checks()
    for f in findings:
        if f["level"] != "ok":
            logger.warning("安全自检[{}]: {}", f["level"], f["message"])
    fatal = startup_check.fatal_findings(findings)
    if fatal:
        raise RuntimeError(
            "安全自检未通过，拒绝启动（这是有意的：带默认密钥/不验签上线等于裸奔）:\n  - "
            + "\n  - ".join(f["message"] for f in fatal))

    # P8 会话生命周期扫描 + B3-8 记忆补跑 + 出站失败退避重试：三者都靠 workerlease 选主，
    # 关停时取消并等它们真正退出，否则 asyncio 会在解释器收尾时打印
    # "Task was destroyed but it is pending"
    from app.services.knowledge_jobs import stop_workers
    from app.services.lifecycle import session_sweeper
    from app.services.memory.pipeline import memory_backfill_loop
    from app.services.platforms.outbound_retry import outbox_retry_loop

    background_tasks = [asyncio.create_task(session_sweeper()),
                        asyncio.create_task(memory_backfill_loop()),
                        asyncio.create_task(outbox_retry_loop())]
    try:
        yield
    finally:
        for task in background_tasks:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        stop_workers()
        logger.info("后台任务已关停（生命周期扫描 + 记忆补跑 + 外发重试 + 知识库重建 worker）")


app = FastAPI(title="智能客服 Agent", version="0.1.0", lifespan=lifespan)


@app.middleware("http")
async def _count_requests(request: Request, call_next):
    """HTTP 请求计数（供 /metrics）。

    基数控制在 method × 路径前两段 × 状态码类别：路由模板在中间件层还拿不到
    （route 要等路由执行完才写进 scope），按原始 path 计数会被 /api/files/<uuid> 炸开。
    """
    response = await call_next(request)
    segments = [p for p in request.url.path.split("/") if p][:2]
    bucket = "/" + "/".join(segments) if segments else "/"
    _http_counts[(request.method, bucket, f"{response.status_code // 100}xx")] += 1
    return response

# 运营台上传的图片/文件对外可读（P8-4，经 /api/files 走 Vite 代理）。
# 只开放 media 子目录：知识库源文件同在 data/uploads 下但不对外暴露。
# B3-6 起不再用 StaticFiles 裸挂载：链接必须带 ?exp=&sig= 签名（app/utils/media_url.py），
# 否则拿到 URL 就能读，uuid 文件名只能防枚举、不等于访问控制。
from app.config import BACKEND_ROOT  # noqa: E402
from app.utils import media_url as _media_url  # noqa: E402

_media_dir = BACKEND_ROOT / "data" / "uploads" / "media"
_media_dir.mkdir(parents=True, exist_ok=True)


@app.get("/api/files/{fname}")
def serve_media(fname: str, exp: str = "", sig: str = "") -> FileResponse:
    """签名放行的媒体读取：链接带 ?exp=&sig=，过期或被转发给第三方都读不到。"""
    if not _media_url.verify(fname, exp, sig):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "文件访问链接无效或已过期")
    target = _media_dir / fname
    if not target.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "文件不存在")
    return FileResponse(target, filename=fname, content_disposition_type="inline",
                        media_type=_media_url.media_type_for(fname))


def _cors_origins() -> list[str]:
    raw = get_settings().get_dotted("server.cors_origins", None)
    if raw is None:      # 未配置 → 开发期本地前端白名单
        return ["http://localhost:8091", "http://127.0.0.1:8091"]
    return [str(o).strip() for o in raw if str(o).strip()]


# 限流必须在 CORS **之前**注册：后注册的中间件在外层，CORS 放外面才能让 429 响应
# 也带上跨域头，浏览器前端才读得到"请求过于频繁"的提示。
app.add_middleware(rate_limit.RateLimitMiddleware)

# 跨域白名单进配置（B3-6）：生产同源反代时配成空列表即可彻底关掉 CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

from app.api.v1 import (  # noqa: E402
    auth,
    chat,
    console,
    conversation,
    feedback,
    handoff,
    insights,
    knowledge,
    mcp,
    memory,
    platforms,
    system,
    wecom_callback,
)

app.include_router(auth.router, prefix="/api/v1")
app.include_router(conversation.router, prefix="/api/v1")
app.include_router(chat.router, prefix="/api/v1")
app.include_router(console.router, prefix="/api/v1")
app.include_router(knowledge.router, prefix="/api/v1")
app.include_router(handoff.router, prefix="/api/v1")
app.include_router(memory.router, prefix="/api/v1")
app.include_router(system.router, prefix="/api/v1")
app.include_router(insights.router, prefix="/api/v1")
app.include_router(platforms.admin_router, prefix="/api/v1")
# 企微/平台回调必须免 JWT（外部服务器无法携带我们的 token），单独挂在 /api 下
app.include_router(wecom_callback.router, prefix="/api")
app.include_router(platforms.callback_router, prefix="/api")
# 满意度回访页也免 JWT（买家没有站内登录态），防线是链接上的签名
app.include_router(feedback.router, prefix="/api")
app.include_router(mcp.router, prefix="/api/v1")


@app.get("/health")
def health() -> dict:
    """存活探针：进程还在、事件循环能响应就算活。给 docker HEALTHCHECK 用。"""
    return {"status": "ok", "service": "cs-agent-backend", "version": app.version,
            "uptime_seconds": int(time.time() - _START_TS)}


@app.get("/ready")
def ready() -> JSONResponse:
    """就绪探针：查库、看模型链是否至少有一个可用、看启动自检有没有致命项。

    与 /health 分开的意义：反代/load balancer 应该把流量从"活着但没法服务"的实例上
    摘掉（库连接坏了、密钥被改错），而不是只看进程在不在。
    """
    from sqlalchemy import text as sql_text

    from app import startup_check
    from app.core.models.manager import chat_chain, is_configured
    from app.db.session import engine
    from app.services import knowledge_jobs

    checks: dict[str, str] = {}
    try:
        with engine.connect() as conn:
            conn.execute(sql_text("SELECT 1"))
        checks["database"] = "ok"
    except Exception as e:
        checks["database"] = f"error: {str(e)[:120]}"

    # 模型没配 / worker 没起是"降级"而不是"没就绪"：知识库管理、转人工、工单这些
    # 不依赖模型的功能仍然可用，把实例判死会让整站不可用，比降级更糟。
    checks["model"] = "ok" if any(is_configured(p) for p in chat_chain()) else "degraded: no_configured_provider"
    checks["ingest_worker"] = "ok" if knowledge_jobs.worker_alive() else "idle"
    fatal = startup_check.fatal_findings(startup_check.run_checks())
    if fatal:
        checks["security"] = "fatal: " + "; ".join(f["key"] for f in fatal)

    ok = checks["database"] == "ok" and not fatal
    body = {"ready": ok, "checks": checks, "uptime_seconds": int(time.time() - _START_TS)}
    return JSONResponse(body, status_code=200 if ok else 503)


@app.get("/metrics")
def metrics(request: Request) -> PlainTextResponse:
    """Prometheus 抓取端点。

    默认不开：`security.metrics_token` 配了就必须带 `?token=` 或 Bearer；没配 token 时
    只在非生产形态开放（演示用），生产不配 token 直接 403——指标里带着模型名、会话量这类
    运营信息，不该挂在公网路径上等爬虫。
    """
    from sqlmodel import Session as SASession

    from app.services import usage
    from app.services.mcp import confirm as mcp_confirm

    s = get_settings()
    token = str(s.get_dotted("security.metrics_token", "") or "")
    provided = request.query_params.get("token") or request.headers.get("authorization", "")
    provided = provided.removeprefix("Bearer ").strip()
    if token:
        if provided != token:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "metrics token 不正确")
    elif s.is_production():
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            "生产环境必须配置 security.metrics_token 才能暴露 /metrics")

    extra: dict[str, float] = {
        "process_uptime_seconds": int(time.time() - _START_TS),
        "mcp_pending_confirmations": mcp_confirm.pending_count(),
        "rate_limit_buckets": rate_limit.limiter.size(),
        "http_requests": sum(_http_counts.values()),
    }
    with SASession(engine) as session:
        body = usage.prometheus_text(session, extra=extra)
    for (method, path, cls_), count in _http_counts.items():
        body += (f'cs_http_requests_total{{method="{method}",path="{path}",class="{cls_}"}} '
                 f"{count}\n")
    return PlainTextResponse(body, media_type="text/plain; version=0.0.4")


# ---- 可选：由后端直接托管前端构建产物（同源部署，CORS 可以彻底关掉）----
# 单容器交付时不用再来一个 nginx：`npm run build` 的 dist 拷进镜像，
# server.serve_frontend=true 就挂载。路由必须放在**所有 API 之后**，
# 因为 Starlette 按注册顺序匹配，兜底路由排前面会把 /api/* 吃掉。
# 开关判断放在请求期而不是导入期：测试与热改配置都能生效，代价只是一次字典读取。
# 本块必须留在文件**最后**：Starlette 按注册顺序匹配，兜底路由先注册会
# 抢掉 /health、/ready、/metrics 这些非 /api 前缀的探针（踩过一次）。
def _frontend_dir() -> Path:
    raw = str(get_settings().get_dotted("server.frontend_dist", "") or "").strip()
    if not raw:
        return BACKEND_ROOT / "web"
    p = Path(raw)
    return p if p.is_absolute() else (BACKEND_ROOT / p)


@app.get("/{full_path:path}", include_in_schema=False)
def spa_fallback(full_path: str) -> FileResponse:
    if not bool(get_settings().get_dotted("server.serve_frontend", False)):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "接口不存在")
    web = _frontend_dir()
    index = web / "index.html"
    if not index.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            f"前端未构建：找不到 {index}（跑 npm run build 或关掉 serve_frontend）")
    # 这些前缀各有正规路由；走到兜底说明是没注册过的接口，
    # 不能用 index.html 把 404 掩盖成 200（前端会看到一片"页面坏了"却查不到接口错误）
    if full_path.startswith(("api/", "health", "ready", "metrics", "docs", "openapi.json")):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "接口不存在")
    candidate = (web / full_path).resolve()
    if full_path and candidate.is_file() and web.resolve() in candidate.parents:
        return FileResponse(candidate)
    return FileResponse(index)   # 深链接（/chat/12）交给 Vue Router 解析
