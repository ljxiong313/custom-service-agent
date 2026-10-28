"""BM25 索引持久化 + 版本标记失效的单测。

全程离线：conftest 的模型 key 留空，检索本来就是"向量路降级、BM25 兜底"的形态，
所以这些断言跑的就是生产降级路径本身。
"只建了一次"一律用 monkeypatch 包 _build 计数来证明，不靠 sleep/计时。
"""
from __future__ import annotations

import threading
from pathlib import Path

import pytest
from sqlmodel import Session, select

from app.config import get_settings
from app.db.models import Chunk, KnowledgeFile, pk
from app.db.session import engine
from app.services.rag import bm25_store, handler, ingest, text_index

LINE = "商品「保湿面霜 50g」，价格:129元，库存:36件"
OTHER = "商品「防晒霜 30ml」，SPF50+，防水，价格:89元"
POLICY = "全场满 199 元包邮，赠品面膜一片，7 天无理由退换"


@pytest.fixture
def bm25_cfg(tmp_path, monkeypatch):
    """把 BM25 缓存挪到临时目录（不污染 data/），并在前后清进程内缓存。"""
    def apply(*, persist: bool = True, path: Path | None = None) -> Path:
        rag = dict(get_settings().get("rag") or {})
        rag["bm25_persist"] = persist
        rag["bm25_index_path"] = str(path or (tmp_path / "bm25" / "knowledge.pkl.gz"))
        monkeypatch.setitem(get_settings(), "rag", rag)
        text_index.reset_cache()
        return bm25_store.index_path()

    yield apply
    text_index.reset_cache()


@pytest.fixture
def build_counter(monkeypatch):
    """统计 _build 调用次数 = "全量重建发生了几次"。"""
    counter = {"n": 0}
    real_build = text_index._build

    def spy(session: Session, fingerprint: str):
        counter["n"] += 1
        return real_build(session, fingerprint)

    monkeypatch.setattr(text_index, "_build", spy)
    return counter


def _seed(db: Session, *, name: str = "doc.txt", enabled: bool = True,
          lines: tuple[str, ...] = (LINE, OTHER)) -> KnowledgeFile:
    path = Path(name)
    kf = KnowledgeFile(name=path.name, path=str(path), file_type="text",
                       size=len("".join(lines)), status="ready", enabled=enabled,
                       chunk_count=len(lines))
    db.add(kf)
    db.commit()
    db.refresh(kf)
    for seq, text in enumerate(lines):
        db.add(Chunk(file_id=pk(kf), seq=seq, text=text, tokens=len(text) // 2))
    db.commit()
    db.expire_all()            # 断言都在同一个 Session 里读写，绕开身份映射的旧值
    return kf


def _chunk_ids(db: Session, file_id: int) -> list[int]:
    return [pk(c) for c in db.exec(
        select(Chunk).where(Chunk.file_id == file_id).order_by(Chunk.id)).all()]


def _ids(hits: list[text_index.Bm25Hit]) -> list[int]:
    return [h.chunk_id for h in hits]


# ---- 缓存命中不重建 ----

def test_second_search_hits_in_process_cache(db, bm25_cfg, build_counter):
    index_path = bm25_cfg()
    _seed(db)

    first: dict = {}
    second: dict = {}
    hits_a = text_index.search(db, "面霜 价格", stats=first)
    hits_b = text_index.search(db, "防晒霜 价格", stats=second)

    assert hits_a and hits_b
    assert build_counter["n"] == 1                     # 两次检索只建了一次
    assert first["source"] == "rebuild" and second["source"] == "memory"
    assert index_path.exists() and index_path.stat().st_size > 0


def test_process_restart_reuses_disk_cache(db, bm25_cfg, build_counter):
    index_path = bm25_cfg()
    _seed(db)
    before = text_index.search(db, "面霜")
    assert build_counter["n"] == 1 and index_path.exists()

    text_index.reset_cache()                           # 模拟进程重启：进程内缓存没了，磁盘还在
    stats: dict = {}
    after = text_index.search(db, "面霜", stats=stats)

    assert stats["source"] == "disk"
    assert build_counter["n"] == 1                     # 关键断言：重启后不重新分词建库
    assert _ids(after) == _ids(before)


# ---- 语料变化后自动失效 ----

def test_new_chunk_invalidates_cache_and_is_recallable(db, bm25_cfg, build_counter):
    bm25_cfg()
    kf = _seed(db)
    assert text_index.search(db, "包邮") == []         # 语料里还没有"包邮"
    assert build_counter["n"] == 1

    db.add(Chunk(file_id=pk(kf), seq=9, text=POLICY, tokens=len(POLICY) // 2))
    db.commit()
    db.expire_all()

    hits = text_index.search(db, "包邮")
    assert build_counter["n"] == 2                     # 版本标记变了 → 重建
    # BM25Plus 给每篇文档一个 delta 基线分，所以"命中列表"不等于"包含该词的文档"，
    # 排序语义交给 RRF 融合层——这里断言的是新切片确实进了索引且排在最前。
    assert _ids(hits)[0] == _chunk_ids(db, pk(kf))[-1], "新增切片必须立即可被召回"


def test_disabling_file_drops_its_chunks_from_index(db, bm25_cfg, build_counter):
    bm25_cfg()
    kf_a = _seed(db, name="a.txt", lines=(LINE,))
    kf_b = _seed(db, name="b.txt", lines=(OTHER,))
    ids_before = set(_ids(text_index.search(db, "商品", top_k=50)))
    assert ids_before == set(_chunk_ids(db, pk(kf_a)) + _chunk_ids(db, pk(kf_b)))

    kf_b.enabled = False
    db.add(kf_b)
    db.commit()
    db.expire_all()

    hits = text_index.search(db, "商品", top_k=50)
    assert build_counter["n"] == 2                     # enabled 翻转也算语料变化
    assert set(_ids(hits)) == set(_chunk_ids(db, pk(kf_a)))   # 禁用文件的切片全部退出


def test_deleted_chunks_rebuild_fresh(db, bm25_cfg, build_counter):
    index_path = bm25_cfg()
    kf = _seed(db)
    assert text_index.search(db, "防晒霜")
    assert build_counter["n"] == 1

    ingest.purge(db, kf)                               # 内部会 invalidate()
    assert text_index.search(db, "防晒霜") == []
    # 空语料没有可缓存的索引；旧缓存文件留在磁盘上也必须因版本标记不符而失效（绝不能被读到）
    assert bm25_store.load(index_path, bm25_store.corpus_fingerprint(db)) is None
    assert db.exec(select(Chunk)).all() == []

    _seed(db, lines=(OTHER,))                          # 重新入库同样的切片 → 又能召回
    assert text_index.search(db, "防晒霜")


# ---- 与"全量重建"结果一致（正确性红线）----

def test_cached_results_equal_full_rebuild(db, bm25_cfg, build_counter):
    """进程内命中 / 磁盘命中 / 关缓存现取现建，三条路径的命中集合与顺序必须逐条相同。"""
    lines = tuple(f"商品「条目{i}」，价格:{i % 97}元，库存:{i}件，规格:{'大' if i % 3 else '小'}号"
                  for i in range(40))
    _seed(db, lines=lines)
    query = "商品 价格 库存"

    index_path = bm25_cfg()                            # persist on：首次重建并落盘
    cold = text_index.search(db, query, top_k=15)
    warm = text_index.search(db, query, top_k=15)
    text_index.reset_cache()
    disk = text_index.search(db, query, top_k=15)
    assert index_path.exists()

    bm25_cfg(persist=False)                            # 关掉缓存 = 今天的老行为
    fresh = text_index.search(db, query, top_k=15)

    assert _ids(cold) == _ids(fresh), "缓存结果必须与全量重建逐条一致"
    assert _ids(warm) == _ids(fresh)
    assert _ids(disk) == _ids(fresh)
    assert [round(h.score, 12) for h in disk] == [round(h.score, 12) for h in fresh]


def test_handler_retrieve_works_on_bm25_fallback(db, bm25_cfg):
    """模型 key 留空 = 向量路必然降级，检索整体仍可用（这条降级路径必须一直可测）。"""
    bm25_cfg()
    _seed(db)
    hits = handler.retrieve(db, "面霜 价格")
    assert hits and "面霜" in hits[0].text
    assert hits[0].file_name == "doc.txt" and hits[0].score > 0


# ---- 坏缓存不能把检索带崩 ----

@pytest.mark.parametrize("corrupt", ["empty", "junk", "truncated", "payload_flip", "header_only"])
def test_corrupt_cache_falls_back_to_rebuild(db, bm25_cfg, build_counter, corrupt):
    index_path = bm25_cfg()
    _seed(db)
    assert text_index.search(db, "面霜")
    assert index_path.exists()
    good = index_path.read_bytes()

    if corrupt == "empty":
        data = b""
    elif corrupt == "junk":
        data = "这不是我们的索引文件".encode() * 20
    elif corrupt == "truncated":
        data = good[: len(good) // 2]
    elif corrupt == "payload_flip":
        buf = bytearray(good)
        buf[-3] ^= 0xFF
        data = bytes(buf)
    else:                                    # 头部留着、payload 整段丢失
        data = good[:12] + b"\x00" * 8

    index_path.write_bytes(data)
    text_index.reset_cache()
    build_counter["n"] = 0

    stats: dict = {}
    hits = text_index.search(db, "面霜", stats=stats)
    assert stats["source"] == "rebuild" and build_counter["n"] == 1
    assert hits, "坏缓存只该损失一次重建，不该让检索零命中或抛异常"
    assert index_path.read_bytes() != data           # 已用新索引覆盖

    text_index.reset_cache()
    stats2: dict = {}
    assert text_index.search(db, "面霜", stats=stats2)
    assert stats2["source"] == "disk", "重建后必须重新写下可用缓存，而不是每次都重建"


def test_format_version_bump_invalidates_cache(db, bm25_cfg, build_counter, monkeypatch):
    bm25_cfg()
    _seed(db)
    text_index.search(db, "面霜")
    assert build_counter["n"] == 1

    text_index.reset_cache()
    monkeypatch.setattr(bm25_store, "FORMAT_VERSION", bm25_store.FORMAT_VERSION + 1)
    text_index.search(db, "面霜")
    assert build_counter["n"] == 2                     # 格式换代必须丢弃旧文件


def test_missing_index_directory_is_ok(db, bm25_cfg, build_counter, tmp_path):
    path = tmp_path / "nope" / "deep" / "knowledge.pkl.gz"
    bm25_cfg(path=path)
    _seed(db)
    assert text_index.search(db, "面霜")               # 目录不存在时 save 自建，不抛
    assert path.exists()


def test_load_returns_none_for_foreign_fingerprint(db, bm25_cfg):
    """另一套语料/另一个库留下的缓存：标记不符就当作没有缓存（跨进程/换库的安全网）。"""
    index_path = bm25_cfg()
    kf = _seed(db)
    text_index.search(db, "面霜")
    assert bm25_store.load(index_path, "deadbeef") is None
    state = bm25_store.load(index_path, bm25_store.corpus_fingerprint(db))
    assert state is not None and len(state.chunk_ids) == 2
    ingest.purge(db, kf)


# ---- 并发安全 ----

def test_concurrent_searches_do_not_corrupt_cache(db, bm25_cfg, build_counter):
    """多线程同时发现"缓存过期"：只有一个线程重建，磁盘上必须仍是完整可读的文件。"""
    index_path = bm25_cfg()
    _seed(db)
    text_index.search(db, "面霜")                     # 先落一份旧版本缓存
    assert index_path.exists()

    kf = db.exec(select(KnowledgeFile)).first()
    assert kf is not None
    db.add(Chunk(file_id=pk(kf), seq=7, text=POLICY, tokens=10))
    db.commit()
    db.expire_all()
    text_index.reset_cache()
    build_counter["n"] = 0

    results: dict[str, list[int]] = {}
    errors: list[BaseException] = []
    gate = threading.Barrier(4)

    def worker(term: str) -> None:
        try:
            gate.wait()
            with Session(engine) as sess:
                results[term] = _ids(text_index.search(sess, term, top_k=10))
        except BaseException as e:  # noqa: BLE001 - 线程里的异常要带回主线程断言
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(t,))
               for t in ("包邮", "面霜", "防晒霜", "商品 价格")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)

    assert not errors, errors
    assert len(results) == 4
    assert build_counter["n"] == 1, "进程内锁要保证同一版本只重建一次"
    state = bm25_store.load(index_path, bm25_store.corpus_fingerprint(db))
    assert state is not None, "并发写完后磁盘缓存必须仍是可读的完整文件"
    assert results["包邮"], "新切片在并发重建后仍可召回"


# ---- 开关关闭时走原路径 ----

def test_persist_disabled_rebuilds_every_search(db, bm25_cfg, build_counter):
    index_path = bm25_cfg(persist=False)
    _seed(db)
    text_index.search(db, "面霜")
    text_index.search(db, "防晒霜")
    text_index.search(db, "商品 价格")

    assert build_counter["n"] == 3                     # 老行为：每次检索现取现建
    assert not index_path.exists()                     # 且完全不写缓存文件


def test_persist_switch_off_then_on_does_not_serve_stale_memory(db, bm25_cfg, build_counter):
    bm25_cfg()
    _seed(db)
    text_index.search(db, "面霜")                       # 建一次并落盘
    bm25_cfg(persist=False)
    stats_off: dict = {}
    text_index.search(db, "面霜", stats=stats_off)
    assert stats_off["source"] == "rebuild" and build_counter["n"] == 2   # 关掉期间每次都建
    bm25_cfg()                                          # 再打开（fixture 同时清进程内缓存）
    stats_on: dict = {}
    hits = text_index.search(db, "面霜", stats=stats_on)
    assert stats_on["source"] == "disk" and build_counter["n"] == 2 and hits


def test_index_path_defaults_under_data_dir(monkeypatch):
    rag = dict(get_settings().get("rag") or {})
    rag["bm25_index_path"] = ""                        # yaml 里留空 → 回落 data/bm25/
    monkeypatch.setitem(get_settings(), "rag", rag)
    p = bm25_store.index_path()
    assert p.name == "knowledge.pkl.gz"
    assert p.parts[-3:] == ("data", "bm25", p.name)

    rag["bm25_index_path"] = "data/bm25/rel.pkl.gz"    # 相对路径按后端根解析
    monkeypatch.setitem(get_settings(), "rag", rag)
    assert bm25_store.index_path().is_absolute()
