"""恢复后对账：SQLite 的 Chunk 表 ↔ Chroma 向量是否一致，并抽样跑一次检索。

    cd src/backend
    CS_CONFIG=config.local.yaml py scripts/verify_restore.py
    CS_CONFIG=config.local.yaml py scripts/verify_restore.py --fix      # 补建缺失向量

为什么需要它：向量库是从快照里整目录拷回来的，运行中拷出来的索引可能撕裂；而
`Chunk` 表才是唯一真相。以前两边不一致只能靠"全量重新入库"兜底——那是几十分钟起、
还要重新解析每个文件的操作。对账脚本先告诉你差多少、差在哪些文件，再按文件补建。

补建粒度是**文件级**（复用 ingest.reindex_file，与知识库页的「重建向量」同一个入口）：
按切片补要新写一条 embedding 增量通路，而缺失几乎总是成批出现（换模型、上传时向量化
失败、快照撕裂），文件级重建在正确性和实现复杂度之间更划算。

Chroma 里多出来的孤儿向量只报告不自动删——删除不可逆，交给人确认更合适。
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

BATCH = 500


def main() -> int:
    parser = argparse.ArgumentParser(description="Chunk 表与 Chroma 向量对账")
    parser.add_argument("--fix", action="store_true", help="对含缺失向量的文件重建向量")
    parser.add_argument("--query", default="面霜", help="抽样检索的关键词")
    args = parser.parse_args()

    from sqlmodel import Session, col, select

    from app.db.models import Chunk, KnowledgeFile
    from app.db.session import engine
    from app.services.rag import handler, ingest, vectorstore

    with Session(engine) as session:
        files = session.exec(select(KnowledgeFile).order_by(col(KnowledgeFile.id))).all()
        rows = session.exec(select(Chunk.id, Chunk.file_id).order_by(col(Chunk.id))).all()
        print(f"知识库文件: {len(files)} 个"
              f"（ready={sum(1 for f in files if f.status == 'ready')}）")
        print(f"Chunk 表切片: {len(rows)} 条")
        if not rows:
            print("没有切片：向量库理应为空，对账结束")
            return 0

        ids = [int(r[0]) for r in rows]
        file_of = {int(r[0]): int(r[1]) for r in rows}
        have: set[int] = set()
        for i in range(0, len(ids), BATCH):
            have |= vectorstore.has_chunks(ids[i:i + BATCH])
        missing = [i for i in ids if i not in have]
        print(f"已有向量: {len(have)} 条，缺失: {len(missing)} 条")

        try:
            total_vec = int(vectorstore._collection().count())
        except Exception as e:
            print(f"向量库计数失败（目录可能损坏，建议直接从快照恢复后重建）: {e}")
            total_vec = -1
        if total_vec >= 0:
            orphan = total_vec - len(have)
            print(f"孤儿向量（Chroma 有、表里没有）约 {max(0, orphan)} 条：不自动删，请人工核对")

        if not missing:
            print("对账通过：Chunk 表与向量库一致")
        else:
            per_file = Counter(file_of[i] for i in missing)
            print("缺失分布：" + ", ".join(f"file#{fid}={n}" for fid, n in per_file.most_common(20)))
            if not args.fix:
                print("未补建。要补建请加 --fix（按文件重建向量，会打 embedding 调用）")
                return 1
            for fid in per_file:
                kf = session.get(KnowledgeFile, fid)
                if kf is None:
                    print(f"  file#{fid} 在表里已不存在，跳过")
                    continue
                n = ingest.reindex_file(session, kf)
                print(f"  重建 file#{fid} {kf.name}: {n} 条向量")
            after: set[int] = set()
            for i in range(0, len(missing), BATCH):
                after |= vectorstore.has_chunks(missing[i:i + BATCH])
            print(f"补建后仍缺 {len(missing) - len(after)} 条")
            if len(after) != len(missing):
                print("多半是 embedding 模型 key 不可用（订阅套餐类 key 常不放行 embedding），"
                      "换标准 key 后重试")
                return 1

        hits = handler.retrieve(session, args.query, top_k=3)
        print(f"抽样检索「{args.query}」命中 {len(hits)} 条"
              + (f"，首条：{hits[0].text[:40]}…" if hits else ""))
        if not hits:
            print("检索零命中：向量与 BM25 双路都没结果，请检查知识库文件是否 ready")
            return 1
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
