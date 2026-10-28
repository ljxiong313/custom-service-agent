"""备份/恢复演练的单测（B3-8）：快照能生成、清单能核对、恢复真能把数据搬回来。

以前只验过"快照能生成且 integrity_check 通过"，从没演练过恢复——而没被恢复过的备份
只是"看起来像备份的文件"。这里把整条链路钉住：备份 → 校验清单 → 恢复到另一目录 →
比对行数，并验证恢复前会把原文件挪走而不是删掉（可逆性）。

脚本用 importlib 按路径加载：pyproject 的 testpaths 不收集 scripts/，
而 backup/restore 本质是运维脚本，不该为了可测而改写成服务内接口。
"""
from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest
from sqlmodel import Session

from app.db.models import Conversation, Message, User
from app.db.session import engine

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def backup_mod():
    return _load("backup_data")


@pytest.fixture(scope="module")
def restore_mod():
    return _load("restore_data")


@pytest.fixture
def seeded(db: Session) -> int:
    user = User(username="bk_owner", hashed_password="x", role="admin")
    db.add(user)
    db.commit()
    conv = Conversation(user_id=user.id, title="备份演练")
    db.add(conv)
    db.commit()
    for i in range(3):
        db.add(Message(conversation_id=conv.id, role="user", content=f"第{i}条"))
    db.commit()
    return int(conv.id)


def _db_url_path() -> Path:
    raw = engine.url.database
    assert raw
    p = Path(str(raw))
    return p if p.is_absolute() else (Path(__file__).resolve().parents[1] / p).resolve()


def test_backup_writes_manifest_and_snapshot(backup_mod, seeded, tmp_path: Path):
    out = tmp_path / "backups"
    assert backup_mod.run(str(out), keep=5, skip=["chroma"], dry_run=False) == 0
    snaps = list(out.glob("cs-agent-*"))
    assert len(snaps) == 1
    manifest = json.loads((snaps[0] / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["database"]["kind"] == "sqlite"
    assert manifest["database"]["messages"] >= 3
    assert (snaps[0] / manifest["database"]["file"]).exists()


def test_restore_roundtrip_moves_old_file_aside(restore_mod, backup_mod, seeded, tmp_path: Path):
    out = tmp_path / "backups"
    assert backup_mod.run(str(out), keep=5, skip=["chroma"], dry_run=False) == 0
    snapshot = next(out.glob("cs-agent-*"))

    # 校验必须先无问题
    assert restore_mod.verify(snapshot) == []

    target_dir = tmp_path / "data"
    target_dir.mkdir()
    db_name = json.loads((snapshot / "manifest.json").read_text(encoding="utf-8"))["database"]["file"]
    current = target_dir / db_name
    current.write_bytes(b"corrupted-placeholder")     # 现场有个坏文件，恢复要把它挪走而不是删掉

    manifest = json.loads((snapshot / "manifest.json").read_text(encoding="utf-8"))
    restore_mod.restore_sqlite(snapshot, manifest, apply=True, data_dir=target_dir)

    with sqlite3.connect(current) as conn:
        ok, = conn.execute("PRAGMA integrity_check").fetchone()
        messages, = conn.execute("SELECT count(*) FROM message").fetchone()
    assert ok == "ok" and int(messages) >= 3

    stashed = list(target_dir.glob(".restore-pre-*"))
    assert stashed and (stashed[0] / db_name).read_bytes() == b"corrupted-placeholder"
    # 恢复脚本必须把 journal_mode 置回 WAL：服务靠 WAL 才能并发读写
    with sqlite3.connect(current) as conn:
        mode, = conn.execute("PRAGMA journal_mode").fetchone()
    assert str(mode).lower() == "wal"


def _snapshot_via_backup_api(dest_dir: Path) -> Path:
    """按 backup_data 的正规方式做快照（折叠 WAL），供"清单不符"用例使用。"""
    src = _db_url_path()
    dest_dir.mkdir(parents=True, exist_ok=True)
    out = dest_dir / src.name
    source, target = sqlite3.connect(src), sqlite3.connect(out)
    try:
        source.backup(target)
    finally:
        source.close()
        target.close()
    return out


def test_load_manifest_requires_manifest(restore_mod, tmp_path: Path):
    empty = tmp_path / "cs-agent-20260101-000000"
    empty.mkdir()
    with pytest.raises(SystemExit):
        restore_mod.load_manifest(empty)


def test_verify_flags_count_drift(restore_mod, seeded, tmp_path: Path):
    snap = tmp_path / "cs-agent-20260102-000000"
    db = _snapshot_via_backup_api(snap)
    (snap / "manifest.json").write_text(json.dumps({
        "created_at": "x", "host": "h",
        "database": {"kind": "sqlite", "file": db.name, "tables": 999, "messages": 999},
        "directories": {"uploads": {"files": 42, "bytes": 1}},
    }, ensure_ascii=False), encoding="utf-8")
    problems = restore_mod.verify(snap)
    assert any("表数量与清单不符" in p for p in problems)
    assert any("message 行数与清单不符" in p for p in problems)
    assert any("uploads" in p for p in problems)


def test_verify_flags_torn_wal_copy(restore_mod, seeded, tmp_path: Path):
    """直接 cp 一个开着 WAL 的库 → 主文件可能是空的/残缺的，必须被 judge 成不可恢复。"""
    import shutil

    src = _db_url_path()
    snap = tmp_path / "cs-agent-20260103-000000"
    snap.mkdir()
    shutil.copy2(src, snap / src.name)
    (snap / "manifest.json").write_text(json.dumps({
        "created_at": "x", "host": "h",
        "database": {"kind": "sqlite", "file": src.name, "tables": 18, "messages": 3},
        "directories": {},
    }), encoding="utf-8")
    problems = restore_mod.verify(snap)
    # 残缺主文件会连 message 都查不到；已 checkpoint 的库则表现为行数与清单不符
    assert problems and any(("无法查询" in p) or ("不符" in p) for p in problems)


def test_prune_keeps_only_recent_snapshots(backup_mod, seeded, tmp_path: Path):
    out = tmp_path / "backups"
    for _ in range(4):
        assert backup_mod.run(str(out), keep=0, skip=["chroma", "uploads"], dry_run=False) == 0
    assert len(list(out.glob("cs-agent-*"))) == 4
    # keep=2 时下一次备份产出后只留 2 份（轮转在生成之后执行）
    assert backup_mod.run(str(out), keep=2, skip=["chroma", "uploads"], dry_run=False) == 0
    assert len(list(out.glob("cs-agent-*"))) == 2


def test_dry_run_touches_nothing(backup_mod, seeded, tmp_path: Path):
    out = tmp_path / "backups"
    assert backup_mod.run(str(out), keep=7, skip=[], dry_run=True) == 0
    assert not out.exists()
