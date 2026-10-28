"""BM25 检索规模基准（离线，不需要模型 key：只打 BM25 这条路）。

用法（在 src/backend 下）：
    ../../.venv/Scripts/python.exe scripts/bench_rag.py --sizes 1000,10000,50000

为什么需要这张表：代码里原来的假设是"语料 <10k chunk，每次检索现取现建（毫秒级）"。
这个假设是否成立、以及"什么时候该换真检索引擎（Meilisearch/ES/tantivy）"要看两个
**互相独立**的成本：

1. 建库成本（jieba 分词 + 重算 idf）：持久化缓存能把它从"每次检索"摊薄成"每个语料
   版本一次"，进程重启后还能从磁盘捡回来；
2. 打分成本（rank_bm25 的 get_scores 对每个查询词扫一遍全库词频表，O(切片数)）：
   缓存**救不了**它，只能换引擎。

所以表里同时给出"现取现建（老路径）""首次检索+落盘""磁盘缓存命中""进程内缓存命中"
"其中纯 get_scores 打分""版本标记 SQL"六项。判断口径：
- 老路径已经秒级 → 持久化是必须的（本次改造解决的部分）；
- 稳态检索里"纯打分"占大头且随规模线性上升 → 就是"该换引擎"的信号。

脚本只读自己的临时库与临时缓存目录，不碰 data/，也不需要 api_key。
"""
from __future__ import annotations

import argparse
import os
import shutil
import statistics
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]


def _write_config(tmp: Path) -> Path:
    """最小可用配置：临时 SQLite + 临时 BM25 目录，模型 key 全空（BM25 降级路径）。"""
    cfg = tmp / "config.bench.yaml"
    cfg.write_text(f"""
server: {{ host: 127.0.0.1, port: 7999 }}
database: {{ url: sqlite:///{(tmp / 'bench.db').as_posix()} }}
security:
  jwt_secret: bench-secret-0123456789abcdef-0123456789abcdef
  rate_limit: {{ enabled: false }}
multi_models:
  dashscope:
    api_key: ""
    base_url: http://127.0.0.1:9/unused
    chat_model: test-chat
    embedding_model: ""
    rerank_model: ""
    rerank_base_url: ""
    vl_model: ""
    temperature: 0
default_model: dashscope
llm: {{ request_timeout_seconds: 5, max_retries: 0, fallback: [] }}
memory: {{ short_max_tokens: 1000, long_enabled: false }}
session: {{ idle_seconds: 100000, close_after_seconds: 100000, sweep_interval_seconds: 3600 }}
rag:
  chunk_size: 500
  chunk_overlap: 100
  recall_k: 20
  top_k: 5
  chroma_path: {(tmp / 'chroma').as_posix()}
  ingest_workers: 1
  bm25_persist: true
  bm25_index_path: {(tmp / 'bm25' / 'bench.pkl.gz').as_posix()}
  embedding_batch_size: 10
mcp: {{ allowed_commands: [python] }}
wecom: {{ mock: true }}
""", encoding="utf-8")
    return cfg


LINE_TMPL = ("商品「{brand}{kind} {size}」，SKU:{sku}，价格:{price}元，库存:{stock}件，"
             "分类:{kind}，规格:{spec}，货期时间:{lead}，{note}")
BRANDS = ["水之韵", "清润", "本草", "海蓝", "雪花", "珀莱", "薇诺", "玉泽"]
KINDS = ["保湿面霜", "防晒霜", "洁面乳", "眼霜", "精华液", "补水面膜", "身体乳"]
SIZES = ["30ml", "50g", "100ml", "5 支装", "120ml", "正装 + 小样"]
SPECS = ["1 瓶 2 支", "SPF50+ 防水", "敏感肌可用", "含烟酰胺 5%", "箱规 24 盒", "无香精"]
NOTES = ["赠品面膜一片", "满 199 元包邮", "7 天无理由退换", "生产日期见包装", "预售 15 天发货"]


def make_chunk_text(i: int, lines_per_chunk: int) -> str:
    rows = []
    for j in range(lines_per_chunk):
        k = i * lines_per_chunk + j
        rows.append(LINE_TMPL.format(
            brand=BRANDS[k % len(BRANDS)], kind=KINDS[(k // 3) % len(KINDS)],
            size=SIZES[(k // 7) % len(SIZES)], sku=f"A{k:07d}",
            price=39 + (k * 7) % 400, stock=(k * 13) % 90,
            spec=SPECS[(k // 11) % len(SPECS)], lead=f"{1 + k % 15} 天",
            note=NOTES[k % len(NOTES)]))
    return "\n".join(rows)


def seed(total_chunks: int, lines_per_chunk: int) -> int:
    """直接写 Chunk/KnowledgeFile 表（不走向量与解析），返回写入耗时秒。"""
    from sqlalchemy import delete
    from sqlmodel import Session, SQLModel

    import app.db.models  # noqa: F401  注册表
    from app.db.session import engine

    SQLModel.metadata.create_all(engine)
    t0 = time.perf_counter()
    per_file = 500
    with Session(engine) as session:
        session.exec(delete(app.db.models.Chunk))          # type: ignore[call-overload]
        session.exec(delete(app.db.models.KnowledgeFile))  # type: ignore[call-overload]
        session.commit()
        conn = session.connection()
        n_files = (total_chunks + per_file - 1) // per_file
        for f in range(n_files):
            conn.exec_driver_sql(
                "INSERT INTO knowledgefile (name, path, file_type, size, status, error, "
                "enabled, chunk_count, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (f"bench-{f}.txt", f"/tmp/bench-{f}.txt", "text", 0, "ready", None, 1,
                 min(per_file, total_chunks - f * per_file), "2026-01-01 00:00:00.000000"))
            file_id = int(conn.exec_driver_sql("SELECT last_insert_rowid()").scalar_one())
            start = f * per_file
            end = min(start + per_file, total_chunks)
            conn.exec_driver_sql(
                "INSERT INTO chunk (file_id, seq, text, tokens, created_at) VALUES (?,?,?,?,?)",
                [(file_id, seq, make_chunk_text(k, lines_per_chunk), 250,
                  "2026-01-01 00:00:00.000000")
                 for seq, k in enumerate(range(start, end))])
        session.commit()
    return time.perf_counter() - t0


def median_runs(fn: Callable[[], object], repeats: int) -> float:
    xs: list[float] = []
    for _ in range(repeats):
        t0 = time.perf_counter()
        fn()
        xs.append(time.perf_counter() - t0)
    return statistics.median(xs)


def bench_size(total_chunks: int, query: str, tmp: Path) -> dict[str, float]:
    from sqlmodel import Session

    from app.config import get_settings
    from app.db.session import engine
    from app.services.rag import bm25_store, text_index

    rag = get_settings()["rag"]                        # 直接改内存里的配置（独立进程，无副作用）
    index_path = tmp / "bm25" / f"bench-{total_chunks}.pkl.gz"
    rag["bm25_index_path"] = str(index_path)
    rag["bm25_persist"] = True
    text_index.reset_cache()

    out: dict[str, float] = {}
    with Session(engine) as session:
        # 1) 老路径：每次检索现取现建（= 今天 rag.bm25_persist=false 的行为）
        rag["bm25_persist"] = False
        out["rebuild_per_query"] = median_runs(lambda: text_index.search(session, query), 3)

        # 2) 持久化开启、磁盘无缓存：建库 + 落盘（每个语料版本只付一次）
        rag["bm25_persist"] = True
        index_path.unlink(missing_ok=True)
        text_index.reset_cache()
        out["first_with_persist"] = median_runs(lambda: text_index.search(session, query), 1)
        out["index_file_bytes"] = index_path.stat().st_size if index_path.exists() else 0

        # 3) 进程重启后：只有磁盘缓存（免去分词，保留打分）
        def cold_from_disk() -> None:
            text_index.reset_cache()
            text_index.search(session, query)

        out["disk_cache_hit"] = median_runs(cold_from_disk, 3)

        # 4) 稳态：版本标记 SQL + 进程内索引打分
        out["in_process_hit"] = median_runs(lambda: text_index.search(session, query), 10)

        # 5) 拆解：版本标记聚合 SQL 与 rank_bm25 打分各占多少（缓存救不了后者）
        out["fingerprint_sql"] = median_runs(lambda: bm25_store.corpus_fingerprint(session), 10)
        state = bm25_store.load(index_path, bm25_store.corpus_fingerprint(session))
        tokens = text_index._tokenize(query)
        out["get_scores_only"] = (median_runs(lambda: state.index.get_scores(tokens), 5)
                                  if state is not None else 0.0)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="BM25 检索规模基准（离线）")
    parser.add_argument("--sizes", default="1000,10000,50000", help="逗号分隔的切片规模")
    parser.add_argument("--lines-per-chunk", type=int, default=10,
                        help="每个合成切片的商品行数（≈500 token 的真实块大小）")
    parser.add_argument("--query", default="面霜 敏感肌 价格 包邮", help="查询词")
    args = parser.parse_args(argv)

    sizes = [int(s) for s in args.sizes.split(",") if s.strip()]
    tmp = Path(tempfile.mkdtemp(prefix="cs-bench-"))
    os.environ["CS_CONFIG"] = str(_write_config(tmp))
    if str(BACKEND_ROOT) not in sys.path:
        sys.path.insert(0, str(BACKEND_ROOT))

    from loguru import logger

    logger.remove()                                    # 基准只要表格，不要日志噪音
    logger.add(sys.stderr, level="ERROR")
    import jieba

    jieba.setLogLevel(40)                              # 压掉 jieba 的词典加载日志

    print(f"BM25 基准（jieba+BM25Plus，离线合成语料，{args.lines_per_chunk} 行/切片，"
          f"查询「{args.query}」）")
    print(f"临时库/缓存目录: {tmp}\n")
    header = (f"{'切片数':>8} | {'现取现建/次':>12} | {'首次+落盘':>10} | {'磁盘命中':>9} | "
              f"{'进程内命中':>9} | {'版本标记SQL':>11} | {'纯打分':>8} | {'缓存文件':>9}")
    print(header)
    print("-" * len(header))

    rows = []
    for n in sizes:
        seed_seconds = seed(n, args.lines_per_chunk)
        r = bench_size(n, args.query, tmp)
        rows.append((n, r))
        print(f"{n:>8} | {r['rebuild_per_query'] * 1000:>10.0f} ms | "
              f"{r['first_with_persist'] * 1000:>8.0f} ms | "
              f"{r['disk_cache_hit'] * 1000:>7.0f} ms | "
              f"{r['in_process_hit'] * 1000:>7.0f} ms | "
              f"{r['fingerprint_sql'] * 1000:>9.1f} ms | "
              f"{r['get_scores_only'] * 1000:>6.1f} ms | "
              f"{r['index_file_bytes'] / 1024:>7.0f} KB"
              f"   (灌库 {seed_seconds:.1f}s)")

    print("\n读法：")
    print("- 现取现建/次 = 改造前每次检索的代价；超过 ~200ms 就说明持久化是必须的。")
    print("- 磁盘命中 ≈ 稳态 + 一次反序列化；进程重启后第一次检索付这个钱。")
    print("- 进程内命中 = 稳态延迟（版本标记 SQL + 打分 + 排序）；版本标记 SQL 恒定小额，")
    print("  纯打分随规模线性上升——它才是决定「要不要换 Meilisearch/ES」的那一列。")

    speedups = [(n, r["rebuild_per_query"] / max(r["in_process_hit"], 1e-9)) for n, r in rows]
    print("\n稳态相对老路径的加速比: " + ", ".join(f"{n}:{s:.0f}x" for n, s in speedups))
    shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
