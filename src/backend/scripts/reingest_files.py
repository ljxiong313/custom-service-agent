"""用当前解析器/分块器批量重建已有知识文件的 chunk、向量与商品行（不删原文件）。

改进了 parser/chunker 之后，库里旧的切片不会自己变——本脚本按 file_id 清掉
Chunk + Chroma 向量 + 该文件导入的 Product 行，再走一遍 ingest_file 重建。
单个文件请直接用管理页的「重新入库」（POST /knowledge/files/{id}/reingest，同一套
ingest.purge + ingest_file）；本脚本的价值是一次跑完全部、且能在服务停着时离线修数据。

用法（需先停掉后端，避免两个进程同时打开 Chroma PersistentClient）：
    CS_CONFIG=config.local.yaml py scripts/reingest_files.py --dry-run
    CS_CONFIG=config.local.yaml py scripts/reingest_files.py --name 库存
    CS_CONFIG=config.local.yaml py scripts/reingest_files.py --all
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from loguru import logger  # noqa: E402
from sqlmodel import Session, select  # noqa: E402

from app.db.models import KnowledgeFile  # noqa: E402
from app.db.session import engine  # noqa: E402
from app.services.rag import ingest, vectorstore  # noqa: E402

logger.remove()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", type=int, help="只重建指定 file_id")
    ap.add_argument("--name", help="按文件名子串匹配重建")
    ap.add_argument("--all", action="store_true", help="重建全部文件")
    ap.add_argument("--dry-run", action="store_true", help="只打印将要重建的文件")
    args = ap.parse_args()

    with Session(engine) as session:
        files = session.exec(select(KnowledgeFile).order_by(KnowledgeFile.id)).all()
        if args.id:
            files = [f for f in files if f.id == args.id]
        elif args.name:
            files = [f for f in files if args.name in f.name]
        elif not args.all:
            print("请指定 --id / --name / --all")
            return 2

        for f in files:
            mark = "（将被重建）" if not args.dry_run else ""
            print(f"[{f.id}] {f.name}  status={f.status} chunks={f.chunk_count}{mark}")
            if args.dry_run:
                continue
            old_chunk_ids, old_products = ingest.purge(session, f)
            ingest.ingest_file(session, f)
            session.refresh(f)
            print(f"      清理 chunk={len(old_chunk_ids)} product={old_products} → "
                  f"重建 chunk={f.chunk_count} status={f.status} error={f.error}")
        print(f"Chroma 向量总数: {vectorstore._collection().count()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
