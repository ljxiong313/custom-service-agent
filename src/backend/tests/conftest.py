"""离线单测公共设施：临时配置 + 临时 SQLite，全程不联网、不依赖 7861 上的服务。

为什么要在导入 app 之前改 CS_CONFIG：`app.db.session` 在**模块导入时**就读配置建 engine，
导入之后再改配置只影响 get_settings()，engine 还指着生产库（data/cs_agent.db）。
pytest 先导入 conftest、再导入测试模块，所以这里的环境变量一定赶在第一次 import app 之前。

模型 key 一律留空：需要外呼模型的路径本来就该报"模型未配置"，
真调模型的链路交给 scripts/smoke_test.py 与 eval/run_eval.py（它们跑在真实服务上）。
"""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="cs-agent-pytest-"))
_CONFIG = _TMP / "config.test.yaml"

_CONFIG.write_text(f"""
server: {{ host: 127.0.0.1, port: 7861 }}
database: {{ url: sqlite:///{(_TMP / 'test.db').as_posix()} }}
security: {{ jwt_secret: test-secret-0123456789abcdef-0123456789abcdef, jwt_algorithm: HS256, jwt_expire_minutes: 60 }}
multi_models:
  dashscope:
    api_key: ""
    base_url: http://127.0.0.1:9/unused
    chat_model: test-chat
    embedding_model: test-embedding
    rerank_model: ""
    rerank_base_url: ""
    vl_model: ""
    temperature: 0
default_model: dashscope
llm: {{ request_timeout_seconds: 5, max_retries: 0 }}
memory: {{ short_max_tokens: 2000, long_enabled: false }}
# 扫描间隔拉长到 1 小时：测试自己调用 sweep_once，不让 lifespan 的后台任务插队
session: {{ idle_seconds: 100000, close_after_seconds: 100000, sweep_interval_seconds: 3600 }}
rag:
  chunk_size: 200
  chunk_overlap: 40
  recall_k: 10
  top_k: 3
  chroma_path: {(_TMP / 'chroma').as_posix()}
  ingest_workers: 1
mcp: {{ allowed_commands: [python, python3, py, node] }}
wecom:
  mock: true
  corp_id: ""
  corp_secret: ""
  agent_id: 0
  touser: ""
  callback_token: ""
  encoding_aes_key: ""
  workbench_url: ""
platforms:
  taobao: {{ enabled: true, mock: true, gateway: "http://127.0.0.1:9/taobao", reply_method: "", callback_verify: "sign", to_uid_field: "to_uid", content_field: "content", ack: {{ code: 0, msg: "success" }} }}
  jd: {{ enabled: true, mock: true, gateway: "http://127.0.0.1:9/jd", reply_method: "", callback_verify: "sign", to_uid_field: "to_uid", content_field: "content", ack: {{ code: 0, msg: "success" }} }}
  pdd: {{ enabled: true, mock: true, gateway: "http://127.0.0.1:9/pdd", reply_method: "", callback_verify: "sign", to_uid_field: "to_uid", content_field: "content", ack: {{ error: {{}} }} }}
  douyin: {{ enabled: true, mock: true, gateway: "http://127.0.0.1:9/douyin/", reply_method: "", callback_verify: "sign", to_uid_field: "sender_uid", content_field: "content", ack: {{ code: 0, msg: "success" }} }}
""", encoding="utf-8")

os.environ["CS_CONFIG"] = str(_CONFIG)

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlmodel import Session, SQLModel  # noqa: E402

from app.db.session import engine  # noqa: E402  导入即按上面的临时配置建库


def _truncate_all() -> None:
    """清空所有表：每个用例都从空库开始，"第一个注册账号是 admin"这类断言才可复现。"""
    with engine.begin() as conn:
        for table in reversed(SQLModel.metadata.sorted_tables):
            conn.exec_driver_sql(f'DELETE FROM "{table.name}"')


@pytest.fixture(scope="session", autouse=True)
def _database():
    import app.db.models  # noqa: F401  注册全部表到 metadata
    from app.db.session import init_db

    init_db()
    yield
    shutil.rmtree(_TMP, ignore_errors=True)


@pytest.fixture(autouse=True)
def _clean_tables(_database):
    _truncate_all()
    yield
    _truncate_all()


@pytest.fixture(scope="session")
def client(_database) -> TestClient:
    """跑完整 lifespan（启动 sweeper + 入库 worker），关停路径也因此被覆盖。"""
    from app.main import app

    with TestClient(app) as c:
        yield c


def register(client: TestClient, username: str, password: str = "secret123") -> dict:
    """注册并登录，返回 {token, headers, user}；第一个账号天然是 admin。"""
    client.post("/api/v1/auth/register", json={"username": username, "password": password})
    r = client.post("/api/v1/auth/login", json={"username": username, "password": password})
    r.raise_for_status()
    body = r.json()
    return {"token": body["access_token"], "user": body["user"],
            "headers": {"Authorization": f"Bearer {body['access_token']}"}}


@pytest.fixture
def admin(client: TestClient) -> dict:
    return register(client, "boss")


@pytest.fixture
def staff(client: TestClient, admin: dict) -> dict:
    """admin 之后注册的账号：role=user，用于断言写操作门禁。"""
    return register(client, "cs01")


@pytest.fixture
def db() -> Session:
    with Session(engine) as session:
        yield session
