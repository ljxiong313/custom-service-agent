"""备份恢复演练：把某份快照还原回去，并核对 manifest 里的行数是否对得上。

    cd src/backend
    CS_CONFIG=config.local.yaml py scripts/restore_data.py --list
    CS_CONFIG=config.local.yaml py scripts/restore_data.py --snapshot data/backups/cs-agent-20261027-0930 --verify-only
    CS_CONFIG=config.local.yaml py scripts/restore_data.py --snapshot ... --yes

为什么单独写这个脚本：备份脚本自己测过"快照能生成、integrity_check 通过"，但**从没演练过
恢复**——没被恢复过的备份只是"看起来像备份的文件"。第一次真恢复往往发生在事故现场，
那时候才发现缺列、缺目录、权限不对、config 里的路径指向别的机器，代价最高。

安全约束（都是可逆性考虑）：
- 必须显式 `--yes` 才动数据目录，默认只做校验（`--verify-only`）；
- 恢复前把当前 data 下的目标整体挪进 `data/.restore-pre-<时间戳>/`，不删任何东西，
  搞砸了还能挪回来；
- 只覆盖快照里存在的东西，快照里没有的文件保持原样（不做"目录同步删除"）。

SQLite 走文件替换（并清掉 -wal/-shm，否则旧 WAL 会把已回滚的写再放回来）；
MySQL/PostgreSQL 需要目标库为空库或你明确接受追加，脚本只打印应当执行的命令、
不代你连生产库灌数据——把别人的生产库覆盖掉是不可逆操作，必须人来做。
"""
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import BACKEND_ROOT  # noqa: E402
from app.db.session import engine  # noqa: E402

DATA_DIR = BACKEND_ROOT / "data"
MANIFEST_NAME = "manifest.json"
SNAPSHOT_PREFIX = "cs-agent-"
RESTORE_DIRS = ("chroma", "uploads")
CONFIG_GLOBS = ("config.simple.yaml", "config.local.yaml")


def list_snapshots(out_root: Path) -> list[Path]:
    return sorted(p for p in out_root.glob(f"{SNAPSHOT_PREFIX}*") if p.is_dir())


def load_manifest(snapshot: Path) -> dict:
    path = snapshot / MANIFEST_NAME
    if not path.exists():
        raise SystemExit(f"快照缺少 {MANIFEST_NAME}：{snapshot}\n"
                         "旧版备份脚本产出的快照没有清单，只能人工核对内容后再恢复。")
    return json.loads(path.read_text(encoding="utf-8"))


def verify_sqlite(db_file: Path, manifest: dict) -> list[str]:
    """核对快照库：完整性 + 表数 + 行数是否与备份当时记录的清单一致。"""
    problems: list[str] = []
    conn = sqlite3.connect(db_file)
    try:
        ok, = conn.execute("PRAGMA integrity_check").fetchone()
        tables, = conn.execute("SELECT count(*) FROM sqlite_master WHERE type='table'").fetchone()
        messages, = conn.execute("SELECT count(*) FROM message").fetchone()
    except sqlite3.OperationalError as e:
        # "no such table: message" 是快照不可用的典型信号：直接 cp 一个开着 WAL 的库，
        # 拿到的主文件可能还没吸收 WAL 里的写入。这种快照必须判死，不能当"能恢复"。
        return [f"快照库无法查询（很可能是直接复制 WAL 库导致的残缺文件）: {e}"]
    finally:
        conn.close()   # 不关就占着文件句柄，Windows 上后续 rmtree 会失败
    recorded = manifest.get("database", {})
    if ok != "ok":
        problems.append(f"integrity_check 返回 {ok}")
    if recorded.get("tables") is not None and int(recorded["tables"]) != int(tables):
        problems.append(f"表数量与清单不符：清单 {recorded['tables']}，实际 {tables}")
    if recorded.get("messages") is not None and int(recorded["messages"]) != int(messages):
        problems.append(f"message 行数与清单不符：清单 {recorded['messages']}，实际 {messages}")
    print(f"快照库: {db_file.name}  integrity={ok} tables={tables} messages={messages}")
    return problems


def verify(snapshot: Path) -> list[str]:
    manifest = load_manifest(snapshot)
    db = manifest.get("database", {})
    problems: list[str] = []
    print(f"清单: created_at={manifest.get('created_at')} host={manifest.get('host')} "
          f"db={db.get('kind')}")
    if db.get("kind") == "sqlite":
        f = snapshot / str(db.get("file"))
        if not f.exists():
            return [f"清单指向的库文件不存在: {f}"]
        problems += verify_sqlite(f, manifest)
    else:
        print(f"外部库快照: {db.get('file')}（{db.get('bytes', 0)} bytes，工具 {db.get('tool')}）")
        if not (snapshot / str(db.get("file"))).exists():
            problems.append(f"dump 文件不存在: {db.get('file')}")
    for name in RESTORE_DIRS:
        stats = manifest.get("directories", {}).get(name)
        target = snapshot / name
        if stats is None:
            print(f"{name}: 清单里没有（备份时被 --skip 跳过了）")
            continue
        if not target.exists():
            problems.append(f"清单记录 {name} 有 {stats.get('files')} 个文件，快照里却没有该目录")
            continue
        files = sum(1 for p in target.rglob("*") if p.is_file())
        if files != stats.get("files"):
            problems.append(f"{name} 文件数与清单不符：清单 {stats.get('files')}，实际 {files}")
        else:
            print(f"{name}: {files} 个文件（与清单一致）")
    return problems


def _stash(current: Path, stash_root: Path) -> Path:
    """把现有目标挪走而不是删掉：恢复失败时唯一能把现场还原的东西就是这个。"""
    stash_root.mkdir(parents=True, exist_ok=True)
    dest = stash_root / current.name
    if dest.exists():
        dest = stash_root / f"{current.name}-{int(time.time())}"
    current.rename(dest)
    return dest


def restore_sqlite(snapshot: Path, manifest: dict, apply: bool,
                 data_dir: Path = DATA_DIR) -> None:
    db_name = str(manifest["database"]["file"])
    src = snapshot / db_name
    current = data_dir / db_name
    print(f"数据库: {src} → {current if apply else '（verify-only，未写入）'}")
    if not apply:
        return
    stash = data_dir / f".restore-pre-{time.strftime('%Y%m%d-%H%M%S')}"
    if current.exists():
        moved = _stash(current, stash)
        print(f"  原库已挪到: {moved}")
        for suffix in ("-wal", "-shm"):   # 旧 WAL 会把已经回滚掉的写再放回来
            stale = current.with_name(current.name + suffix)
            if stale.exists():
                stale.rename(moved.with_name(moved.name + suffix))
    shutil.copy2(src, current)
    conn = sqlite3.connect(current)
    try:
        ok, = conn.execute("PRAGMA integrity_check").fetchone()
        foreign, = conn.execute("PRAGMA foreign_keys").fetchone()
        conn.execute("PRAGMA journal_mode=WAL")
    finally:
        conn.close()   # 不关就占着句柄，后续 rmtree/轮转会在 Windows 上失败
    print(f"  恢复后校验: integrity={ok} foreign_keys={foreign}（journal_mode 已置回 WAL）")


def restore_dirs(snapshot: Path, manifest: dict, apply: bool,
               data_dir: Path = DATA_DIR) -> None:
    for name in RESTORE_DIRS:
        src = snapshot / name
        if not src.exists():
            continue
        current = data_dir / name
        print(f"{name}: {src} → {current if apply else '（verify-only，未写入）'}")
        if not apply:
            continue
        stash = data_dir / f".restore-pre-{time.strftime('%Y%m%d-%H%M%S')}"
        if current.exists():
            print(f"  原目录已挪到: {_stash(current, stash)}")
        shutil.copytree(src, current)


def restore_config(snapshot: Path, apply: bool) -> None:
    """配置文件默认**不**自动覆盖：快照里的 config.local.yaml 是备份那台机器的路径与密钥。"""
    found = [snapshot / g for g in CONFIG_GLOBS if (snapshot / g).exists()]
    if not found:
        return
    print("配置快照存在（未自动覆盖，恢复方机器路径/密钥通常不同）:")
    for f in found:
        print(f"  {f.name} → 需要时手工复制，复制后务必跑 scripts/verify_restore.py 复核")
    if apply:
        print("  要恢复配置请手工 cp，本脚本不代你做这一步")


def print_external_commands(manifest: dict) -> None:
    db = manifest.get("database", {})
    if db.get("kind") == "mysql":
        print("外部库恢复（请人工执行，先确认目标库可被覆盖）：\n"
              f"  mysql -h {db.get('host', '127.0.0.1')} -u <user> -p <db> < {db.get('file')}")
    elif db.get("kind") == "postgres":
        print("外部库恢复（请人工执行，先确认目标库可被覆盖）：\n"
              f"  pg_restore --dbname <db> --no-owner --no-privileges {db.get('file')}")


def main() -> int:
    parser = argparse.ArgumentParser(description="恢复演练：校验快照并按需还原 data/")
    parser.add_argument("--out", default=str(DATA_DIR / "backups"), help="快照根目录")
    parser.add_argument("--snapshot", help="要恢复的快照目录")
    parser.add_argument("--list", action="store_true", help="列出可用快照（含清单摘要）")
    parser.add_argument("--verify-only", action="store_true", help="只校验，不写 data/")
    parser.add_argument("--yes", action="store_true", help="确认执行恢复（会先挪走现有数据）")
    args = parser.parse_args()

    out_root = Path(args.out)
    if args.list or not args.snapshot:
        snaps = list_snapshots(out_root)
        if not snaps:
            print(f"{out_root} 下没有快照（先跑 scripts/backup_data.py）")
            return 1
        for s in snaps:
            try:
                m = load_manifest(s)
                db = m.get("database", {})
                print(f"{s.name}: {m.get('created_at')} db={db.get('kind')} "
                      f"messages={db.get('messages')} dirs={list(m.get('directories', {}))}")
            except SystemExit as e:
                print(f"{s.name}: {e}")
        print("\n用 --snapshot <路径> 校验或恢复其中一份")
        return 0

    snapshot = Path(args.snapshot)
    if not snapshot.is_absolute():
        snapshot = (out_root / args.snapshot).resolve()
    if not snapshot.exists():
        raise SystemExit(f"快照不存在: {snapshot}")

    problems = verify(snapshot)
    if problems:
        print("校验发现问题：\n  - " + "\n  - ".join(problems))
        print("为安全起见不执行恢复；确认无误后用 --force-checksum 前先人工看上面的差异")
        return 2

    manifest = load_manifest(snapshot)
    apply = bool(args.yes) and not args.verify_only
    if not apply:
        print("\n[verify-only] 未改动 data/。要真恢复请加 --yes（当前数据会被挪到 .restore-pre-*）")
        print_external_commands(manifest)
        return 0

    if manifest["database"].get("kind") != "sqlite":
        print_external_commands(manifest)
        raise SystemExit("外部数据库不代做灌库：请按上面的命令手工恢复后再跑本脚本恢复目录")

    print(f"\n开始恢复（{engine.url.drivername}）；快照 {snapshot}")
    restore_sqlite(snapshot, manifest, apply=True)
    restore_dirs(snapshot, manifest, apply=True)
    restore_config(snapshot, apply=True)
    print("\n恢复完成。下一步必须做：\n"
          "  1) 启动服务（CS_CONFIG 指向本机配置），确认 /health 与 /ready 通过\n"
          "  2) py scripts/verify_restore.py  # 对账 SQLite Chunk 表与 Chroma 向量、抽样检索\n"
          "  3) 检索与一页看一两个历史会话，确认引用与图片附件能打开")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
