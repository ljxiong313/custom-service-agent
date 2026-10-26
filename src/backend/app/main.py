"""FastAPI 应用入口。

启动：
    cd src/backend
    CS_CONFIG=config.local.yaml py run.py            # 读配置里的 server.host/port
    py -m uvicorn app.main:app --port 7861 --reload  # 或手工指定端口
业务接口前缀 /api/v1；免 JWT 的外部回调挂 /api（企微/平台）；文档 /docs；健康检查 /health。

部署形态：**单 worker**。本进程持有两类进程内状态——会话生命周期的周期扫描任务、
MCP 工具确认的等待表（services/mcp/confirm.py）。多 worker 下每个进程各扫一遍、
确认请求可能落到没有该 future 的进程上必然超时，所以启动时检测并拒绝（见下）。
"""
from __future__ import annotations

import asyncio
import os
import sys
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from loguru import logger

from app.config import get_settings
from app.db.session import init_db
from app.utils.logging import setup_logging


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
        logger.error("检测到 --workers {}：生命周期扫描与 MCP 确认表是进程内状态，"
                     "多 worker 会重复提示/重复终结且确认必然超时。请固定单 worker 部署。",
                     _worker_count())

    # P8 会话生命周期后台扫描（空闲提示/超时终结）；关停时取消并等它真正退出，
    # 否则 asyncio 会在解释器收尾时打印 "Task was destroyed but it is pending"
    from app.services.knowledge_jobs import stop_workers
    from app.services.lifecycle import session_sweeper

    sweeper = asyncio.create_task(session_sweeper())
    try:
        yield
    finally:
        sweeper.cancel()
        with suppress(asyncio.CancelledError):
            await sweeper
        stop_workers()
        logger.info("后台任务已关停（生命周期扫描 + 知识库重建 worker）")


app = FastAPI(title="智能客服 Agent", version="0.1.0", lifespan=lifespan)

# 运营台上传的图片/文件对外可读（P8-4，经 /api/files 走 Vite 代理）。
# 只挂载 media 子目录：知识库源文件同在 data/uploads 下但不对外暴露。
from app.config import BACKEND_ROOT  # noqa: E402

_media_dir = BACKEND_ROOT / "data" / "uploads" / "media"
_media_dir.mkdir(parents=True, exist_ok=True)
app.mount("/api/files", StaticFiles(directory=_media_dir), name="uploads")

# 开发期允许本地前端跨域（生产部署建议同源反代并收紧此配置）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:8091", "http://127.0.0.1:8091"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

from app.api.v1 import (  # noqa: E402
    auth,
    chat,
    console,
    conversation,
    handoff,
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
app.include_router(platforms.admin_router, prefix="/api/v1")
# 企微/平台回调必须免 JWT（外部服务器无法携带我们的 token），单独挂在 /api 下
app.include_router(wecom_callback.router, prefix="/api")
app.include_router(platforms.callback_router, prefix="/api")
app.include_router(mcp.router, prefix="/api/v1")


@app.get("/health")
def health():
    return {"status": "ok", "service": "cs-agent-backend", "version": app.version}
