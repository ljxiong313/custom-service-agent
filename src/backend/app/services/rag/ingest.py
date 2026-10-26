"""知识文件入库编排：解析 → 分块 → 写 Chunk 表 → 向量化入 Chroma → 商品表导入。

上传与管理页的「重新入库」「重建向量」都走 services/knowledge_jobs 的任务线程，
本模块只被任务线程与离线脚本调用，逻辑不分叉。
失败处理：任何一步异常 → status=failed + error 文案（前端可见），已写入的
chunk/向量整体回滚删除，保证不残留半成品。向量化单独失败只降级（error 提示，
BM25 仍可用），不推翻已入库的切片。
"""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from loguru import logger
from sqlmodel import Session, col, select

from app.db.models import Chunk, IngestJob, KnowledgeFile, Product, pk

from . import parser, vectorstore
from .chunker import chunk_text
from .parser import ProductRow


def ingest_file(session: Session, file: KnowledgeFile, *, purge_first: bool = False,
                progress: Callable[[int, int], None] | None = None) -> None:
    """入库一个文件。purge_first=True 时先清旧产物（重新入库）。

    清理放在任务内部而不是接口里：接口里先 purge 会让"任务排队中/失败"的
    文件在库里变成 0 chunk，检索当场失去这个语料。
    """
    file.status = "parsing"
    file.error = None
    session.add(file)
    session.commit()
    if purge_first:
        purge(session, file)
    file_id = pk(file)      # 已落库文件的 int 主键（Chunk/Product/向量 metadata 都存它）

    created_chunk_ids: list[int] = []
    try:
        parsed = parser.parse(Path(file.path), file.file_type)
        texts = chunk_text(parsed.text)
        for seq, text in enumerate(texts):
            c = Chunk(file_id=file_id, seq=seq, text=text,
                      tokens=len(text) // 2)  # 粗略 token 计数，仅用于展示
            session.add(c)
            session.commit()
            session.refresh(c)
            created_chunk_ids.append(pk(c))
        # 向量化失败不阻断入库：chunk 已落库，BM25 关键词检索仍可用（error 字段提示降级）
        degrade_note = None
        if texts:
            try:
                # 向量 id 必须用 Chunk 主键：早期版本误用列表下标，导致向量与 chunk 错位
                vectorstore.add_chunks([(cid, texts[i], file_id, file.name)
                                        for i, cid in enumerate(created_chunk_ids)],
                                       progress=progress)
                # add_chunks 写入的 metadata 恒为 enabled=True，重新入库禁用的文件要还原
                vectorstore.set_file_enabled(file_id, file.enabled)
            except Exception as e:
                degrade_note = f"向量化失败（仅关键词检索可用）: {e}"[:300]
                logger.warning("知识库向量化降级: {}", degrade_note)

        for prow in parsed.products:
            _upsert_product(session, prow, file_id)

        file.status = "ready"
        file.chunk_count = len(texts)
        file.error = degrade_note
        logger.info("知识库入库完成: {}（{} chunks, {} products）",
                    file.name, len(texts), len(parsed.products))
    except Exception as e:
        logger.exception("知识库入库失败: {}", file.name)
        # 回滚：删向量与 chunk 行
        vectorstore.remove_chunks(created_chunk_ids)
        for cid in created_chunk_ids:
            row = session.get(Chunk, cid)
            if row:
                session.delete(row)
        session.commit()
        file.status = "failed"
        file.error = str(e)[:500]
    session.add(file)
    session.commit()


def remove_file(session: Session, file: KnowledgeFile) -> None:
    """删除文件的全部 chunk、向量、商品行与任务记录。"""
    ids = [pk(c) for c in session.exec(
        select(Chunk).where(Chunk.file_id == file.id)).all()]
    vectorstore.remove_chunks(ids)
    for cid in ids:
        row = session.get(Chunk, cid)
        if row:
            session.delete(row)
    for p in session.exec(select(Product).where(Product.file_id == file.id)).all():
        session.delete(p)
    # 任务行也一起删：IngestJob.file_id 有外键，开了 PRAGMA foreign_keys 后留着会挡住删除
    for j in session.exec(select(IngestJob).where(IngestJob.file_id == file.id)).all():
        session.delete(j)
    session.delete(file)
    session.commit()


def purge(session: Session, file: KnowledgeFile) -> tuple[list[int], int]:
    """清空一个文件的检索产物（chunk 表 + 向量 + 该文件导入的商品行），保留文件本身。

    重新入库前的清理：解析器/分块器改进后旧切片不会自己变，必须先清再重建，
    否则新旧切片同时被召回（回答里混入重复内容）。

    返回被删的 chunk id 列表与商品行数：BM25 每次检索从 Chunk 表现取现建（见
    text_index.py），所以清完即生效、不需要额外失效通知；调用方拿数量只够写日志，
    真要核对"旧产物清干净了没有"必须用 id 列表。
    """
    chunks = session.exec(select(Chunk).where(Chunk.file_id == file.id)).all()
    ids = [pk(c) for c in chunks]
    vectorstore.remove_chunks(ids)
    for c in chunks:
        session.delete(c)
    products = session.exec(select(Product).where(Product.file_id == file.id)).all()
    for p in products:
        session.delete(p)
    file.chunk_count = 0
    session.add(file)
    session.commit()
    return ids, len(products)


def reindex_file(session: Session, file: KnowledgeFile,
                 progress: Callable[[int, int], None] | None = None) -> int:
    """只重建向量（以 Chunk 表为准），不动切片与商品行。

    适用：换 embedding 模型（向量空间整个变了）、或上传时向量化失败只落了 BM25 后补建。
    同 id 先删后写，重复调用幂等。
    """
    file_id = pk(file)
    chunks = session.exec(
        select(Chunk).where(Chunk.file_id == file_id).order_by(col(Chunk.id))).all()
    if not chunks:
        return 0
    vectorstore.remove_chunks([pk(c) for c in chunks])
    vectorstore.add_chunks([(pk(c), c.text, file_id, file.name) for c in chunks],
                           progress=progress)
    vectorstore.set_file_enabled(file_id, file.enabled)
    if file.error and "向量化失败" in file.error:
        file.error = None
        session.add(file)
        session.commit()
    return len(chunks)


def _upsert_product(session: Session, row: ProductRow, file_id: int) -> None:
    """按 (sku, name) 幂等：重复导入同表覆盖更新而非追加。"""
    exist = session.exec(select(Product).where(
        Product.sku == row.sku, Product.name == row.name, Product.file_id == file_id)).first()
    target = exist or Product(sku=row.sku, name=row.name, file_id=file_id)
    target.price, target.stock = row.price, row.stock
    target.category, target.specs, target.tags = row.category, row.specs, row.tags
    session.add(target)
    session.commit()
