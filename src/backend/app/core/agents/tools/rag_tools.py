"""RAG 相关 Agent 工具：rag_search（语义检索）与 query_product（结构化精确查询）。

约定（见 docs/工具规划.md）：
- 工具 docstring 面向 LLM 撰写：何时用、参数、返回格式；
- 工具失败返回可读错误字符串（模型可复述），不抛异常中断对话流；
- rag_search 命中后把引用写入请求级 collector，chat 层转成 SSE citation 事件。
"""
from __future__ import annotations

from contextvars import ContextVar

from langchain_core.tools import tool
from loguru import logger
from sqlmodel import select

from app.db.models import Product
from app.db.session import Session, engine  # 工具在请求流内自管会话

# 请求级引用收集器：chat.py 每个请求 set 一次，工具执行时 append
citation_collector: ContextVar[list[dict] | None] = ContextVar("citation_collector", default=None)


@tool
def rag_search(query: str) -> str:
    """检索商品知识库，获取商品介绍、使用说明、售后政策、活动规则等文档内容。

    用法：当用户询问商品的描述性信息（功能、用法、政策条款）且没有精确价格/库存
    数字要求时调用。返回若干条带编号的片段，回答时必须以 [编号] 标注引用的来源。
    若返回"未检索到"，请如实告知用户相关信息暂未录入知识库。
    """
    try:
        from app.services.rag.handler import retrieve

        with Session(engine) as session:
            results = retrieve(session, query)
        if not results:
            return "未检索到相关内容。"

        citations = citation_collector.get()
        lines = []
        for i, r in enumerate(results, 1):
            lines.append(f"[{i}] 来源《{r.file_name}》：{r.text}")
            if citations is not None:
                item = {"file_id": r.file_id, "file_name": r.file_name,
                        "chunk_id": r.chunk_id, "snippet": r.text[:120]}
                if item not in citations:
                    citations.append(item)
        return "\n\n".join(lines)
    except Exception as e:
        logger.warning("rag_search 失败: {}", e)
        return f"知识库检索暂时不可用：{e}"


@tool
def query_product(keyword: str) -> str:
    """按商品名称或 SKU 精确查询商品表，返回价格、库存、分类、规格等结构化信息。

    用法：用户询问"多少钱 / 有没有货 / 什么规格"等需要精确数字的问题时必须调用
    （不要用语义检索结果回答数字）。keyword 支持模糊匹配，如「面霜」「SKU001」。
    """
    try:
        with Session(engine) as session:
            like = f"%{keyword.strip()}%"
            rows = session.exec(
                select(Product)
                .where(Product.name.like(like) | Product.sku.like(like))  # type: ignore[attr-defined]
                .limit(10)
            ).all()
        if not rows:
            return f"商品表中未找到与「{keyword}」相关的商品。"
        lines = []
        for p in rows:
            parts = [f"名称:{p.name}"]
            if p.sku:
                parts.append(f"SKU:{p.sku}")
            parts.append(f"价格:{p.price}元" if p.price is not None else "价格:未标注")
            parts.append(f"库存:{p.stock}件" if p.stock is not None else "库存:未标注")
            if p.category:
                parts.append(f"分类:{p.category}")
            if p.specs:
                parts.append(f"规格:{p.specs}")
            lines.append("，".join(parts))
        return "\n".join(lines)
    except Exception as e:
        logger.warning("query_product 失败: {}", e)
        return f"商品查询暂时不可用：{e}"
