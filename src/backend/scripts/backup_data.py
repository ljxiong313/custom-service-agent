"""数据备份：SQLite 一致性快照 + Chroma/上传文件/配置归档，按份数轮转清理。

    cd src/backend
    py scripts/backup_data.py --dry-run
    CS_CONFIG=config.local.yaml py scripts/backup_data.py --keep 7

为什么不是复制 db 文件：服务开着 WAL，最近写入还停在 `cs_agent.db-wal` 里，手抄
`cs_agent.db` + `-wal` 很容易拿到一对时间上不对齐的文件。sqlite3 的 backup API 在 SQL
层边读边把 WAL 折进目标库，服务不停也能拿到一致快照（停了只是更快、更省磁盘）。

Chroma 目录另说：PersistentClient 没有在线快照接口，运行中拷出来的索引可能撕裂。
可以接受，因为 Chunk 表才是唯一真相，向量随时能用 scripts/reindex_vectors.py 重建；
要严格一致就先停服务再跑本脚本。备份里的 config.local.yaml 是明文密钥，归档只能放在
本机或加密介质上。
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import BACKEND_ROOT, get_settings  # noqa: E402
from app.db.session import engine  # noqa: E402  导入即按配置把 sqlite 路径锚定到后端根目录

DATA_DIR = BACKEND_ROOT / "data"
SNAPSHOT_PREFIX = "cs-agent-"


def _sqlite_path() -> Path | None:
    """非 SQLite（比如切了 MySQL）时返回 None：那种库该用数据库自己的备份工具。"""
    if engine.url.drivername != "sqlite":
        return None
    raw = engine.url.database
    assert raw is not None, "sqlite URL 缺少库文件路径"
    path = Path(raw)
    return path if path.is_absolute() else (BACKEND_ROOT / path).resolve()


def _dir_size(src: Path) -> str:
    files = [f for f in src.rglob("*") if f.is_file()]
    return f"{len(files)} 个文件 / {sum(f.stat().st_size for f in files) // 1024}KB"


def _backup_sqlite(src: Path, dst: Path) -> str:
    """backup API 复制：源库可以正被服务写着，目标库是折叠过 WAL 的完整快照。"""
    with sqlite3.connect(src) as source, sqlite3.connect(dst) as target:
        source.backup(target)
    with sqlite3.connect(dst) as check:
        ok, = check.execute("PRAGMA integrity_check").fetchone()
        tables, = check.execute("SELECT count(*) FROM sqlite_master WHERE type='table'").fetchone()
        messages, = check.execute("SELECT count(*) FROM message").fetchone()
    if ok != "ok":
        raise SystemExit(f"备份库完整性校验失败: {ok}（{dst}）")
    return f"integrity=ok tables={tables} messages={messages}"


def _prune(out_root: Path, keep: int) -> list[str]:
    """按目录名轮转（时间戳前缀可字典序排序），只删本脚本产出的快照目录。"""
    if keep <= 0:
        return []
    snapshots = sorted(p for p in out_root.glob(f"{SNAPSHOT_PREFIX}*") if p.is_dir())
    removed = []
    for old in snapshots[:-keep]:
        shutil.rmtree(old)
        removed.append(old.name)
    return removed


def _claim(out_root: Path, stem: str) -> Path:
    """同一秒里连跑两次也要各占一格：时间戳撞了就从 -2 往后找空位。"""
    candidate = out_root / stem
    nth = 1
    while candidate.exists():
        nth += 1
        candidate = out_root / f"{stem}-{nth}"
    return candidate


def main() -> int:
    parser = argparse.ArgumentParser(description="备份 SQLite / Chroma / 上传文件 / 配置")
    parser.add_argument("--out", default=str(DATA_DIR / "backups"), help="快照根目录")
    parser.add_argument("--keep", type=int, default=7, help="保留最近几份，0 表示不清理")
    parser.add_argument("--skip", action="append", default=[], choices=["chroma", "uploads", "config"],
                        help="跳过大目录（chroma 可用 scripts/reindex_vectors.py 重建）")
    parser.add_argument("--dry-run", action="store_true", help="只打印将要做的事")
    args = parser.parse_args()

    db_path = _sqlite_path()
    if db_path is None:
        raise SystemExit(f"当前 database.url 不是 SQLite（{engine.url.drivername}），"
                         "请用数据库自带的备份工具；本脚本只处理单文件 SQLite。")
    if not db_path.exists():
        raise SystemExit(f"数据库文件不存在: {db_path}（先启动一次服务建库）")

    out_root = Path(args.out)
    snapshot = _claim(out_root, SNAPSHOT_PREFIX + time.strftime("%Y%m%d-%H%M%S"))
    print(f"配置文件: {get_settings().get('_config_file')}")
    print(f"快照目录: {snapshot}" + ("  [dry-run]" if args.dry_run else ""))
    print(f"数据库:   {db_path}")

    if args.dry_run:
        for name, src in (("chroma", DATA_DIR / "chroma"), ("uploads", DATA_DIR / "uploads")):
            if name not in args.skip and src.exists():
                print(f"  {name}: {_dir_size(src)}")
        if "config" not in args.skip:
            print(f"  config: {[p.name for p in BACKEND_ROOT.glob('config*.yaml')]}")
        print(f"轮转: 保留最近 {args.keep} 份" if args.keep else "轮转: 关闭")
        return 0

    snapshot.mkdir(parents=True)
    print(f"数据库:   {_backup_sqlite(db_path, snapshot / db_path.name)}")

    for name, src in (("chroma", DATA_DIR / "chroma"), ("uploads", DATA_DIR / "uploads")):
        if name in args.skip:
            print(f"{name}: 已跳过")
        elif not src.exists():
            print(f"{name}: 目录不存在，跳过")
        else:
            shutil.copytree(src, snapshot / name)
            print(f"{name}: {_dir_size(src)}")

    if "config" not in args.skip:
        configs = list(BACKEND_ROOT.glob("config*.yaml"))
        for conf in configs:
            shutil.copy2(conf, snapshot / conf.name)
        warned = "（含明文密钥，勿外传）" if any(p.name == "config.local.yaml" for p in configs) else ""
        print(f"config: {len(configs)} 个文件" + warned)

    removed = _prune(out_root, args.keep)
    print(f"完成: {snapshot}")
    print(f"轮转: 删除 {len(removed)} 份 → {', '.join(removed)}" if removed else "轮转: 无需清理")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
