"""记忆后台管道：对话完成后异步执行中期摘要 + 长期画像抽取。

设计：asyncio.create_task 挂后台（进程内），失败只记日志——记忆是增强项，
绝不影响当轮对话体验。并发控制：同一会话同时只跑一个管道任务。
"""
from __future__ import annotations

import asyncio

from loguru import logger

_running: set[int] = set()


def schedule_memory_jobs(conversation_id: int, user_id: int) -> None:
    if conversation_id in _running:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.create_task(_run(conversation_id, user_id))


async def _run(conversation_id: int, user_id: int) -> None:
    _running.add(conversation_id)
    from app.db.session import Session, engine

    try:
        # LLM 调用是同步阻塞的，放线程池避免拖慢事件循环
        def work() -> None:
            with Session(engine) as session:
                from app.config import get_settings
                from app.services.memory import long as long_mem
                from app.services.memory import mid

                mid.ensure_summary(session, conversation_id)
                if bool(get_settings().get_dotted("memory.long_enabled", True)):
                    long_mem.extract_facts(session, conversation_id, user_id)

        await asyncio.to_thread(work)
    except Exception as e:
        logger.debug("记忆管道失败（忽略）: {}", e)
    finally:
        _running.discard(conversation_id)
