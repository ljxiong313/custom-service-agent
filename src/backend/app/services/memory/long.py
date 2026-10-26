"""长期记忆：跨会话的用户画像事实。

写入：pipeline 在对话结束后让 LLM 从最近对话抽取"稳定事实"（偏好/身份/待办结论），
去重后存 MemoryFact 表 + Chroma collection "user_memory"（id 一致，metadata 带 user_id）。
读取：recall_memory 工具按需向量召回当前用户的事实（只查本人，防串号）。
管理：记忆管理页可浏览/删除（删除同时清向量）。
"""
from __future__ import annotations

import json

from loguru import logger
from sqlmodel import Session, col, select

from app.db.models import MemoryFact, Message


def _collection():
    import chromadb

    from app.config import BACKEND_ROOT, get_settings

    # 同 vectorstore：yaml 里 chroma_path: "" 表示"用默认目录"，get_dotted 不会替我们回落
    path = get_settings().get_dotted("rag.chroma_path", "") or str(BACKEND_ROOT / "data" / "chroma")
    return chromadb.PersistentClient(path=path).get_or_create_collection(
        "user_memory", metadata={"hnsw:space": "cosine"})


def extract_facts(session: Session, conversation_id: int, user_id: int) -> int:
    """从会话最近 30 条消息抽取用户事实，返回新增条数。"""
    msgs = list(session.exec(
        select(Message).where(Message.conversation_id == conversation_id)
        .order_by(col(Message.id).desc()).limit(30)
    ).all())[::-1]
    if len(msgs) < 2:
        return 0

    from app.core.models.manager import get_chat_model

    transcript = "\n".join(f"{'用户' if m.role == 'user' else '客服'}: {m.content[:200]}" for m in msgs)
    prompt = (
        "从以下客服对话中提取关于【用户本人】的长期有效事实"
        "（偏好、预算、身份特征、购买意向、重要结论），忽略一次性寒暄与商品本身的信息。\n"
        '只输出 JSON 数组，如 ["用户偏好性价比高的国产品牌面霜"]；没有则输出 []。\n\n' + transcript)
    try:
        resp = get_chat_model().invoke(prompt)
        text = (resp.text if hasattr(resp, "text") else str(resp.content)).strip()
        if text.startswith("```"):
            text = text.strip("`").removeprefix("json").strip()
        facts = json.loads(text)
        assert isinstance(facts, list)
    except Exception as e:
        logger.warning("长期记忆抽取失败（跳过）: {}", e)
        return 0

    existing = {f.fact for f in session.exec(
        select(MemoryFact).where(MemoryFact.user_id == user_id)).all()}
    added = 0
    for fact in facts:
        fact = str(fact).strip()
        if not fact or fact in existing:
            continue
        row = MemoryFact(user_id=user_id, fact=fact, source_conversation_id=conversation_id)
        session.add(row)
        session.commit()
        session.refresh(row)
        _index(row)
        added += 1
    if added:
        logger.info("长期记忆: user={} 新增 {} 条事实", user_id, added)
    return added


def _index(fact: MemoryFact) -> None:
    from app.core.models.manager import get_embeddings

    _collection().add(ids=[str(fact.id)], documents=[fact.fact],
                      embeddings=get_embeddings().embed_documents([fact.fact]),
                      metadatas=[{"user_id": fact.user_id}])


def remove_fact(session: Session, fact: MemoryFact) -> None:
    try:
        _collection().delete(ids=[str(fact.id)])
    except Exception as e:
        logger.warning("删除记忆向量失败（行仍删除）: {}", e)
    session.delete(fact)
    session.commit()


def recall(user_id: int, query: str, top_k: int = 5) -> list[str]:
    col = _collection()
    if col.count() == 0:
        return []
    from app.core.models.manager import get_embeddings

    res = col.query(query_embeddings=get_embeddings().embed_documents([query]),
                    n_results=min(top_k, col.count()),
                    where={"user_id": user_id})
    return list(res["documents"][0])
