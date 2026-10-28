"""P7 平台渠道冒烟：mock 回调 → 影子会话 → 关键词转人工 → 工作台回复 → 平台外发箱。

运行：CS_CONFIG=config.local.yaml py scripts/platform_smoke.py（需后端已在 7861 运行）
说明：全程 mock 模式，无需任何平台资质与真实模型 key
（普通消息会因模型 401 降级，但影子会话建档发生在模型调用之前，可独立验证）。
"""
import os
import sys
import time

import httpx

BASE = os.environ.get("CS_BASE_URL", "http://127.0.0.1:7861")
CB = f"{BASE}/api/platform"
ok = fail = 0


def check(name: str, cond: bool, extra: str = ""):
    global ok, fail
    print(f"{'PASS' if cond else 'FAIL'}  {name} {extra}")
    ok, fail = ok + (1 if cond else 0), fail + (0 if cond else 1)


def wait_for(fn, timeout=8.0):
    """轮询后台任务结果（webhook 是快速 ack + BackgroundTasks 异步处理）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if fn():
            return True
        time.sleep(0.25)
    return fn()


c = httpx.Client(timeout=60)
c.post(f"{BASE}/api/v1/auth/register", json={"username": "tester", "password": "test123456"})
tok = c.post(f"{BASE}/api/v1/auth/login", json={"username": "tester", "password": "test123456"}).json()["access_token"]
H = {"Authorization": f"Bearer {tok}"}

BUYER = f"buyer_smoke_{int(time.time())}"

# ---- 回调路由本身 ----
r = c.post(f"{CB}/xiaohongshu/callback", json={"buyer_id": "x", "text": "hi"})
check("未接入平台回调返回 404", r.status_code == 404)

r = c.post(f"{CB}/taobao/callback",
           json={"buyer_id": BUYER, "shop_id": "shop_demo", "text": "我要转人工", "msg_id": "m1"})
check("淘宝 mock 回调快速 ack", r.status_code == 200 and r.json().get("code") == 0,
      f"ack={r.text[:60]}")

# ---- 影子会话建档 + P8 确认式转人工（询问 → 买家确认 → 建单）----
created = wait_for(lambda: any(
    p["platform"] == "taobao" and p["conversations"] > 0
    for p in c.get(f"{BASE}/api/v1/platforms/status", headers=H).json()))
check("影子会话建档（平台状态可见）", created)

asked = wait_for(lambda: any(
    "是否需要为您转接人工客服" in rec["text"] for rec in
    c.get(f"{BASE}/api/v1/platforms/outbox", headers=H).json().get("taobao", [])))
check("转人工先询问买家（不直接建单）", asked)
premature = any("客户确认" in (t.get("reason") or "") for t in
                c.get(f"{BASE}/api/v1/handoff/tickets", headers=H, params={"status_filter": "open"}).json())
check("询问阶段尚未建单", not premature)

c.post(f"{CB}/taobao/callback", json={"buyer_id": BUYER, "shop_id": "shop_demo", "text": "是", "msg_id": "m1c"})
queued = wait_for(lambda: any(
    "已为您联系人工客服" in rec["text"] for rec in
    c.get(f"{BASE}/api/v1/platforms/outbox", headers=H).json().get("taobao", [])))
tickets = c.get(f"{BASE}/api/v1/handoff/tickets", headers=H, params={"status_filter": "open"}).json()
ticket = next((t for t in tickets if "客户确认转人工" in (t.get("reason") or "")), None)
check("买家确认后建单+排队提示送达", queued and ticket is not None,
      f"ticket={ticket and ticket['id']}")

# ---- 同一买家再来消息：复用会话不重复建档，机器人挂起不抢答 ----
before = sum(p["conversations"] for p in c.get(f"{BASE}/api/v1/platforms/status", headers=H).json())
c.post(f"{CB}/taobao/callback", json={"buyer_id": BUYER, "shop_id": "shop_demo", "text": "在吗", "msg_id": "m2"})
time.sleep(1.5)
after = sum(p["conversations"] for p in c.get(f"{BASE}/api/v1/platforms/status", headers=H).json())
check("同买家消息复用影子会话（不重复建档）", before == after, f"count={after}")

# ---- 客服工作台回复 → 送达平台外发箱（P7 出站闭环）----
if ticket:
    staff_text = f"您好，人工客服已接入（冒烟 {BUYER}）"
    r = c.post(f"{BASE}/api/v1/handoff/tickets/{ticket['id']}/reply", headers=H, json={"text": staff_text})
    check("客服回复接口成功", r.status_code == 200)
    delivered = wait_for(lambda: any(
        staff_text in rec["text"] for rec in
        c.get(f"{BASE}/api/v1/platforms/outbox", headers=H).json().get("taobao", [])))
    check("客服回复已送达平台（外发箱）", delivered)
    if ticket:
        r = c.post(f"{BASE}/api/v1/handoff/tickets/{ticket['id']}/close", headers=H)
        check("结单恢复 bot", r.json().get("ok") is True)
else:
    check("客服回复接口成功", False, "（无工单，跳过）")
    check("客服回复已送达平台（外发箱）", False, "（无工单，跳过）")
    check("结单恢复 bot", False, "（无工单，跳过）")

# ---- 抖店嵌套 data 解析 + 拼多多 uid 字段解析（建档即可，模型 401 不影响映射）----
r = c.post(f"{CB}/douyin/callback", json={"data": {"uid": f"dy_{BUYER}", "content": "你好", "shop_id": "shop_dy"}})
check("抖店回调 ack", r.status_code == 200)
dy_ok = wait_for(lambda: any(
    p["platform"] == "douyin" and p["conversations"] > 0
    for p in c.get(f"{BASE}/api/v1/platforms/status", headers=H).json()))
check("抖店 data 嵌套解析建档", dy_ok)

r = c.post(f"{CB}/pdd/callback", json={"uid": f"pdd_{BUYER}", "text": "你好", "shop_id": "shop_pdd"})
pdd_ok = wait_for(lambda: r.status_code == 200 and any(
    p["platform"] == "pdd" and p["conversations"] > 0
    for p in c.get(f"{BASE}/api/v1/platforms/status", headers=H).json()))
check("拼多多 uid 字段解析建档", pdd_ok, f"ack={r.text[:40]}")

# ---- P8 空闲超时生命周期（进程内时间旅行驱动 sweep，不依赖后台任务等待）----
try:
    import datetime as _dt

    sys.path.insert(0, ".")
    from sqlmodel import Session as _S
    from sqlmodel import select as _q

    from app.db.models import Conversation as _Conv
    from app.db.models import ExternalConversation as _Ext
    from app.db.models import HandoffTicket as _Tk
    from app.db.models import Message as _Msg
    from app.db.session import engine as _eng
    from app.services.lifecycle import sweep_once

    now = _dt.datetime.now(_dt.UTC)
    with _S(_eng) as s:
        ext = s.exec(_q(_Ext).where(_Ext.buyer_id == BUYER)).first()
        conv = s.get(_Conv, ext.conversation_id)
        conv.last_customer_msg_at = now - _dt.timedelta(seconds=600)
        conv.end_prompted_at = None
        s.add(conv)
        s.commit()
        conv_id = conv.id
    sweep_once()
    with _S(_eng) as s:
        conv = s.get(_Conv, conv_id)
        prompted_msg = s.exec(_q(_Msg).where(
            _Msg.conversation_id == conv_id, _Msg.content.like("%自动结束%"))).first()
        check("P8 空闲超时发送结束提示", conv.end_prompted_at is not None and prompted_msg is not None)
        conv.end_prompted_at = now - _dt.timedelta(seconds=120)
        s.add(conv)
        s.commit()
    sweep_once()
    with _S(_eng) as s:
        conv = s.get(_Conv, conv_id)
        open_tk = s.exec(_q(_Tk).where(
            _Tk.conversation_id == conv_id, _Tk.status.in_(("open", "handling")))).all()  # type: ignore[attr-defined]
        check("P8 超时终结+中断转人工（强制结单）", conv.status == "closed" and not open_tk)

    def buyer_rounds() -> list[dict]:
        return [x for x in c.get(f"{BASE}/api/v1/console/conversations", headers=H,
                                params={"limit": 200}).json() if x["buyer_id"] == BUYER]

    def msgs(cid: int) -> list:
        return c.get(f"{BASE}/api/v1/console/conversations/{cid}/messages", headers=H).json()

    old_msgs = len(msgs(conv_id))
    rounds = len(buyer_rounds())
    c.post(f"{CB}/taobao/callback", json={"buyer_id": BUYER, "shop_id": "shop_demo", "text": "还在吗", "msg_id": "m9"})
    check("P8 终结后买家重新咨询自动开新一轮", wait_for(lambda: len(buyer_rounds()) > rounds),
          f"rounds {rounds}->{len(buyer_rounds())}")
    check("P8 已终结那轮历史不被改写", len(msgs(conv_id)) == old_msgs)
    new_conv = next((x for x in buyer_rounds() if x["status"] == "active"), None)
    check("P8 新一轮买家收到 Agent 应答", new_conv is not None and
          wait_for(lambda: any(m["role"] == "assistant" for m in msgs(new_conv["id"]))))

    # 幂等：平台对同一条 msg_id 超时重推，不得重复应答、也不得再开一轮
    if new_conv:
        n_before = len(msgs(new_conv["id"]))
        rounds_before = len(buyer_rounds())
        c.post(f"{CB}/taobao/callback",
               json={"buyer_id": BUYER, "shop_id": "shop_demo", "text": "还在吗", "msg_id": "m9"})
        time.sleep(1.5)
        check("P7 重复 msg_id 幂等丢弃", len(msgs(new_conv["id"])) == n_before and
              len(buyer_rounds()) == rounds_before)
except Exception as e:  # noqa: BLE001
    check("P8 超时生命周期", False, f"{type(e).__name__}: {str(e)[:80]}")

# ---- 运营台手动建单（跨用户：会话属于影子买家，操作者是登录客服）+ 看板统计 ----
me = c.get(f"{BASE}/api/v1/auth/me", headers=H).json()["id"]
buyer_convs = [x for x in c.get(f"{BASE}/api/v1/console/conversations", headers=H,
                                params={"limit": 200}).json() if x["buyer_id"] == BUYER]
active_round = next((x for x in buyer_convs if x["status"] == "active"), None)
closed_round = next((x for x in buyer_convs if x["status"] == "closed"), None)
if active_round:
    r = c.post(f"{BASE}/api/v1/handoff/tickets", headers=H,
               json={"conversation_id": active_round["id"], "reason": "运营判断需人工介入（冒烟）"})
    tk = r.json() if r.status_code == 200 else {}
    check("运营台对买家会话手动建单", r.status_code == 200 and tk.get("status") == "pending_human",
          f"{r.status_code} {str(r.text)[:60]}")
    again = c.post(f"{BASE}/api/v1/handoff/tickets", headers=H,
                   json={"conversation_id": active_round["id"], "reason": "重复开单"}).json()
    check("同会话未结工单复用（不重复开）", again.get("ticket_id") == tk.get("ticket_id"),
          f"{again.get('ticket_id')} vs {tk.get('ticket_id')}")
    row = next((x for x in c.get(f"{BASE}/api/v1/handoff/tickets", headers=H).json()
                if x["id"] == tk.get("ticket_id")), {})
    check("工单归属记买家而非客服", row.get("user_id") not in (None, me), f"user_id={row.get('user_id')}")
    stats = c.get(f"{BASE}/api/v1/handoff/stats", headers=H).json()
    check("看板统计待接/今日新建", stats["open"] >= 1 and stats["created_today"] >= 1, str(stats)[:80])
    c.post(f"{BASE}/api/v1/handoff/tickets/{tk['ticket_id']}/reply", headers=H,
           json={"text": "人工跟进中（冒烟）"})
    stats = c.get(f"{BASE}/api/v1/handoff/stats", headers=H).json()
    check("看板首响时长统计到人工回复", stats["handling"] >= 1
          and isinstance(stats["avg_first_response_seconds"], (int, float)), str(stats)[:90])
    c.post(f"{BASE}/api/v1/handoff/tickets/{tk['ticket_id']}/close", headers=H)
    check("看板结单数统计", c.get(f"{BASE}/api/v1/handoff/stats", headers=H).json()["closed"] >= 1)
else:
    check("运营台对买家会话手动建单", False, "（未找到活跃轮次会话）")
if closed_round:
    r = c.post(f"{BASE}/api/v1/handoff/tickets", headers=H,
               json={"conversation_id": closed_round["id"], "reason": "对已终结会话开单"})
    check("已终结会话拒绝建单", r.status_code == 409, f"{r.status_code} {str(r.text)[:60]}")

# ---- 运营台代答附件 → 平台媒体出站（B2-2：mock 下也留 kind=media 的痕迹）----
if active_round:
    r = c.post(f"{BASE}/api/v1/console/conversations/{active_round['id']}/reply", headers=H,
               json={"text": "这是库存图（冒烟）",
                     "attachments": [{"url": "/api/files/demo.png", "name": "demo.png", "type": "image"}]})
    check("运营台代答带附件成功", r.status_code == 200, f"{r.status_code} {str(r.text)[:60]}")
    media = wait_for(lambda: any(
        rec.get("kind") == "media" for rec in
        c.get(f"{BASE}/api/v1/platforms/outbox", headers=H).json().get("taobao", [])))
    check("附件走媒体出站（外发箱有 media 记录）", media)
else:
    check("运营台代答带附件成功", False, "（未找到活跃轮次会话）")
    check("附件走媒体出站（外发箱有 media 记录）", False, "（未找到活跃轮次会话）")

# ---- 管理接口安全：状态只回布尔/网关，不含任何密钥 ----
status_body = c.get(f"{BASE}/api/v1/platforms/status", headers=H).text
check("状态接口不回显密钥", all(k not in status_body for k in ("app_secret", "access_token")))

print(f"\n===== {ok} passed, {fail} failed =====")
sys.exit(1 if fail else 0)
