"""演示数据种子的离线单测：灌出来的数据要"每个界面都有得看"，且时间口径不许串时区。

为什么值得单测：`scripts/seed_demo_data.py` 是交付演示、界面截图与验收走查的唯一入口。
它一旦悄悄退化——改模型字段后少灌了一半、工单时间排到未来、或者往真实库里写了假数据——
第一个发现的人往往是正在给客户演示的那一个。这里钉住四件事：
界面覆盖度、时间一律 UTC 且不在未来、看板口径能算出正数、以及不可逆操作的护栏。
"""
from __future__ import annotations

import datetime
import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, func, select

from app.db.models import (
    Chunk,
    Conversation,
    HandoffTicket,
    KnowledgeFile,
    LlmCall,
    MemoryFact,
    Message,
    OutboxRecord,
    ToolConfirmRequest,
    User,
)

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "seed_demo_data.py"

API = "/api/v1"


def _load_module() -> Any:
    """按路径加载脚本（scripts/ 不是包，也不该塞进 sys.path 影响别的用例）。"""
    spec = importlib.util.spec_from_file_location("seed_demo_data_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def seed() -> Any:
    return _load_module()


@pytest.fixture
def demo_settings(seed: Any, tmp_path: Path,
                  monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """把 `ensure_demo_config` 真正写出的演示参数注入当前 Settings。

    单测配置既没有 llm.pricing，chunk_size 也是默认的 200：
    前者会让看板成本列整列「未配价」，后者会让一份 FAQ 只切出 1 片。
    这里借演示配置自己的值，顺带验证它确实覆盖了 seed_usage 用到的模型名。
    """
    import yaml

    from app.config import get_settings

    monkeypatch.setattr(seed, "BACKEND_ROOT", tmp_path)
    monkeypatch.setenv("CS_CONFIG", "untouched")
    cfg = yaml.safe_load(seed.ensure_demo_config(7899).read_text(encoding="utf-8"))
    settings = get_settings()
    monkeypatch.setitem(settings.setdefault("llm", {}), "pricing", cfg["llm"]["pricing"])
    monkeypatch.setitem(settings.setdefault("rag", {}), "chunk_size", cfg["rag"]["chunk_size"])
    monkeypatch.setitem(settings["rag"], "chunk_overlap", cfg["rag"]["chunk_overlap"])
    return cfg


@pytest.fixture
def seeded(seed: Any, db: Session, demo_settings: dict[str, Any]) -> dict[str, Any]:
    """每个用例都从空库开始（conftest 的 _clean_tables），所以这里逐用例灌一次。"""
    return seed.seed_all(db)


def test_seed_all_covers_every_screen(seed: Any, db: Session, seeded: dict[str, Any]) -> None:
    """覆盖度：六个界面各自依赖的表都必须有行，少一张表就意味着某页开天窗。"""
    assert seeded["新建账号"] >= 3
    assert seeded["会话"] >= 15
    assert seeded["消息"] >= 60
    assert seeded["工单与外发"] >= 4
    assert seeded["记忆条目"] >= 3
    assert seeded["满意度评分"] >= 4
    assert seeded["模型用量流水"] >= 100
    for model, screen in ((User, "设置-成员"), (Conversation, "运营台"), (Message, "对话"),
                          (HandoffTicket, "转人工工作台"), (OutboxRecord, "外发记录"),
                          (KnowledgeFile, "知识库"), (Chunk, "分块/引用"),
                          (MemoryFact, "记忆"), (LlmCall, "用量与成本"),
                          (ToolConfirmRequest, "MCP 确认卡片")):
        count = int(db.exec(select(func.count()).select_from(model)).one())
        assert count > 0, f"{screen} 对应的 {model.__name__} 是空的"


def test_seeded_time_is_utc_aware_and_never_in_future(seed: Any, db: Session,
                                                      seeded: dict[str, Any]) -> None:
    """时间口径回归位：SQLite 存的是无时区字符串，写入只要有一次 naive 本地时间，
    前端按本地时区一渲染就是整屏 +8 小时；间隔拉太大会把消息排到"未来"，
    看板的"平均会话时长"随之变成负数。两头都要夹住。
    """
    now = datetime.datetime.now(datetime.UTC)
    checked = 0
    for model in (Conversation, Message, HandoffTicket, LlmCall, MemoryFact, OutboxRecord):
        for row in db.exec(select(model)).all():
            created = row.created_at
            assert created.tzinfo is not None, f"{model.__name__} 写成了 naive 时间"
            assert created.utcoffset() == datetime.timedelta(0), \
                f"{model.__name__} 不是 UTC：{created.isoformat()}"
            assert created <= now + datetime.timedelta(seconds=5), \
                f"{model.__name__} #{row.id} 排在当下之后"
            assert created >= now - datetime.timedelta(days=31)
            checked += 1
    assert checked > 100


def test_closed_at_is_after_ticket_creation(db: Session, seeded: dict[str, Any]) -> None:
    """结单时间早于建单时间会把"平均处理时长"算成负数，看板不会报错只会显示假数。"""
    tickets = db.exec(select(HandoffTicket).where(HandoffTicket.closed_at.is_not(None))).all()  # type: ignore[union-attr]
    assert tickets
    for ticket in tickets:
        assert ticket.closed_at is not None and ticket.closed_at >= ticket.created_at


def test_messages_within_a_conversation_are_chronological(db: Session,
                                                          seeded: dict[str, Any]) -> None:
    conv_ids = [c.id for c in db.exec(select(Conversation)).all()]
    assert conv_ids
    for cid in conv_ids:
        stamps = [m.created_at for m in db.exec(
            select(Message).where(Message.conversation_id == cid).order_by(Message.id)).all()]  # type: ignore[arg-type]
        assert len(stamps) >= 2, f"会话 {cid} 只有 {len(stamps)} 条消息，运营台点开是空的"
        assert stamps == sorted(stamps), f"会话 {cid} 的消息时间不是递增"


def test_dashboard_computes_positive_numbers_from_seed(seed: Any, client: TestClient,
                                                       admin: dict[str, Any], db: Session,
                                                       seeded: dict[str, Any]) -> None:
    """种子的最终验收标准是"看板能算出可信数字"，所以直接打真实接口而不是复算一遍。"""
    r = client.get(f"{API}/insights/dashboard", headers=admin["headers"], params={"days": 30})
    assert r.status_code == 200, r.text
    body = r.json()

    conv = body["conversations"]
    assert conv["conversations"] >= seeded["会话"] - 5  # 窗口外的老会话允许被切掉
    for key in ("bot_resolved_rate", "handoff_rate", "close_rate"):
        assert conv[key] is None or 0.0 <= conv[key] <= 1.0, key

    human = body["handoff"]
    assert human["tickets"] >= 1
    assert human["responded"] >= 1, "没有任何工单带人工回复，首响列会整列空着"
    assert human["avg_first_response_seconds"] is not None and human["avg_first_response_seconds"] > 0
    assert human["avg_handling_seconds"] is not None and human["avg_handling_seconds"] > 0

    dur = body["session_duration"]
    assert dur["avg_session_seconds"] is not None and dur["avg_session_seconds"] > 0
    assert dur["median_session_seconds"] is not None and dur["median_session_seconds"] > 0

    sat = body["satisfaction"]
    assert sat["rated"] >= 4
    assert sum(sat["distribution"].values()) == sat["rated"]
    assert 1 <= sat["avg_rating"] <= 5
    # 演示数据刻意做成两极分化，全 5 分说明评分剧本没生效
    assert len([v for v in sat["distribution"].values() if v]) >= 3

    llm = body["llm"]
    assert llm["totals"]["calls"] >= 100
    assert llm["pricing_configured"] is True
    assert llm["totals"]["cost_est"] is not None and llm["totals"]["cost_est"] > 0, \
        "成本为 None 说明演示配置的 llm.pricing 没覆盖 seed_usage 用到的模型名，看板会整列显示「未配价」"


def test_usage_rows_have_realistic_token_magnitudes(seed: Any, db: Session,
                                                    seeded: dict[str, Any]) -> None:
    """token 量级是演示数据最容易失真的一项：几十 token 的 chat 调用一眼就是假的。"""
    rows = db.exec(select(LlmCall)).all()
    assert rows
    for row in rows:
        lo, hi = seed._TOKEN_RANGE_BY_KIND[row.kind]
        assert lo <= row.input_tokens <= hi, f"{row.kind} 输入 token {row.input_tokens} 不在量级内"
        assert row.total_tokens == row.input_tokens + row.output_tokens
        assert row.latency_ms > 0
        if row.status == "ok":
            assert row.cost_est is not None
    assert any(r.status == "error" for r in rows), "没有失败样本，看板的错误率永远是 0%"


def test_knowledge_files_are_ready_with_chunks(db: Session, seeded: dict[str, Any]) -> None:
    """知识库页与引用卡片都读 Chunk 表：切片为 0 的"ready"文件等于没内容。"""
    files = db.exec(select(KnowledgeFile)).all()
    assert len(files) >= 4
    broken = [f for f in files if f.status != "ready"]
    assert len(broken) <= 1, "演示数据只允许保留一个 failed 样本（给「重新入库」按钮当靶子）"
    for f in broken:
        assert f.error, "failed 文件必须带可读的 error，否则走查时会以为是脚本坏了"
    for f in [x for x in files if x.status == "ready"]:
        n = len(db.exec(select(Chunk).where(Chunk.file_id == f.id)).all())
        assert n >= 2, f"{f.name} 只切出 {n} 片，分块页没内容可看"
        assert f.chunk_count == n, f"{f.name} 的 chunk_count 与实际切片数不一致"


def test_pending_confirm_card_is_inside_ttl(seed: Any, db: Session,
                                            seeded: dict[str, Any]) -> None:
    """确认卡片 TTL 只有 180 秒：灌完数几分钟后必然消失，所以 --rearm 是截图前的固定动作。"""
    pending = db.exec(select(ToolConfirmRequest)
                      .where(ToolConfirmRequest.status == "pending")).all()
    assert len(pending) >= 1
    assert seed.rearm_pending(db) == len(pending)
    db.expire_all()
    for row in db.exec(select(ToolConfirmRequest)
                       .where(ToolConfirmRequest.status == "pending")).all():
        age = (datetime.datetime.now(datetime.UTC) - row.created_at).total_seconds()
        assert 0 <= age < seed_confirm_ttl(), "rearm 之后卡片仍然超龄，MCP 页会是空的"


def seed_confirm_ttl() -> float:
    from app.services.mcp.confirm import TTL_SECONDS

    return TTL_SECONDS


def test_ensure_demo_config_stays_out_of_the_real_data_dir(seed: Any, tmp_path: Path,
                                                           monkeypatch: pytest.MonkeyPatch) -> None:
    """--serve 的隔离性：库、Chroma、uploads 与配置都必须落在 data/demo/ 下，
    否则一次演示就会盖掉开发者本机 `data/cs_agent.db` 里的真数据。
    """
    monkeypatch.setattr(seed, "BACKEND_ROOT", tmp_path)
    monkeypatch.setenv("CS_CONFIG", "untouched")
    cfg_path = seed.ensure_demo_config(7870)

    assert cfg_path == tmp_path / "data" / "demo" / "config.demo.yaml"
    assert cfg_path.exists()
    import yaml

    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    assert cfg["server"]["port"] == 7870
    assert cfg["server"]["env"] == "dev"
    assert cfg["server"]["serve_frontend"] is True
    assert cfg["server"]["public_base_url"] == "http://127.0.0.1:7870"
    assert "cs_demo.db" in cfg["database"]["url"]
    assert "cs_agent.db" not in cfg["database"]["url"]
    assert (tmp_path / "data" / "demo").as_posix() in cfg["database"]["url"]
    assert cfg["rag"]["chroma_path"].endswith("data/demo/chroma")
    assert cfg["rag"]["chunk_size"] < 500, "演示库要能切出多片，否则分块页是空的"
    assert cfg["llm"]["pricing"]["dashscope"]["default"], "没有价目，看板成本列显示「未配价」"
    assert cfg["session"]["idle_seconds"] > 100000, "阈值太小会让 sweeper 在截图途中自动结单"
    assert cfg["security"]["rate_limit"]["enabled"] is False
    assert all(cfg["platforms"][name]["mock"] is True for name in ("taobao", "jd", "pdd", "douyin"))
    assert cfg["wecom"]["mock"] is True


def test_demo_db_guard_refuses_real_data(seed: Any, db: Session,
                                         seeded: dict[str, Any]) -> None:
    """不可逆操作的保护栏：库里只要有一个不属于演示账号的会话，判定就必须拒绝。"""
    ok, _ = seed._looks_like_demo_db(db)
    assert ok, "刚灌完的演示库必须仍然放行（否则 --reset 与重复灌都会被自己拦住）"

    outsider = User(username="real_owner", hashed_password="x")
    db.add(outsider)
    db.commit()
    db.add(Conversation(user_id=outsider.id, title="真实客户会话"))
    db.commit()
    ok, why = seed._looks_like_demo_db(db)
    assert not ok and "不属于演示账号" in why


def test_demo_db_guard_refuses_untouched_production_db(seed: Any, db: Session) -> None:
    """一个演示账号都没有的库（开发者本机刚跑起来的 cs_agent.db）必须拒绝。"""
    db.add(User(username="someone", hashed_password="x"))
    db.commit()
    ok, why = seed._looks_like_demo_db(db)
    assert not ok and "没有演示账号" in why


def test_seed_is_idempotent_enough_to_re_run(seed: Any, db: Session,
                                             seeded: dict[str, Any]) -> None:
    """重复灌是"追加"而不是报错（脚本摘要里也这么写），所以第二次跑不能崩、不能改角色。

    `seed_all` 返回的"会话"是本次剧本条数、不是库内总量，所以这里数表里的行。
    """
    before = int(db.exec(select(func.count()).select_from(Conversation)).one())
    again = seed.seed_all(db)
    assert again["新建账号"] == 0, "账号已存在时不该再建，也不该把已有账号改属性"
    after = int(db.exec(select(func.count()).select_from(Conversation)).one())
    assert after == before * 2, f"会话应当翻倍追加，实际 {before} -> {after}"
    assert db.exec(select(HandoffTicket).where(HandoffTicket.status == "open")).all()
