"""长期记忆工具：recall_memory（工厂闭包绑定当前用户，防串号）。"""
from __future__ import annotations

from langchain_core.tools import BaseTool, tool
from sqlmodel import func, select

from app.db.models import MemoryFact, User, pk


def build_memory_tools(user: User, session) -> list[BaseTool]:
    """仅当该用户已有长期记忆时才注册工具，避免空召回。"""
    count = session.exec(select(func.count()).select_from(MemoryFact)
                         .where(MemoryFact.user_id == user.id)).one()
    if not count:
        return []

    user_id = pk(user)

    @tool
    def recall_memory(query: str) -> str:
        """召回该用户的历史偏好与画像事实（预算、品牌偏好、过往咨询结论等）。

        用法：在推荐商品、回答个性化问题前调用，使回复贴合用户历史偏好；
        与本轮会话内容冲突时以本轮用户表述为准。
        """
        try:
            from app.services.memory.long import recall

            facts = recall(user_id, query)
            return "\n".join(f"- {f}" for f in facts) if facts else "没有相关历史记忆。"
        except Exception as e:
            return f"记忆召回暂不可用：{e}"

    return [recall_memory]
