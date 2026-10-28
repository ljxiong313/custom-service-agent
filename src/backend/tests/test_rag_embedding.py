"""入库吞吐与失败重试的单测（全程离线：不打模型）。

口径：
- 批大小由配置 rag.embedding_batch_size 决定，默认 10、硬上限 20（百炼兼容模式），
  超上限夹紧并只 warn 一次；
- 每批失败按 rerank 已有的重试语义退避重试（测试里把重试间隔置 0，只验"重试发生没有"）；
- 重试仍失败不静默跳过：其余批次照常被向量化，最后汇总成 EmbeddingBatchError，
  由 ingest/reindex 把可读文案落进 KnowledgeFile.error / IngestJob.error。
用 monkeypatch 替掉 manager.get_embeddings（vectorstore._embed 调用时才 import，
所以替模块属性即生效），按次数/条数断言，不靠计时。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from loguru import logger
from sqlmodel import Session, select

from app.config import get_settings
from app.db.models import Chunk, IngestJob, KnowledgeFile, pk
from app.db.session import engine
from app.services import knowledge_jobs
from app.services.rag import handler, ingest, text_index, vectorstore

DIM = 6


class _Missing:
    pass


_MISSING = _Missing()


def _always_fail(attempt: int, n: int) -> None:
    raise RuntimeError("embedding 服务不可用")


def _fail_network(attempt: int, n: int) -> None:
    raise RuntimeError("网络不可达")


def _fail_query(attempt: int, n: int) -> None:
    raise RuntimeError("embedding 挂了")


class FakeEmbeddings:
    """替身 embedding：按 policy 决定第几次调用失败，并记录每次请求的条数。"""

    def __init__(self, policy: Any = None) -> None:
        self.sizes: list[int] = []
        self.attempts = 0
        self.policy = policy

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self.attempts += 1
        self.sizes.append(len(texts))
        if self.policy is not None:
            self.policy(self.attempts, len(texts))     # 抛异常即"这批失败"
        return [[(len(t) % 7 + i) / 10 + 0.1 for i in range(DIM)] for t in texts]


@pytest.fixture
def chroma_tmp(tmp_path, monkeypatch):
    """独立的临时 Chroma + 临时 BM25 缓存目录（测试不碰仓库的 data/）+ 零重试等待。

    把 EMBED_RETRY_DELAY_SECONDS 置 0 只影响速度：重试**次数**仍由 fake 的调用计数证明。
    """
    rag = dict(get_settings().get("rag") or {})
    rag["chroma_path"] = str(tmp_path / "chroma")
    rag["bm25_index_path"] = str(tmp_path / "bm25" / "knowledge.pkl.gz")
    rag["embedding_batch_size"] = 10
    monkeypatch.setitem(get_settings(), "rag", rag)
    monkeypatch.setattr(vectorstore, "EMBED_RETRY_DELAY_SECONDS", 0.0)
    vectorstore.reset_collection_cache()
    text_index.reset_cache()
    yield tmp_path
    vectorstore.reset_collection_cache()
    text_index.reset_cache()


@pytest.fixture
def fake_embeddings(monkeypatch):
    """替掉 manager.get_embeddings，返回可断言的 FakeEmbeddings 实例。"""
    def apply(policy: Any = None) -> FakeEmbeddings:
        fake = FakeEmbeddings(policy)
        import app.core.models.manager as manager

        monkeypatch.setattr(manager, "get_embeddings", lambda name=None: fake)
        return fake

    return apply


def _set_batch(monkeypatch, value: object) -> None:
    rag = dict(get_settings().get("rag") or {})
    if isinstance(value, _Missing):
        rag.pop("embedding_batch_size", None)
    else:
        rag["embedding_batch_size"] = value
    monkeypatch.setitem(get_settings(), "rag", rag)


def _chunks(n: int) -> list[tuple[int, str, int, str]]:
    return [(i + 1, f"商品「条目{i}」，价格:{i}元", 1, "doc.txt") for i in range(n)]


# ---- 批大小：配置化 + 夹紧 ----

@pytest.mark.parametrize("raw,expected", [
    (_MISSING, 10),
    (None, 10),
    ("", 10),
    ("12", 12),
    (15.0, 15),
    (20, 20),
    (21, 20),
    (500, 20),
    (0, 1),
    (-3, 1),
    ("abc", 10),
])
def test_embedding_batch_size_default_and_clamp(monkeypatch, raw, expected):
    _set_batch(monkeypatch, raw)
    assert vectorstore.embedding_batch_size() == expected


def test_batch_size_clamp_warns_only_once(monkeypatch):
    _set_batch(monkeypatch, 500)
    monkeypatch.setattr(vectorstore, "_BATCH_WARNED", False)
    records: list[str] = []
    sink = logger.add(records.append, level="WARNING")
    try:
        assert vectorstore.embedding_batch_size() == 20
        assert vectorstore.embedding_batch_size() == 20
        assert vectorstore.embedding_batch_size() == 20
    finally:
        logger.remove(sink)
    hits = [r for r in records if "embedding_batch_size" in r]
    assert len(hits) == 1, records                     # 启动时提示一次，不每批刷屏


def test_add_chunks_respects_configured_batch(chroma_tmp, fake_embeddings, monkeypatch):
    _set_batch(monkeypatch, 7)
    fake = fake_embeddings()
    vectorstore.add_chunks(_chunks(25))
    assert fake.sizes == [7, 7, 7, 4]                  # 代码里不再写死 10
    assert len(vectorstore.has_chunks([c[0] for c in _chunks(25)])) == 25


def test_add_chunks_defaults_to_ten_not_twenty(chroma_tmp, fake_embeddings, monkeypatch):
    _set_batch(monkeypatch, _MISSING)
    fake = fake_embeddings()
    vectorstore.add_chunks(_chunks(10))
    assert fake.sizes == [10]                          # 10 条一批不会顶到 20 的上限


# ---- 批级重试 ----

def test_flaky_batch_is_retried_and_completes(chroma_tmp, fake_embeddings, monkeypatch):
    _set_batch(monkeypatch, 10)

    def fail_first_time(attempt: int, _n: int) -> None:
        if attempt == 1:
            raise RuntimeError("Connect timeout")

    fake = fake_embeddings(fail_first_time)
    vectorstore.add_chunks(_chunks(12))                # 2 批
    assert fake.attempts == 3                          # 第 1 批：失败一次 + 重试一次
    assert fake.sizes == [10, 10, 2]                   # 重试的是同一批（不是整文件重来）
    assert len(vectorstore.has_chunks(list(range(1, 13)))) == 12


def test_persistent_failure_raises_batch_error_but_keeps_other_batches(
        chroma_tmp, fake_embeddings, monkeypatch):
    _set_batch(monkeypatch, 10)
    attempts_seen: list[int] = []

    def fail_middle_batch(attempt: int, _n: int) -> None:
        attempts_seen.append(attempt)
        # 第 2 批的两端（11、12 次调用）都失败，其余成功
        if attempt in (2, 3):
            raise RuntimeError("batch size is invalid")

    fake = fake_embeddings(fail_middle_batch)
    seen: list[tuple[int, int]] = []
    with pytest.raises(vectorstore.EmbeddingBatchError) as exc:
        vectorstore.add_chunks(_chunks(25), progress=lambda p, t: seen.append((p, t)))

    err = exc.value
    assert err.failed_batches == [2] and err.total_batches == 3
    assert err.failed_chunk_ids == list(range(11, 21))
    assert "向量化失败批次 [2]/3" in str(err) and "batch size is invalid" in str(err)
    assert fake.attempts == 4                          # 第 1/3 批各一次，第 2 批两次（重试一次）
    # 其余批次的向量照常写入：失败不扩散，但必须显式报告
    assert len(vectorstore.has_chunks(list(range(1, 26)))) == 15
    assert seen[-1] == (25, 25)                        # 失败批次也推进进度，否则页面看着像卡死


def test_progress_counts_all_chunks_when_everything_succeeds(chroma_tmp, fake_embeddings,
                                                             monkeypatch):
    _set_batch(monkeypatch, 4)
    fake_embeddings()
    seen: list[tuple[int, int]] = []
    vectorstore.add_chunks(_chunks(10), progress=lambda p, t: seen.append((p, t)))
    assert seen == [(4, 10), (8, 10), (10, 10)]


# ---- 端到端：error 字段可读、状态正确、检索仍降级可用 ----

def _make_source(tmp_path: Path) -> Path:
    path = tmp_path / "报价.txt"
    path.write_text("\n".join(f"商品「面霜 {i}」，价格:{i}元，库存:{i}件" for i in range(30)),
                    encoding="utf-8")
    return path


def _register(db: Session, path: Path, *, enabled: bool = True) -> KnowledgeFile:
    kf = KnowledgeFile(name=path.name, path=str(path), file_type="text",
                       size=path.stat().st_size, status="pending", enabled=enabled)
    db.add(kf)
    db.commit()
    db.refresh(kf)
    return kf


def test_ingest_file_marks_degrade_but_stays_ready(db, chroma_tmp, fake_embeddings,
                                                  monkeypatch, tmp_path):
    """上传时向量化全挂：切片照旧入库（BM25 可用），error 写清"哪几批、哪些切片"。"""
    _set_batch(monkeypatch, 10)
    fake = fake_embeddings(_always_fail)
    path = _make_source(tmp_path)
    kf = _register(db, path)

    ingest.ingest_file(db, kf)

    assert kf.status == "ready"
    assert kf.chunk_count > 0 and fake.attempts >= 1
    assert kf.error is not None and kf.error.startswith("向量化失败（仅关键词检索可用）")
    assert "向量化失败批次" in kf.error and "embedding 服务不可用" in kf.error
    assert db.exec(select(Chunk).where(Chunk.file_id == pk(kf))).all()          # 切片没被回滚
    assert vectorstore.has_chunks([pk(c) for c in db.exec(
        select(Chunk).where(Chunk.file_id == pk(kf))).all()]) == set()          # 确实没有向量

    hits = handler.retrieve(db, "面霜 价格")            # 降级路径整体仍可用
    assert hits and "面霜" in hits[0].text


def test_reindex_failure_lands_in_job_and_file_error(db, chroma_tmp, fake_embeddings,
                                                     monkeypatch, tmp_path):
    """重建向量最终失败：IngestJob.error 与 KnowledgeFile.error 都可读、status=failed。"""
    _set_batch(monkeypatch, 10)
    path = _make_source(tmp_path)
    kf = _register(db, path)
    for seq in range(3):
        db.add(Chunk(file_id=pk(kf), seq=seq, text=f"商品「面霜 {seq}」，价格:39元", tokens=10))
    db.commit()

    fake = fake_embeddings(_fail_network)
    job = knowledge_jobs.create_job(db, kf, knowledge_jobs.KIND_REINDEX, total=3)

    knowledge_jobs._run_job(pk(job))                   # 同步执行任务体，避免线程等待

    assert fake.attempts == 2                          # 一批三片：一次 + 重试一次
    with Session(engine) as s:
        reloaded = s.get(IngestJob, pk(job))
        assert reloaded.status == "failed"
        assert reloaded.error and "向量化失败批次" in reloaded.error and "网络不可达" in reloaded.error
        kf2 = s.get(KnowledgeFile, pk(kf))
        assert kf2.status == "failed" and kf2.error and "向量化失败" in kf2.error


def test_reindex_clears_stale_degrade_note_on_success(db, chroma_tmp, fake_embeddings,
                                                     monkeypatch, tmp_path):
    _set_batch(monkeypatch, 10)
    path = _make_source(tmp_path)
    kf = _register(db, path)
    db.add(Chunk(file_id=pk(kf), seq=0, text="商品「面霜」，价格:39元", tokens=10))
    db.commit()
    kf.error = "向量化失败（仅关键词检索可用）: 旧的降级提示"
    db.add(kf)
    db.commit()

    fake = fake_embeddings()
    assert ingest.reindex_file(db, kf) == 1
    assert fake.sizes == [1]
    assert kf.error is None                            # 补建成功后降级提示要清掉
    assert vectorstore.has_chunks([pk(c) for c in db.exec(
        select(Chunk).where(Chunk.file_id == pk(kf))).all()])


def test_query_path_degrades_when_embedding_fails(db, chroma_tmp, fake_embeddings,
                                                  monkeypatch, tmp_path):
    """检索侧 embedding 挂掉不影响整体：向量路返回空，BM25 兜底（handler 已覆盖）。"""
    _set_batch(monkeypatch, 10)
    fake_embeddings(_fail_query)
    path = _make_source(tmp_path)
    kf = _register(db, path)
    db.add(Chunk(file_id=pk(kf), seq=0, text="商品「防晒霜 30ml」，价格:89元", tokens=10))
    db.commit()

    assert vectorstore.query("防晒霜") == []
    assert text_index.search(db, "防晒霜"), "BM25 必须继续可用"
