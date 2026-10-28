"""数据备份：一致性快照 + Chroma/上传文件/配置归档，按份数轮转清理，并写 manifest。

    cd src/backend
    py scripts/backup_data.py --dry-run
    CS_CONFIG=config.local.yaml py scripts/backup_data.py --keep 7

支持三种库：
- SQLite：用 sqlite3 的 backup API。服务开着 WAL 时最近写入还在 `cs_agent.db-wal` 里，
  手抄 `.db` + `-wal` 很容易拿到一对时间上不对齐的文件；backup API 在 SQL 层边读边把
  WAL 折进目标库，不停服务也能拿到一致快照（停了只是更快、更省磁盘）。
- MySQL：调 `mysqldump --single-transaction`（InnoDB 下不锁表）。
- PostgreSQL：调 `pg_dump`。
  两者的口令都不走命令行参数（`ps` 能看见），MySQL 用临时 `--defaults-extra-file`
  的 0600 配置文件，PostgreSQL 用只传给子进程的 PGPASSWORD 环境变量。

Chroma 目录另说：PersistentClient 没有在线快照接口，运行中拷出来的索引可能撕裂。
可以接受，因为 Chunk 表才是唯一真相，向量随时能用 scripts/reindex_vectors.py 重建；
要严格一致就先停服务再跑本脚本。备份里的 config.local.yaml 是明文密钥，归档只能放在
本机或加密介质上。

每份快照都有 manifest.json（库类型、行数、大小、工具版本、时间）。没有它，"这份备份
到底能不能用"只能靠恢复那天现场赌；scripts/restore_data.py 会核对它。
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import unquote

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import BACKEND_ROOT, get_settings  # noqa: E402
from app.db.session import engine  # noqa: E402  导入即按配置把 sqlite 路径锚定到后端根目录

DATA_DIR = BACKEND_ROOT / "data"
SNAPSHOT_PREFIX = "cs-agent-"
MANIFEST_NAME = "manifest.json"


def _sqlite_path() -> Path | None:
    """当前是 SQLite 时返回库文件绝对路径，否则 None（走外部 dump 分支）。"""
    if engine.url.drivername != "sqlite":
        return None
    raw = engine.url.database
    assert raw is not None, "sqlite URL 缺少库文件路径"
    path = Path(unquote(raw))
    return path if path.is_absolute() else (BACKEND_ROOT / path).resolve()


def _dir_size(src: Path) -> str:
    files = [f for f in src.rglob("*") if f.is_file()]
    return f"{len(files)} 个文件 / {sum(f.stat().st_size for f in files) // 1024}KB"


def _dir_stats(src: Path) -> dict:
    files = [f for f in src.rglob("*") if f.is_file()]
    return {"files": len(files), "bytes": sum(f.stat().st_size for f in files)}


def _backup_sqlite(src: Path, dst: Path) -> dict:
    """backup API 复制：源库可以正被服务写着，目标库是折叠过 WAL 的完整快照。"""
    # sqlite3 连接的 `with` 只负责提交/回滚，**不会 close**：Windows 上没关的连接会一直
    # 占着文件句柄，导致 _prune() 删旧快照时抛 WinError 32。必须显式 close。
    source = sqlite3.connect(src)
    target = sqlite3.connect(dst)
    try:
        source.backup(target)
    finally:
        source.close()
        target.close()
    check = sqlite3.connect(dst)
    try:
        ok, = check.execute("PRAGMA integrity_check").fetchone()
        tables, = check.execute("SELECT count(*) FROM sqlite_master WHERE type='table'").fetchone()
        messages, = check.execute("SELECT count(*) FROM message").fetchone()
    finally:
        check.close()
    if ok != "ok":
        raise SystemExit(f"备份库完整性校验失败: {ok}（{dst}）")
    print(f"数据库:   integrity=ok tables={tables} messages={messages}")
    return {"kind": "sqlite", "file": dst.name, "integrity": ok,
            "tables": int(tables), "messages": int(messages)}


def _run(cmd: list[str], env: dict | None = None) -> str:
    proc = subprocess.run(cmd, capture_output=True, text=True, env=env)
    if proc.returncode != 0:
        raise SystemExit(f"外部备份命令失败（{cmd[0]} rc={proc.returncode}）: {proc.stderr[:400]}")
    return proc.stdout


def _backup_mysql(dst_dir: Path) -> dict:
    """mysqldump --single-transaction：InnoDB 一致性快照且不锁表。"""
    url = engine.url
    host = url.host or "127.0.0.1"
    port = url.port or 3306
    database = unquote(str(url.database))
    dump = dst_dir / f"{database}.sql"
    out_file: Path | None = None
    env = dict(os.environ)
    args = ["mysqldump", f"--host={host}", f"--port={port}", f"--user={unquote(str(url.username))}",
            "--single-transaction", "--quick", "--routines", "--triggers", database]
    if url.password:
        # 口令绝不放命令行（同机任何用户 ps 就能看见）：写一份 0600 的 defaults 文件
        fd, out_file = tempfile.mkstemp(prefix="cs-backup-my", suffix=".cnf")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            os.chmod(out_file, 0o600)
            fh.write(f"[client]\npassword={url.password}\n")
        args.insert(1, f"--defaults-extra-file={out_file}")
    try:
        with open(dump, "w", encoding="utf-8") as fh:
            proc = subprocess.run(args, stdout=fh, stderr=subprocess.PIPE, text=True, env=env)
        if proc.returncode != 0:
            raise SystemExit(f"mysqldump 失败: {proc.stderr[:400]}")
    finally:
        if out_file:
            Path(out_file).unlink(missing_ok=True)
    version = _run(["mysqldump", "--version"]).strip()
    return {"kind": "mysql", "file": dump.name, "bytes": dump.stat().st_size,
            "tool": version, "database": database, "host": host}


def _backup_postgres(dst_dir: Path) -> dict:
    """pg_dump 自定义格式（-Fc），恢复用 pg_restore 可选择性重建。"""
    url = engine.url
    host = url.host or "127.0.0.1"
    port = str(url.port or 5432)
    database = unquote(str(url.database))
    dump = dst_dir / f"{database}.dump"
    env = dict(os.environ)
    env["PGPASSWORD"] = unquote(str(url.password or ""))
    env["PGHOST"] = host
    env["PGPORT"] = port
    env["PGUSER"] = unquote(str(url.username or "postgres"))
    args = ["pg_dump", "--format=custom", "--no-owner", "--no-privileges",
            "--file", str(dump), database]
    proc = subprocess.run(args, capture_output=True, text=True, env=env)
    env.pop("PGPASSWORD", None)
    if proc.returncode != 0:
        raise SystemExit(f"pg_dump 失败: {proc.stderr[:400]}")
    version = _run(["pg_dump", "--version"]).strip()
    return {"kind": "postgres", "file": dump.name, "bytes": dump.stat().st_size,
            "tool": version, "database": database, "host": host}


def backup_database(dst_dir: Path) -> dict:
    driver = engine.url.drivername
    if driver.startswith("sqlite"):
        src = _sqlite_path()
        assert src is not None
        if not src.exists():
            raise SystemExit(f"数据库文件不存在: {src}（先启动一次服务建库）")
        return _backup_sqlite(src, dst_dir / src.name)
    if driver.startswith("mysql"):
        return _backup_mysql(dst_dir)
    if driver.startswith(("postgres", "postgresql")):
        return _backup_postgres(dst_dir)
    raise SystemExit(f"未支持的数据库类型: {driver}")


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


def run(out: str, keep: int, skip: list[str], dry_run: bool) -> int:
    db_driver = engine.url.drivername
    sqlite_src = _sqlite_path()
    print(f"配置文件: {get_settings().get('_config_file')}")
    print(f"数据库:   {db_driver}{f' → {sqlite_src}' if sqlite_src else ''}")

    out_root = Path(out)
    snapshot = _claim(out_root, SNAPSHOT_PREFIX + time.strftime("%Y%m%d-%H%M%S"))
    print(f"快照目录: {snapshot}" + ("  [dry-run]" if dry_run else ""))

    plan = {"chroma": DATA_DIR / "chroma", "uploads": DATA_DIR / "uploads"}
    if dry_run:
        for name, src in plan.items():
            if name not in skip and src.exists():
                print(f"  {name}: {_dir_size(src)}")
        if "config" not in skip:
            print(f"  config: {[p.name for p in BACKEND_ROOT.glob('config*.yaml')]}")
        print(f"轮转: 保留最近 {keep} 份" if keep else "轮转: 关闭")
        return 0

    snapshot.mkdir(parents=True)
    manifest: dict = {
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "host": platform.node(),
        "python": sys.version.split()[0],
        "config_file": str(get_settings().get("_config_file")),
        "database": backup_database(snapshot),
        "directories": {},
        "config_files": [],
    }

    for name, src in plan.items():
        if name in skip:
            print(f"{name}: 已跳过")
        elif not src.exists():
            print(f"{name}: 目录不存在，跳过")
        else:
            shutil.copytree(src, snapshot / name)
            manifest["directories"][name] = _dir_stats(snapshot / name)
            print(f"{name}: {_dir_size(src)}")

    if "config" not in skip:
        configs = list(BACKEND_ROOT.glob("config*.yaml"))
        for conf in configs:
            shutil.copy2(conf, snapshot / conf.name)
        manifest["config_files"] = [p.name for p in configs]
        manifest["config_contains_secrets"] = any(p.name == "config.local.yaml" for p in configs)
        warned = "（含明文密钥，勿外传）" if manifest["config_contains_secrets"] else ""
        print(f"config: {len(configs)} 个文件" + warned)

    (snapshot / MANIFEST_NAME).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    removed = _prune(out_root, keep)
    print(f"完成: {snapshot}")
    print(f"清单: {snapshot / MANIFEST_NAME}")
    print(f"轮转: 删除 {len(removed)} 份 → {', '.join(removed)}" if removed else "轮转: 无需清理")
    return 0


def args_dry(dry_run: bool) -> str:
    return "  [dry-run]" if dry_run else ""


def main() -> int:
    parser = argparse.ArgumentParser(description="备份数据库快照 / Chroma / 上传文件 / 配置")
    parser.add_argument("--out", default=str(DATA_DIR / "backups"), help="快照根目录")
    parser.add_argument("--keep", type=int, default=7, help="保留最近几份，0 表示不清理")
    parser.add_argument("--skip", action="append", default=[], choices=["chroma", "uploads", "config"],
                        help="跳过大目录（chroma 可用 scripts/reindex_vectors.py 重建）")
    parser.add_argument("--dry-run", action="store_true", help="只打印将要做的事")
    args = parser.parse_args()
    return run(args.out, args.keep, args.skip, args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
