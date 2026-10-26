"""Agent 工具注册中心：按「能力是否就绪」渐进启用（见 docs/工具规划.md）。

原则：知识库没有就绪文件时不挂 rag_search——空工具只会诱发无效调用与幻觉。
P2 追加 recall_memory，P3 追加 request_handoff，P4 合并 MCP 动态工具。
"""
from __future__ import annotations

from langchain_core.tools import BaseTool
from loguru import logger
from sqlmodel import func, select

from app.db.models import Chunk, KnowledgeFile, Product, User


def _knowledge_ready(session) -> bool:
    return session.exec(
        select(func.count()).select_from(Chunk)
        .join(KnowledgeFile, Chunk.file_id == KnowledgeFile.id)  # type: ignore[arg-type]
        .where(KnowledgeFile.enabled == True)  # noqa: E712
    ).one() > 0


def _has_products(session) -> bool:
    return session.exec(select(func.count()).select_from(Product)).one() > 0


def build_tools(user: User, session) -> list[BaseTool]:
    tools: list[BaseTool] = []
    try:
        if _knowledge_ready(session):
            from .rag_tools import rag_search

            tools.append(rag_search)
            if _has_products(session):
                from .rag_tools import query_product

                tools.append(query_product)
    except Exception as e:  # 知识库未就绪/异常不阻断对话
        logger.warning("RAG 工具未启用: {}", e)

    try:  # ---- P2: 长期记忆 ----
        from .memory_tools import build_memory_tools

        tools.extend(build_memory_tools(user, session))
    except Exception as e:
        logger.debug("记忆工具未启用: {}", e)

    try:  # ---- P3: 转人工 ----
        from .handoff_tools import request_handoff

        tools.append(request_handoff)
    except Exception as e:
        logger.debug("转人工工具未启用: {}", e)

    return tools


async def build_tools_async(user: User, session) -> list[BaseTool]:
    """P4：MCP 工具发现是异步的，对话循环使用本入口。"""
    tools = build_tools(user, session)
    try:
        from app.services.mcp.manager import get_mcp_tools

        tools.extend(await get_mcp_tools())
    except Exception as e:
        logger.debug("MCP 工具未启用: {}", e)
    return tools
