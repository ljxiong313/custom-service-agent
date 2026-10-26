"""知识库管理接口：上传入库 / 列表 / 启停 / 删除 / 重新入库 / 重建向量 / 分块浏览 / 任务进度 / 检索测试。"""
from __future__ import annotations

import re
import uuid
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, UploadFile, status
from loguru import logger
from pydantic import BaseModel
from sqlalchemy import ColumnElement, func
from sqlmodel import Session, col, select

from app.config import BACKEND_ROOT
from app.core.security import CurrentUser, DbSession, RequireAdmin
from app.db.models import Chunk, KnowledgeFile, Product, pk
from app.services import knowledge_jobs
from app.services.rag import handler, ingest, parser, vectorstore

router = APIRouter(prefix="/knowledge", tags=["knowledge"])

UPLOAD_DIR = BACKEND_ROOT / "data" / "uploads"
ALLOWED_EXT = {".xlsx", ".xls", ".csv", ".pdf", ".png", ".jpg", ".jpeg", ".webp",
               ".bmp", ".txt", ".md", ".docx"}
MAX_SIZE = 20 * 1024 * 1024  # 20MB


@router.post("/files", response_model=KnowledgeFile)
async def upload_file(user: RequireAdmin, session: DbSession,
                      file: UploadFile = File(...)):
    """上传入库：这里只落盘并排队，解析/分块/向量化在任务线程里做（B1-2）。

    图片需要视觉模型 key；限 admin 是因为入库直接改写全局检索语料，普通坐席只读。
    """
    name = Path(file.filename or "unnamed").name
    ext = Path(name).suffix.lower()
    if ext not in ALLOWED_EXT:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            f"不支持的文件类型 {ext}，可用: {sorted(ALLOWED_EXT)}")
    content = await file.read()
    if len(content) > MAX_SIZE:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "文件超过 20MB 限制")

    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^\w.\-]+", "_", name)[:60]
    path = UPLOAD_DIR / f"{uuid.uuid4().hex[:8]}_{safe}"
    path.write_bytes(content)

    kf = KnowledgeFile(name=name, path=str(path),
                       file_type=parser.detect_type(Path(name)),
                       size=len(content), status="parsing")
    session.add(kf)
    session.commit()
    session.refresh(kf)
    job = knowledge_jobs.create_job(session, kf, knowledge_jobs.KIND_UPLOAD)
    knowledge_jobs.enqueue(job.id or 0)
    # create_job 内部 commit 会 expire kf，而 FastAPI 在请求会话关闭之后才序列化响应，
    # 不 refresh 就直接返回会得到空对象（前端拿不到 file_id，也就看不到进度行）。
    session.refresh(kf)
    return kf


def _get_file(session: Session, file_id: int) -> KnowledgeFile:
    kf = session.get(KnowledgeFile, file_id)
    if not kf:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "文件不存在")
    return kf


def _reject_busy(kf: KnowledgeFile, session: Session) -> None:
    """同一文件同时跑两个重建 = 同一批 chunk 的向量写两遍、商品行翻倍。

    判据用 IngestJob 里 queued/running 的行，而不是文件 status=parsing：
    后者是进程内线程留下的标记，重启后没有线程在跑却永远显示 parsing（B1-4 卡死）。
    """
    if kf.id in knowledge_jobs.active_job_ids(session):
        raise HTTPException(status.HTTP_409_CONFLICT, "该文件正在入库/重建中，请等待完成后再操作")
    if kf.status == "parsing":
        # 没有活动任务却停在 parsing：上一次进程被打断留下的死状态。先按任务行回收；
        # recover 只认有 IngestJob 行的任务（旧数据、建任务前崩溃都碰不到），所以这里兜底
        # 把文件本身置 failed——否则同一个"请重试"会永远重试不动，删除/重入库全被 409 挡住。
        knowledge_jobs.recover_interrupted_jobs()
        session.refresh(kf)
        if kf.status == "parsing":
            kf.status = "failed"
            kf.error = "上次入库/重建被进程重启打断，请重新发起"
            session.add(kf)
            session.commit()
            session.refresh(kf)
        raise HTTPException(status.HTTP_409_CONFLICT, "上次任务被进程重启打断，状态已回收，请重试本次操作")


@router.get("/files", response_model=list[KnowledgeFile])
def list_files(user: CurrentUser, session: DbSession):
    return session.exec(select(KnowledgeFile).order_by(KnowledgeFile.id.desc())).all()  # type: ignore[union-attr]


class EnabledBody(BaseModel):
    enabled: bool


@router.patch("/files/{file_id}", response_model=KnowledgeFile)
def set_enabled(file_id: int, body: EnabledBody, user: RequireAdmin, session: DbSession):
    kf = _get_file(session, file_id)
    kf.enabled = body.enabled
    session.add(kf)
    session.commit()
    session.refresh(kf)
    vectorstore.set_file_enabled(file_id, body.enabled)
    return kf


@router.delete("/files/{file_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_file(file_id: int, user: RequireAdmin, session: DbSession):
    kf = _get_file(session, file_id)
    _reject_busy(kf, session)  # 入库中途删除会留下指向已删文件的孤儿 chunk
    path = Path(kf.path)
    ingest.remove_file(session, kf)
    # 磁盘文件最后删：先 unlink 再 commit 时，commit 失败就变成"库里还在、文件没了"的坏行
    path.unlink(missing_ok=True)


@router.get("/jobs")
def list_jobs(user: CurrentUser, session: DbSession, limit: int = 30):
    """重建任务列表（进度/失败原因）。上传也走这里，页面据此显示"已排队/入库中 x/y"。"""
    return {"pending": knowledge_jobs.pending_count(session),
            "items": knowledge_jobs.recent_jobs(session, max(1, min(limit, 100)))}


@router.post("/jobs/{job_id}/retry", response_model=KnowledgeFile)
def retry_job(job_id: int, user: RequireAdmin, session: DbSession):
    """重试一个 interrupted/failed 任务（进程重启或向量失败后的补救入口）。"""
    from app.db.models import IngestJob

    job = session.get(IngestJob, job_id)
    if not job:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "任务不存在")
    if job.status not in ("interrupted", "failed"):
        raise HTTPException(status.HTTP_409_CONFLICT, "只有中断或失败的任务可以重试")
    kf = _get_file(session, job.file_id)
    _reject_busy(kf, session)
    if job.kind != knowledge_jobs.KIND_REINDEX and not Path(kf.path).exists():
        raise HTTPException(status.HTTP_409_CONFLICT, "原始文件已不在磁盘上，请重新上传")
    job.status = "queued"
    job.error = None
    job.processed = 0
    session.add(job)
    session.commit()
    knowledge_jobs.enqueue(job.id or 0)
    session.refresh(kf)
    return kf


@router.post("/files/{file_id}/reingest", response_model=KnowledgeFile)
def reingest_file(file_id: int, user: RequireAdmin, session: DbSession):
    """重新入库：排队一个任务，由任务线程清掉旧 chunk/向量/商品行后从头重建。

    解析器改动后旧切片不会自己变，failed/interrupted 文件也要能重试——不删原文件，
    所以上传时拿到的 file_id 与历史引用保持不变。
    清理放在任务里而不是这里：接口先 purge 会让"排队中/失败"的文件当场失去语料。
    """
    kf = _get_file(session, file_id)
    _reject_busy(kf, session)
    if not Path(kf.path).exists():
        raise HTTPException(status.HTTP_409_CONFLICT, "原始文件已不在磁盘上，请重新上传")
    kf.status = "pending"
    session.add(kf)
    session.commit()
    session.refresh(kf)
    job = knowledge_jobs.create_job(session, kf, knowledge_jobs.KIND_REINGEST,
                                    total=kf.chunk_count)
    knowledge_jobs.enqueue(job.id or 0)
    logger.info("知识库重新入库排队: job#{} file#{} {}", job.id, kf.id, kf.name)
    session.refresh(kf)   # 同上传接口：create_job 的 commit 会 expire kf
    return kf


@router.post("/files/{file_id}/reindex", response_model=KnowledgeFile)
def reindex_vectors(file_id: int, user: RequireAdmin, session: DbSession):
    """只补向量，不重跑解析/分块（换 embedding 模型、或向量化失败降级后修复）。"""
    kf = _get_file(session, file_id)
    _reject_busy(kf, session)
    n = session.exec(select(func.count()).select_from(Chunk).where(Chunk.file_id == file_id)).one()
    if not n:
        raise HTTPException(status.HTTP_409_CONFLICT, "该文件还没有已入库的文本分块，请先「重新入库」")
    job = knowledge_jobs.create_job(session, kf, knowledge_jobs.KIND_REINDEX, total=int(n))
    knowledge_jobs.enqueue(job.id or 0)
    session.refresh(kf)
    return kf


@router.get("/files/{file_id}/chunks")
def list_chunks(file_id: int, user: CurrentUser, session: DbSession,
                limit: int = 50, offset: int = 0):
    """分块浏览：查"切片到底切成了什么样子"，不用再去翻 Chroma 和数据库。

    vectorized 标记该 chunk 在 Chroma 里是否真有向量——false 即向量化降级（仅关键词召回）。
    """
    kf = _get_file(session, file_id)
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    total = session.exec(
        select(func.count()).select_from(Chunk).where(Chunk.file_id == file_id)).one()
    rows = session.exec(
        select(Chunk).where(Chunk.file_id == file_id)
        .order_by(col(Chunk.seq)).offset(offset).limit(limit)).all()
    have = vectorstore.has_chunks([pk(c) for c in rows])
    return {
        "file_id": kf.id, "file_name": kf.name, "status": kf.status,
        "total": int(total), "limit": limit, "offset": offset,
        "items": [{"chunk_id": c.id, "seq": c.seq, "tokens": c.tokens,
                   "chars": len(c.text), "vectorized": c.id in have, "text": c.text}
                  for c in rows],
    }


class SearchBody(BaseModel):
    query: str
    top_k: int | None = None


@router.post("/search")
def search(body: SearchBody, user: CurrentUser, session: DbSession):
    """检索测试：与管理页共用 handler.retrieve，所见即 Agent 所得。"""
    results = handler.retrieve(session, body.query, body.top_k)
    return [{"chunk_id": r.chunk_id, "file_id": r.file_id, "file_name": r.file_name,
             "score": round(r.score, 4), "text": r.text[:300]} for r in results]


@router.get("/products")
def list_products(user: CurrentUser, session: DbSession, q: str | None = None,
                  file_id: int | None = None, limit: int = 50, offset: int = 0):
    """结构化商品浏览（Excel 导入产物）。

    q 按名称/SKU 模糊过滤，file_id 只看某个文件的导入结果。
    分页返回 {items,total,limit,offset}——total 是过滤后的总数，
    前端据此翻页；单页上限 200（旧版硬截 200 条，超出的商品行既看不到也数不清）。
    """
    conds: list[ColumnElement[bool]] = []
    if q:
        like = f"%{q}%"
        conds.append(col(Product.name).like(like) | col(Product.sku).like(like))
    if file_id is not None:
        conds.append(col(Product.file_id) == file_id)
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    total = session.exec(select(func.count()).select_from(Product).where(*conds)).one()
    rows = session.exec(
        select(Product).where(*conds).order_by(col(Product.id)).offset(offset).limit(limit)
    ).all()
    return {"total": int(total), "limit": limit, "offset": offset, "items": rows}
