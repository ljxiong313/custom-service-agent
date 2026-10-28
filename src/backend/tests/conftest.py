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
security:
  jwt_secret: test-secret-0123456789abcdef-0123456789abcdef
  jwt_algorithm: HS256
  jwt_expire_minutes: 60
  media_url_ttl_seconds: 3600
  # 单测里同一账号会连着打几十个写请求，限流会误伤；限流本身在 test_observability.py 单独验
  rate_limit: {{ enabled: false }}
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
llm: {{ request_timeout_seconds: 5, max_retries: 0, fallback: [] }}
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
  # 同上：外发重试循环默认 15s，会把用例刚写下的 failed 行真去重投一次（打到 127.0.0.1:9），
  # 用例断言 next_retry_at/attempts 就变成看时机了。重试逻辑由 test_outbox_retry.py 直接驱动。
  retry_interval_seconds: 3600
  taobao: {{ enabled: true, mock: true, gateway: "http://127.0.0.1:9/taobao", reply_method: "", callback_verify: "sign", to_uid_field: "to_uid", content_field: "content", ack: {{ code: 0, msg: "success" }} }}
  jd: {{ enabled: true, mock: true, gateway: "http://127.0.0.1:9/jd", reply_method: "", callback_verify: "sign", to_uid_field: "to_uid", content_field: "content", ack: {{ code: 0, msg: "success" }} }}
  pdd: {{ enabled: true, mock: true, gateway: "http://127.0.0.1:9/pdd", reply_method: "", callback_verify: "sign", to_uid_field: "to_uid", content_field: "content", ack: {{ error: {{}} }} }}
  douyin: {{ enabled: true, mock: true, gateway: "http://127.0.0.1:9/douyin/", reply_method: "", callback_verify: "sign", to_uid_field: "sender_uid", content_field: "content", ack: {{ code: 0, msg: "success" }} }}
""", encoding="utf-8")

os.environ["CS_CONFIG"] = str(_CONFIG)

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlmodel import Session, SQLModel, select  # noqa: E402

from app.db.models import User  # noqa: E402
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
    row = register(client, "boss")
    # 钉死角色：某些用例的参数顺序会让 buyer/staff 先注册，"第一个账号天然是 admin"
    # 就不再成立，admin fixture 必须自己保证自己是 admin。
    _set_role("boss", "admin")
    return row


def _set_role(username: str, role: str) -> None:
    """把账号角色钉死在库里。

    不走 PATCH /auth/users/{id}/role：那个接口本身要 admin，而 fixture 之间的解析顺序
    （buyer 可能排在 admin 前面）会让"谁是第一个账号"随用例漂移，角色断言就会时好时坏。
    """
    with Session(engine) as session:
        row = session.exec(select(User).where(User.username == username)).first()
        assert row is not None, f"账号 {username} 未注册"
        row.role = role
        session.add(row)
        session.commit()


@pytest.fixture
def staff(client: TestClient) -> dict:
    """真客服账号（role=staff）：覆盖运营台的全量视角（跨用户会话、工单、确认卡片）。"""
    row = register(client, "cs01")
    _set_role("cs01", "staff")
    return row


@pytest.fixture
def buyer(client: TestClient) -> dict:
    """普通用户账号（role=user）：只应看到自己的会话/工单/确认卡片。"""
    row = register(client, "buyer01")
    _set_role("buyer01", "user")
    return row


@pytest.fixture
def db() -> Session:
    with Session(engine) as session:
        yield session
