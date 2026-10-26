"""向量索引重建脚本：以 Chunk 表为准，把缺失/过期的向量补进 Chroma。

适用场景：换 embedding 模型（维度/向量空间变了）、或上传时向量化失败只落了 BM25
（知识文件 error 字段有「向量化失败」文案）后补建，避免删文件重传。
单个文件请直接用管理页的「重建向量」（POST /knowledge/files/{id}/reindex，同一个
ingest.reindex_file）；本脚本用于全量重建或服务停着时离线修数据。

用法（在 src/backend 下；需先停掉后端，两个进程不能同时开 Chroma）：
    CS_CONFIG=config.local.yaml py scripts/reindex_vectors.py            # 全部文件
    CS_CONFIG=config.local.yaml py scripts/reindex_vectors.py --file 13  # 只重建某个文件
    CS_CONFIG=config.local.yaml py scripts/reindex_vectors.py --dry-run  # 只看统计不写库
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Windows 控制台默认 GBK，中文进度会直接抛 UnicodeEncodeError
for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")


def main() -> int:
    from sqlmodel import Session, select

    from app.db.models import Chunk, KnowledgeFile
    from app.db.session import engine
    from app.services.rag import ingest, vectorstore

    ap = argparse.ArgumentParser()
    ap.add_argument("--file", type=int, help="只重建指定 knowledge_file.id")
    ap.add_argument("--dry-run", action="store_true", help="仅统计，不写 Chroma")
    args = ap.parse_args()

    with Session(engine) as session:
        stmt = select(KnowledgeFile).order_by(KnowledgeFile.id)
        if args.file:
            stmt = stmt.where(KnowledgeFile.id == args.file)
        files = session.exec(stmt).all()
        if not files:
            print("没有匹配的知识文件")
            return 1

        total = 0
        for kf in files:
            n = len(session.exec(  # type: ignore[union-attr]
                select(Chunk).where(Chunk.file_id == kf.id)).all())
            print(f"文件 #{kf.id} {kf.name}: {n} chunks, status={kf.status}, error={kf.error or '-'}")
            if not n or args.dry_run:
                continue
            total += ingest.reindex_file(session, kf)
            print(f"  -> 已写入 {n} 条向量")

        print(f"完成：重建 {total} 条向量，Chroma collection 现有 {vectorstore._collection().count()} 条")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
