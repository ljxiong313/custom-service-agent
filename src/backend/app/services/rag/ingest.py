"""知识文件入库编排：解析 → 分块 → 写 Chunk 表 → 向量化入 Chroma → 商品表导入。

上传与管理页的「重新入库」「重建向量」都走 services/knowledge_jobs 的任务线程，
本模块只被任务线程与离线脚本调用，逻辑不分叉。
失败处理：任何一步异常 → status=failed + error 文案（前端可见），已写入的
chunk/向量整体回滚删除，保证不残留半成品。向量化单独失败只降级（error 提示，
BM25 仍可用），不推翻已入库的切片。
向量化降级的口径（B1-9）：批级重试后仍失败的切片**不静默丢弃**——其余批次照常被向量化，
最后汇总成一条带"失败批次/受影响切片"的错误写进 KnowledgeFile.error（上传/重入库）或
IngestJob.error（由任务层从 file.error 透传；重建向量路径则直接 failed 落 IngestJob.error）。

BM25 侧：本模块是 Chunk 表的唯一写路径，每次改变语料集合后都调 text_index.invalidate()
丢掉进程内索引；跨进程/重启的正确性由 bm25_store 的语料版本标记兜住（见那两个模块的注释）。
"""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from loguru import logger
from sqlmodel import Session, col, select

from app.db.models import Chunk, IngestJob, KnowledgeFile, Product, pk

from . import parser, text_index, vectorstore
from .chunker import chunk_text
from .parser import ProductRow


def _index_dirty() -> None:
    """语料集合变了：丢弃进程内 BM25 索引（下一次检索按新版本重建）。"""
    text_index.invalidate()


def _restore_vector_enabled(file_id: int, enabled: bool) -> None:
    """把该文件向量的 enabled 元数据还原成文件状态。

    add_chunks 写死的 metadata 是 enabled=True（重新入库时向量本来就是这个状态），
    部分批次向量化失败时也必须还原，否则"禁用文件"的切片会被向量路召回。
    """
    try:
        vectorstore.set_file_enabled(file_id, enabled)
    except Exception as e:  # noqa: BLE001 - 元数据还原失败只是展示/召回口径问题，不能盖掉主错误
        logger.warning("向量 enabled 还原失败（file#{}）: {}", file_id, e)


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
    _index_dirty()
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
            except Exception as e:
                # 显式降级（旧行为同款提示，但带上失败批次/受影响切片，运维才知道要补多少）
                degrade_note = f"向量化失败（仅关键词检索可用）: {e}"[:500]
                logger.warning("知识库向量化降级: {}", degrade_note)
            finally:
                _restore_vector_enabled(file_id, file.enabled)

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
    _index_dirty()


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
    _index_dirty()


def purge(session: Session, file: KnowledgeFile) -> tuple[list[int], int]:
    """清空一个文件的检索产物（chunk 表 + 向量 + 该文件导入的商品行），保留文件本身。

    重新入库前的清理：解析器/分块器改进后旧切片不会自己变，必须先清再重建，
    否则新旧切片同时被召回（回答里混入重复内容）。

    返回被删的 chunk id 列表与商品行数：调用方拿数量只够写日志，真要核对
    "旧产物清干净了没有"必须用 id 列表。BM25 侧不再"每次现取现建"，所以清完必须
    显式失效（下面 _index_dirty）；跨进程还有语料版本标记兜底，检索结果不会拿到旧切片。
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
    _index_dirty()
    return ids, len(products)


def reindex_file(session: Session, file: KnowledgeFile,
                 progress: Callable[[int, int], None] | None = None) -> int:
    """只重建向量（以 Chunk 表为准），不动切片与商品行。

    适用：换 embedding 模型（向量空间整个变了）、或上传时向量化失败只落了 BM25 后补建。
    同 id 先删后写，重复调用幂等。

    失败口径：批级重试后仍有批次失败时抛 EmbeddingBatchError（错误文案含失败批次与受影响
    切片），任务层据此把 IngestJob.error / KnowledgeFile.error 写成可见的失败，而不是
    "看起来成功了但一半切片没向量"。BM25 语料未变，无需失效。
    """
    file_id = pk(file)
    chunks = session.exec(
        select(Chunk).where(Chunk.file_id == file_id).order_by(col(Chunk.id))).all()
    if not chunks:
        return 0
    vectorstore.remove_chunks([pk(c) for c in chunks])
    try:
        vectorstore.add_chunks([(pk(c), c.text, file_id, file.name) for c in chunks],
                               progress=progress)
    finally:
        _restore_vector_enabled(file_id, file.enabled)
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
