"""BM25 索引的磁盘缓存与"语料版本标记"。

为什么要有这一层：text_index 原本是**每次检索现取现建**（从 Chunk 表读出全部切片、
jieba 分词、重算 idf）。这在千级切片下就要秒级（bench_rag.py 实测 1k 切片 1.7 秒），
并且随切片数线性上升——检索接口根本不是"毫秒级"。这里把建好的索引落到本机 data/ 下的
文件，用"语料版本标记"判断缓存是否过期，命中即免去分词与建索引（1k：1708ms → 20ms）。

版本标记为什么够用（正确性口径）：
- 本项目里 Chunk 行只有两种变化方式——入库时插入、清理时删除，**没有任何原地 UPDATE**
  （全仓唯一写路径见 ingest.py 的 Chunk(...) 构造与 purge/remove_file 的 delete）；
- KnowledgeFile.enabled 翻转不碰 Chunk 行，所以标记里额外带一份"启用文件摘要"；
- 于是 (切片数, 最小 id, 最大 id, id 之和) 唯一确定了"当前参与检索的切片集合"。
  集合变 → 标记变 → 缓存判过期 → 重建。检索结果因此恒等于"当场全量重建"的结果，
  这是这条链路的正确性红线。

增量更新为什么**不做**（写成惰性重建）：
- rank_bm25 的 BM25Plus 没有"删一行/加一行"的接口，idf 依赖全库文档频率、avgdl 依赖
  全库长度和，任何一条切片的增删都会改变**所有**文档的打分；要"增量后仍等于全量重建"，
  就得重算全库 idf + avgdl + 全部 doc_freqs——省下的只有分词那一步（约占 90% 成本）；
- 而 SQLite 是单写者：入库任务与检索在同一进程/同一库上交替提交，任何"半成品批次"
  都可能被另一次读看到（chunk 是逐条 commit 的，见 ingest_file）。增量索引在这种写入
  模式下要为每个中间状态维护一份索引，复杂度和坏文件风险都远超收益。
- 结论：**标记失效 + 惰性全量重建 + 磁盘持久化**。每个语料版本最多付一次重建成本，
  且分摊到进程生命周期而不是每次请求。真要支持高频增删 + 毫秒级查询，正解是换专用
  检索引擎（Meilisearch/ES/tantivy），不是给 BM25Plus 打补丁。

信任边界（pickle）：
- 缓存文件写在 data/bm25/ 下，由本进程自己写入，**不接收上传内容、不放在共享/可被
  第三方写的目录**；pickle.loads 会执行构造逻辑，所以只有"文件可被外人篡改"才是风险。
- 即便如此仍做三层校验：明文头（魔数 + 格式版本 + pickle 协议 + 长度 + payload 的
  sha256 + 语料标记）→ 校验 sha256 与长度**之后**才反序列化 → 反序列化后再校验类型与
  条数一致性。任何一层不过、或任何异常（含 EOF/解码错误），一律当"没有缓存"处理，
  退回全量重建：缓存只影响延迟，永远不能影响可用性或结果正确性。
- sha256 只防损坏/截断，不防有针对性的伪造（无密钥）；真要防篡改应换 HMAC，但那时
  攻击者已能写本机 data/ 目录，等于已有代码执行权限——所以这里不再加码，只在部署时
  把 data/ 的权限按"等同于应用代码"管理。
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import pickle
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loguru import logger
from sqlalchemy import func
from sqlmodel import Session, col, select

from app.config import BACKEND_ROOT, get_settings
from app.db.models import Chunk, KnowledgeFile

# 缓存文件格式版本：**任何**改变索引内容的语义（分词器、payload 结构、BM25 变体/参数）
# 都必须 +1，旧文件会因版本不符被丢弃重建。
FORMAT_VERSION = 1
HEADER_MAGIC = "cs-bm25-index"
PICKLE_PROTOCOL = 4          # 固定协议：换 Python 版本时缓存只是失效，不会读出半个对象
DEFAULT_INDEX_REL_PATH = os.path.join("data", "bm25", "knowledge.pkl.gz")
_TOKEN_SEP = "\x01"          # jieba 词条不含控制字符，可以安全地当拼接分隔符


@dataclass
class CorpusState:
    """一份可用的 BM25 索引 + 它对应的语料版本标记与位置→切片映射。"""

    fingerprint: str
    index: Any                      # rank_bm25.BM25Plus 实例（get_scores 只需它的统计量）
    chunk_ids: list[int]
    file_ids: list[int]
    source: str = "db"              # db=当场重建 / disk=磁盘缓存 / memory=进程内缓存


def persist_enabled() -> bool:
    raw = get_settings().get_dotted("rag.bm25_persist", True)
    if isinstance(raw, str):
        return raw.strip().lower() not in ("", "false", "no", "0", "off")
    return bool(raw)


def index_path() -> Path:
    """rag.bm25_index_path：留空 = data/bm25/knowledge.pkl.gz；相对路径按后端根解析。

    和 chroma_path 同一个坑位（get_dotted 在"键存在但值为空串"时返回空串），必须 or 回落。
    """
    raw = str(get_settings().get_dotted("rag.bm25_index_path", "") or "").strip()
    if not raw:
        return BACKEND_ROOT / DEFAULT_INDEX_REL_PATH
    p = Path(raw)
    return p if p.is_absolute() else BACKEND_ROOT / p


def corpus_fingerprint(session: Session) -> str:
    """语料版本标记：参与检索的切片集合 + 文件启停摘要 + 当前库标识。

    带上 database.url 的哈希是为了"同机多库共用一个缓存目录也不会串味"：两个结构相同的
    库（同样的切片数与 id 分布）光靠聚合值理论上可能撞车，加了这个就不可能。
    """
    row = session.exec(
        select(func.count(), func.min(Chunk.id), func.max(Chunk.id), func.sum(Chunk.id))
        .select_from(Chunk)
        .join(KnowledgeFile, Chunk.file_id == KnowledgeFile.id)  # type: ignore[arg-type]
        .where(KnowledgeFile.enabled == True)  # noqa: E712
    ).one()
    n = int(row[0] or 0)
    ids_min = int(row[1] or 0)
    ids_max = int(row[2] or 0)
    ids_sum = int(row[3] or 0)

    files = session.exec(select(KnowledgeFile.id, KnowledgeFile.enabled)
                         .order_by(col(KnowledgeFile.id))).all()
    enabled_digest = "|".join(f"{int(f[0] or 0)}:{1 if f[1] else 0}" for f in files)

    db_url = str(get_settings().get_dotted("database.url", ""))
    src = (f"v{FORMAT_VERSION}|db={hashlib.sha256(db_url.encode('utf-8')).hexdigest()[:16]}"
           f"|n={n}|min={ids_min}|max={ids_max}|sum={ids_sum}#{enabled_digest}")
    return hashlib.sha256(src.encode("utf-8")).hexdigest()[:32]


def encode_corpus(corpus: list[list[str]]) -> str:
    return _TOKEN_SEP.join(_TOKEN_SEP.join(doc) for doc in corpus)


def decode_corpus(blob: str) -> list[list[str]]:
    if not blob:
        return []
    return [doc.split(_TOKEN_SEP) if doc else [] for doc in blob.split(_TOKEN_SEP)]


def _payload(state: CorpusState) -> dict[str, Any]:
    return {
        "index": state.index,
        "chunk_ids": list(state.chunk_ids),
        "file_ids": list(state.file_ids),
    }


def _validate_loaded(obj: Any, fingerprint: str) -> tuple[Any, list[int], list[int]] | None:
    """反序列化后的结构校验：宁可判为无效重建，也不能把坏对象交给检索。"""
    try:
        index = obj["index"]
        chunk_ids = obj["chunk_ids"]
        file_ids = obj["file_ids"]
        if type(index).__name__ != "BM25Plus" or not hasattr(index, "get_scores"):
            return None
        n = int(index.corpus_size)
        if len(chunk_ids) != n or len(file_ids) != n or len(index.doc_len) != n:
            return None
        if n > 0 and not (all(isinstance(i, int) for i in chunk_ids)
                          and all(isinstance(i, int) for i in file_ids)):
            return None
        if not isinstance(fingerprint, str) or not fingerprint:
            return None
        return index, [int(i) for i in chunk_ids], [int(i) for i in file_ids]
    except Exception as e:  # noqa: BLE001 - 缓存坏了只能降级，不能把检索带崩
        logger.warning("BM25 缓存载荷校验异常，按无效缓存处理: {}", e)
        return None


def load(path: Path, fingerprint: str) -> CorpusState | None:
    """读缓存；任何异常/不匹配都返回 None（调用方据此重建）。绝不抛出。"""
    try:
        if not path.exists():
            return None
        raw = path.read_bytes()
        header_len = int.from_bytes(raw[:2], "big")
        header = json.loads(raw[2:2 + header_len].decode("utf-8"))
        if header.get("magic") != HEADER_MAGIC:
            logger.warning("BM25 缓存魔数不符，忽略: {}", path)
            return None
        if int(header.get("format_version", -1)) != FORMAT_VERSION:
            logger.info("BM25 缓存格式版本变化（{}），重建", header.get("format_version"))
            return None
        if int(header.get("pickle_protocol", -1)) != PICKLE_PROTOCOL:
            logger.info("BM25 缓存 pickle 协议变化（{}），重建", header.get("pickle_protocol"))
            return None
        if str(header.get("fingerprint")) != fingerprint:
            return None                      # 语料变了：最常见、也最正常的一次失效
        offset = 2 + header_len
        size = int(header["payload_size"])
        blob = raw[offset:offset + size]
        if len(blob) != size:
            logger.warning("BM25 缓存被截断（{} != {}），忽略", len(blob), size)
            return None
        if hashlib.sha256(blob).hexdigest() != header["payload_sha256"]:
            logger.warning("BM25 缓存校验和不符（损坏/被改写），忽略: {}", path)
            return None
        payload_bytes = gzip.decompress(blob)
        obj = pickle.loads(payload_bytes)
        checked = _validate_loaded(obj, fingerprint)
        if checked is None:
            logger.warning("BM25 缓存载荷结构不合法，忽略: {}", path)
            return None
        index, chunk_ids, file_ids = checked
        return CorpusState(fingerprint=fingerprint, index=index, chunk_ids=chunk_ids,
                           file_ids=file_ids, source="disk")
    except Exception as e:  # noqa: BLE001 - 读缓存失败必须降级重建，检索不能被拖死
        logger.warning("BM25 缓存读取失败，降级为当场重建 {}: {}", path, e)
        return None


def save(path: Path, state: CorpusState) -> int:
    """原子落盘：先写唯一命名的临时文件，fsync 后 os.replace。

    并发（同进程多线程 / 同机多进程）下每个写者都有自己的临时文件，replace 是原子的，
    读者只会看到"完整的旧文件"或"完整的新文件"，不会出现半截 pickle。
    """
    payload_bytes = pickle.dumps(_payload(state), protocol=PICKLE_PROTOCOL)
    blob = gzip.compress(payload_bytes, compresslevel=6)
    header = {
        "magic": HEADER_MAGIC,
        "format_version": FORMAT_VERSION,
        "pickle_protocol": PICKLE_PROTOCOL,
        "fingerprint": state.fingerprint,
        "docs": len(state.chunk_ids),
        "payload_size": len(blob),
        "payload_sha256": hashlib.sha256(blob).hexdigest(),
    }
    header_bytes = json.dumps(header, ensure_ascii=False).encode("utf-8")
    body = int(len(header_bytes)).to_bytes(2, "big") + header_bytes + blob

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(body)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except Exception as e:  # noqa: BLE001 - 缓存写不进去只是下次再重建，不影响检索可用
        logger.warning("BM25 缓存写盘失败（忽略，下次检索继续重建）: {}", e)
        tmp.unlink(missing_ok=True)
        return 0
    return len(body)


_write_lock = threading.Lock()


def lock() -> threading.Lock:
    """进程内"重建+落盘"的串行锁：同一版本只让一个线程付重建成本。"""
    return _write_lock
