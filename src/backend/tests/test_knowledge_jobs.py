"""B1-2/B1-4 知识库任务队列单测：状态落库、忙拒、中断回收、重试。

ingest_file 用假实现替换：真实入库要打 embedding API 和写 Chroma，
这里要验的是"任务行怎么变"，不是"向量怎么算"。
"""
from __future__ import annotations

import threading
import time

import pytest
from sqlmodel import Session, select

from app.db.models import IngestJob, KnowledgeFile
from app.db.session import engine
from app.services import knowledge_jobs
from app.services.rag import ingest

API = "/api/v1"


@pytest.fixture
def make_file(db: Session):
    """在临时磁盘文件上建一条 KnowledgeFile（不经过上传接口）。"""
    def _make(tmp_path, status: str = "ready", chunk_count: int = 1) -> KnowledgeFile:
        path = tmp_path / "doc.txt"
        path.write_text("商品名称=面霜，价格=39\n", encoding="utf-8")
        kf = KnowledgeFile(name=path.name, path=str(path), file_type="text",
                           size=path.stat().st_size, status=status, chunk_count=chunk_count)
        db.add(kf)
        db.commit()
        db.refresh(kf)
        return kf
    return _make


def _wait(db: Session, job_id: int, timeout: float = 15.0) -> IngestJob:
    deadline = time.time() + timeout
    while time.time() < deadline:
        db.expire_all()
        job = db.get(IngestJob, job_id)
        if job and job.status in ("done", "failed", "interrupted"):
            return job
        time.sleep(0.1)
    raise AssertionError(f"job#{job_id} 未在 {timeout}s 内结束")


# ---- 队列执行 ----

def test_upload_job_runs_to_done(tmp_path, make_file, monkeypatch):
    kf = make_file(tmp_path, status="parsing", chunk_count=0)
    calls = []

    def fake_ingest(session, file, *, purge_first=False, progress=None):
        calls.append((file.id, purge_first))
        file.chunk_count = 2
        file.status = "ready"
        session.add(file)
        session.commit()
        if progress:
            progress(2, 2)

    monkeypatch.setattr(ingest, "ingest_file", fake_ingest)
    job = knowledge_jobs.create_job(Session(engine), kf, knowledge_jobs.KIND_UPLOAD)
    knowledge_jobs.enqueue(job.id or 0)
    done = _wait(Session(engine), job.id)

    assert done.status == "done" and done.processed == 2 and done.total == 2
    assert calls == [(kf.id, False)]                 # 上传不清理旧数据
    with Session(engine) as s:
        fresh = s.get(KnowledgeFile, kf.id)
        assert fresh.status == "ready" and fresh.chunk_count == 2


def test_reingest_job_purges_first(tmp_path, make_file, monkeypatch):
    kf = make_file(tmp_path)
    seen = {}

    def fake_ingest(session, file, *, purge_first=False, progress=None):
        seen["purge_first"] = purge_first
        file.status = "ready"
        session.add(file)
        session.commit()

    monkeypatch.setattr(ingest, "ingest_file", fake_ingest)
    job = knowledge_jobs.create_job(Session(engine), kf, knowledge_jobs.KIND_REINGEST)
    knowledge_jobs.enqueue(job.id or 0)
    _wait(Session(engine), job.id)
    assert seen["purge_first"] is True               # 重入库必须先在任务里清旧切片


def test_failed_job_records_error(tmp_path, make_file, monkeypatch):
    kf = make_file(tmp_path, status="parsing")

    def boom(session, file, *, purge_first=False, progress=None):
        raise RuntimeError("向量化失败：batch size is invalid")

    monkeypatch.setattr(ingest, "ingest_file", boom)
    job = knowledge_jobs.create_job(Session(engine), kf, knowledge_jobs.KIND_UPLOAD)
    knowledge_jobs.enqueue(job.id or 0)
    done = _wait(Session(engine), job.id)

    assert done.status == "failed" and "batch size" in (done.error or "")
    with Session(engine) as s:
        assert s.get(KnowledgeFile, kf.id).status == "failed"   # 失败要落到文件上


# ---- 接口层：受理、忙拒、重试 ----

def test_reingest_accepted_then_queued(client, admin, tmp_path, make_file, monkeypatch):
    kf = make_file(tmp_path)
    release = threading.Event()
    finished = threading.Event()

    def blocking_ingest(session, file, *, purge_first=False, progress=None):
        release.wait(10)                      # 卡在任务里，让"忙拒"分支真的处于 running
        file.status = "ready"
        session.add(file)
        session.commit()
        finished.set()

    monkeypatch.setattr(ingest, "ingest_file", blocking_ingest)
    r = client.post(f"{API}/knowledge/files/{kf.id}/reingest", headers=admin["headers"])
    assert r.status_code == 200 and r.json()["id"] == kf.id   # 响应体必须带 id（前端靠它轮询进度）
    # worker 抢得比响应序列化还快时已经是 parsing，所以只断言"排队或已在跑"，不锁死 pending
    assert r.json()["status"] in ("pending", "parsing"), r.json()

    deadline = time.time() + 10
    running = False
    while time.time() < deadline and not running:
        with Session(engine) as s:
            job = s.exec(select(IngestJob).where(IngestJob.file_id == kf.id)).first()
            running = job is not None and job.status == "running"
        time.sleep(0.05)
    assert running, "任务没被 worker 取走：检查 rag.ingest_workers 与队列启动"

    try:
        busy = client.post(f"{API}/knowledge/files/{kf.id}/reingest", headers=admin["headers"])
        assert busy.status_code == 409 and "正在" in busy.json()["detail"]
        assert client.delete(f"{API}/knowledge/files/{kf.id}", headers=admin["headers"]).status_code == 409
        assert client.post(f"{API}/knowledge/files/{kf.id}/reindex", headers=admin["headers"]).status_code == 409
    finally:
        release.set()
        assert finished.wait(10)                    # 别让 worker 带着写表动作进入清表阶段


def test_jobs_listing_shape(client, admin, tmp_path, make_file):
    kf = make_file(tmp_path)
    with Session(engine) as s:
        knowledge_jobs.create_job(s, kf, knowledge_jobs.KIND_REINDEX, total=3)

    body = client.get(f"{API}/knowledge/jobs", headers=admin["headers"]).json()
    assert body["pending"] >= 1
    row = next(j for j in body["items"] if j["file_id"] == kf.id)
    assert row["kind"] == "reindex" and row["total"] == 3 and row["file_name"] == kf.name
    assert {"id", "status", "processed", "error", "created_at", "updated_at"} <= set(row)


def test_retry_requeues_failed_job(client, admin, tmp_path, make_file, monkeypatch):
    kf = make_file(tmp_path, status="failed")
    monkeypatch.setattr(ingest, "ingest_file",
                        lambda session, file, **k: (setattr(file, "status", "ready"),
                                                    session.add(file), session.commit()))
    with Session(engine) as s:
        job = knowledge_jobs.create_job(s, kf, knowledge_jobs.KIND_UPLOAD)
        job.status = "failed"
        job.error = "向量化失败"
        s.add(job)
        s.commit()
        jid = job.id

    r = client.post(f"{API}/knowledge/jobs/{jid}/retry", headers=admin["headers"])
    assert r.status_code == 200 and r.json()["id"] == kf.id
    assert _wait(Session(engine), jid).status == "done"


def test_retry_validation(client, admin, tmp_path, make_file):
    assert client.post(f"{API}/knowledge/jobs/999999/retry", headers=admin["headers"]).status_code == 404
    kf = make_file(tmp_path)
    with Session(engine) as s:
        job = knowledge_jobs.create_job(s, kf, knowledge_jobs.KIND_UPLOAD)
        job.status = "done"
        s.add(job)
        s.commit()
        jid = job.id
    r = client.post(f"{API}/knowledge/jobs/{jid}/retry", headers=admin["headers"])
    assert r.status_code == 409 and "中断或失败" in r.json()["detail"]


def test_retry_refuses_missing_source(client, admin, tmp_path, make_file):
    kf = make_file(tmp_path, status="failed")
    (tmp_path / "doc.txt").unlink()
    with Session(engine) as s:
        job = knowledge_jobs.create_job(s, kf, knowledge_jobs.KIND_UPLOAD)
        job.status = "interrupted"
        s.add(job)
        s.commit()
        jid = job.id
    r = client.post(f"{API}/knowledge/jobs/{jid}/retry", headers=admin["headers"])
    assert r.status_code == 409 and "重新上传" in r.json()["detail"]


# ---- 重启回收（B1-4 的卡死根因）----

def test_recover_interrupted_jobs(tmp_path, make_file):
    kf = make_file(tmp_path, status="parsing")
    with Session(engine) as s:
        running = knowledge_jobs.create_job(s, kf, knowledge_jobs.KIND_REINGEST)
        other = KnowledgeFile(name="ready.txt", path="x", file_type="text", status="ready")
        s.add(other)
        s.commit()
        s.refresh(other)
        done = knowledge_jobs.create_job(s, other, knowledge_jobs.KIND_UPLOAD)
        done.status = "done"
        s.add(done)
        s.commit()
        running_id, done_id, other_id = running.id, done.id, other.id

    assert knowledge_jobs.recover_interrupted_jobs() == 1
    with Session(engine) as s:
        assert s.get(IngestJob, running_id).status == "interrupted"
        assert s.get(IngestJob, done_id).status == "done"          # 已完成任务不受影响
        assert s.get(KnowledgeFile, kf.id).status == "failed"      # 文件同步脱离 parsing
        assert s.get(KnowledgeFile, other_id).status == "ready"


def test_stale_parsing_no_longer_blocks(client, admin, tmp_path, make_file, monkeypatch):
    """没有活动任务却停在 parsing（上次进程被打断）：先回收再报错，第二次点就能过。"""
    kf = make_file(tmp_path, status="parsing")
    monkeypatch.setattr(ingest, "ingest_file", lambda *a, **k: None)
    first = client.post(f"{API}/knowledge/files/{kf.id}/reingest", headers=admin["headers"])
    assert first.status_code == 409 and "状态已回收" in first.json()["detail"]
    with Session(engine) as s:
        assert s.get(KnowledgeFile, kf.id).status == "failed"
    second = client.post(f"{API}/knowledge/files/{kf.id}/reingest", headers=admin["headers"])
    assert second.status_code == 200, second.text


def test_active_job_ids_and_counts(tmp_path, make_file):
    kf = make_file(tmp_path)
    with Session(engine) as s:
        knowledge_jobs.create_job(s, kf, knowledge_jobs.KIND_UPLOAD)
        j2 = knowledge_jobs.create_job(s, kf, knowledge_jobs.KIND_REINDEX)
        j2.status = "done"
        s.add(j2)
        s.commit()
        assert knowledge_jobs.active_job_ids(s) == {kf.id}
        assert knowledge_jobs.pending_count(s) == 1
        counts = knowledge_jobs.status_counts(s)
        assert counts["queued"] == 1 and counts["done"] == 1


def test_delete_file_removes_its_jobs(tmp_path, make_file, client, admin):
    """IngestJob 有外键到 KnowledgeFile：删文件不清任务行会被 PRAGMA foreign_keys=ON 拦下。"""
    kf = make_file(tmp_path)
    with Session(engine) as s:
        job = knowledge_jobs.create_job(s, kf, knowledge_jobs.KIND_UPLOAD)
        job.status = "done"
        s.add(job)
        s.commit()
        jid = job.id
    assert client.delete(f"{API}/knowledge/files/{kf.id}", headers=admin["headers"]).status_code == 204
    with Session(engine) as s:
        assert s.get(IngestJob, jid) is None
        assert s.exec(select(IngestJob)).all() == []
