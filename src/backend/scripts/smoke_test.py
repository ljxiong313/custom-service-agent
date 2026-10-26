"""后端全链路冒烟测试（P1 RAG / P3 转人工 / P4 MCP / B1 任务队列与 RBAC / B2 就绪自检）。

运行：py scripts/smoke_test.py（需后端已在 7861 运行；演示语料按脚本位置定位，仓库根跑也行）
说明：中文断言用 httpx 直发 JSON，绕开 Windows 控制台编码问题；
向量化在 key 缺失时预期降级（error 含"向量化失败"），检索走 BM25 兜底。
B1-2 起上传/重入库/重建向量都是"排队 → 任务线程执行"，所以接口只断言受理成功，
真正的完成用 _wait_job 轮询 /knowledge/jobs（不要靠 sleep，慢机器上会假失败）。
"""
import sys
import time
from pathlib import Path

import httpx

BASE = "http://127.0.0.1:7861/api/v1"
# 演示语料按脚本自身位置定位：从仓库根、从 src/backend、从容器里跑都是同一个文件
DEMO_DIR = Path(__file__).resolve().parents[1] / "data"
ok = fail = 0

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def check(name: str, cond: bool, extra: str = ""):
    global ok, fail
    print(f"{'PASS' if cond else 'FAIL'}  {name} {extra}")
    ok, fail = ok + (1 if cond else 0), fail + (0 if cond else 1)


c = httpx.Client(timeout=60)


def _files() -> list[dict]:
    return c.get(f"{BASE}/knowledge/files", headers=H).json()


def _jobs() -> dict:
    return c.get(f"{BASE}/knowledge/jobs", headers=H).json()


def _wait_settled(file_id: int, timeout: float = 90.0) -> dict:
    """等文件状态脱离 pending/parsing（ready/failed）"""
    deadline = time.time() + timeout
    row = {}
    while time.time() < deadline:
        row = next((f for f in _files() if f["id"] == file_id), {})
        if row.get("status") not in (None, "pending", "parsing"):
            return row
        time.sleep(1.5)
    return row


def _last_job(kind: str, file_id: int) -> dict | None:
    """接口排队成功后立刻查该文件最近一个同类任务（行在请求内同步写入，无竞态）。"""
    return next((j for j in _jobs()["items"]
                 if j["kind"] == kind and j["file_id"] == file_id), None)


def _wait_job(job_id: int, timeout: float = 120.0) -> dict:
    deadline = time.time() + timeout
    row = {}
    while time.time() < deadline:
        row = next((j for j in _jobs()["items"] if j["id"] == job_id), {})
        if row.get("status") in ("done", "failed", "interrupted"):
            return row
        time.sleep(1.0)
    return row


# ---- 账号 ----
c.post(f"{BASE}/auth/register", json={"username": "tester", "password": "test123456"})
tok = c.post(f"{BASE}/auth/login", json={"username": "tester", "password": "test123456"}).json()["access_token"]
H = {"Authorization": f"Bearer {tok}"}
conv = c.post(f"{BASE}/conversations", headers=H, json={"title": "冒烟会话"}).json()

# 第二个账号专门用于 RBAC：注册时只有系统首个用户是 admin，viewer 必为普通用户
c.post(f"{BASE}/auth/register", json={"username": "viewer", "password": "view123456"})
vtok = c.post(f"{BASE}/auth/login", json={"username": "viewer", "password": "view123456"}).json()["access_token"]
V = {"Authorization": f"Bearer {vtok}"}

check("B1-7 admin 角色下发", c.get(f"{BASE}/auth/me", headers=H).json().get("role") == "admin")
check("B1-7 普通用户角色为 user", c.get(f"{BASE}/auth/me", headers=V).json().get("role") == "user")
check("B1-7 普通用户不可列用户", c.get(f"{BASE}/auth/users", headers=V).status_code == 403)
check("B1-7 普通用户不可改角色", c.patch(f"{BASE}/auth/users/1/role", headers=V,
                                     json={"role": "admin"}).status_code == 403)

# ---- P1 知识库上传（B1-2：受理即返回，入库由任务线程完成）----
# 冒烟自己上传的演示文件必须自己收尾：库里可能已有真实业务文件，
# 留下副本会让每次冒烟都往生产语料里灌重复数据。
demo_ids, upload_jobs = [], []
for name, mime in (("demo_products.xlsx", XLSX), ("demo_policy.txt", "text/plain")):
    with open(DEMO_DIR / name, "rb") as f:
        r = c.post(f"{BASE}/knowledge/files", headers=H,
                   files={"file": (name, f, mime)})
    b = r.json()
    demo_ids.append(b["id"])
    job = _last_job("upload", b["id"])
    if job:
        upload_jobs.append(job["id"])
    check(f"P1 {name} 上传受理", r.status_code == 200
          and b["status"] in ("parsing", "pending") and job is not None,
          f"status={b.get('status')} job={None if not job else job['status']}")

for jid in upload_jobs:
    jr = _wait_job(jid)
    check(f"P1 入库任务完成 job#{jid}", jr.get("status") == "done",
          f"status={jr.get('status')} {jr.get('processed')}/{jr.get('total')} err={str(jr.get('error'))[:60]}")

# ---- B1-7 普通用户读得到、写不动 ----
with open(DEMO_DIR / "demo_policy.txt", "rb") as f:
    r = c.post(f"{BASE}/knowledge/files", headers=V, files={"file": ("demo_policy.txt", f, "text/plain")})
check("B1-7 普通用户禁止上传知识库", r.status_code == 403, f"{r.status_code}")
check("B1-7 普通用户禁止启停文件",
      c.patch(f"{BASE}/knowledge/files/{demo_ids[0]}", headers=V, json={"enabled": False}).status_code == 403)
check("B1-7 普通用户禁止删除文件",
      c.delete(f"{BASE}/knowledge/files/{demo_ids[0]}", headers=V).status_code == 403)
check("B1-7 普通用户禁止重新入库",
      c.post(f"{BASE}/knowledge/files/{demo_ids[0]}/reingest", headers=V).status_code == 403)
check("B1-7 普通用户可检索", c.post(f"{BASE}/knowledge/search", headers=V,
                                json={"query": "面霜", "top_k": 1}).status_code == 200)
check("B1-7 普通用户可看任务列表", c.get(f"{BASE}/knowledge/jobs", headers=V).status_code == 200)

r = c.post(f"{BASE}/knowledge/search", headers=H, json={"query": "面霜 价格", "top_k": 3})
hits = r.json()
check("P1 BM25 检索命中商品", r.status_code == 200 and any("面霜" in h["text"] for h in hits),
      f"hits={len(hits)}")

r = c.get(f"{BASE}/knowledge/files", headers=H)
check("P1 文件列表", r.status_code == 200 and len(r.json()) >= 2)

# 禁用→启用往返（只动本次上传的演示文件，不碰用户的真实文件）
kid = demo_ids[0]
r = c.patch(f"{BASE}/knowledge/files/{kid}", headers=H, json={"enabled": False})
check("P1 禁用文件", r.json()["enabled"] is False)
c.patch(f"{BASE}/knowledge/files/{kid}", headers=H, json={"enabled": True})

# ---- P1 任务列表结构 + 重试入口 ----
jb = _jobs()
need = {"id", "file_id", "file_name", "kind", "status", "processed", "total", "error"}
check("P1 任务列表结构", jb["pending"] >= 0 and jb["items"] and need <= set(jb["items"][0]),
      f"pending={jb.get('pending')} items={len(jb.get('items', []))}")
done_job = next((j["id"] for j in jb["items"] if j["status"] == "done"), 0)
check("P1 未知任务重试 404", c.post(f"{BASE}/knowledge/jobs/999999/retry", headers=H).status_code == 404)
check("P1 完成任务不可重试", c.post(f"{BASE}/knowledge/jobs/{done_job}/retry", headers=H).status_code == 409,
      f"job={done_job}")

# ---- P1 分块可视 + 重新入库/重建向量（解析器改动后不用删文件重传、不用手跑脚本）----
txt_id = demo_ids[1]
page = c.get(f"{BASE}/knowledge/files/{txt_id}/chunks?limit=1&offset=0", headers=H).json()
check("P1 分块分页返回", page["total"] >= 1 and len(page["items"]) == 1
      and page["items"][0]["chunk_id"] > 0, f"total={page.get('total')}")
check("P1 分块向量状态可见", page["items"][0]["vectorized"] is True,
      f"vectorized={page['items'][0].get('vectorized')}")

# 重建向量：文件状态可能还是 ready（worker 还没来得及写 parsing），只能靠任务行判定
r = c.post(f"{BASE}/knowledge/files/{txt_id}/reindex", headers=H)
job = _last_job("reindex", txt_id) if r.status_code == 200 else None
check("P1 重建向量受理", r.status_code == 200 and job is not None,
      f"{r.status_code} {str(r.text)[:60]}")
jr = _wait_job(job["id"]) if job else {}
check("P1 重建向量完成", jr.get("status") == "done" and _wait_settled(txt_id)["status"] == "ready",
      f"job={jr.get('status')} err={str(jr.get('error'))[:60]}")

r = c.post(f"{BASE}/knowledge/files/{txt_id}/reingest", headers=H)
job = _last_job("reingest", txt_id) if r.status_code == 200 else None
# purge 挪进了任务里：排队中文件仍保留旧语料，所以这里只断言"已受理 + 任务已建"
check("P1 重新入库受理", r.status_code == 200 and r.json()["status"] == "pending" and job is not None,
      f"{r.status_code} {str(r.text)[:60]}")
jr = _wait_job(job["id"]) if job else {}
row = _wait_settled(txt_id)
check("P1 重新入库重建切片", jr.get("status") == "done" and row["status"] == "ready"
      and row["chunk_count"] >= 1,
      f"job={jr.get('status')} status={row['status']} chunks={row['chunk_count']} err={str(row.get('error'))[:60]}")
check("P1 重新入库后向量齐全", all(i["vectorized"] for i in c.get(
    f"{BASE}/knowledge/files/{txt_id}/chunks?limit=50", headers=H).json()["items"]))
check("P1 未知文件 404", c.post(f"{BASE}/knowledge/files/999999/reingest", headers=H).status_code == 404)
check("P1 未知文件分块 404", c.get(f"{BASE}/knowledge/files/999999/chunks", headers=H).status_code == 404)

# ---- P3 转人工闭环（mock 企微）----
r = c.post(f"{BASE}/handoff/request", headers=H, json={"conversation_id": conv["id"], "reason": "冒烟测试"})
check("P3 建工单", r.status_code == 200 and r.json()["status"] == "pending_human")
tid = r.json()["ticket_id"]

# 挂起态对话：不调模型也应返回排队提示
with c.stream("POST", f"{BASE}/chat/completions", headers=H,
              json={"conversation_id": conv["id"], "message": "在吗"}) as s:
    body = "".join(s.iter_lines())
check("P3 挂起态机器人静默", "已为您联系人工客服" in body and "event: handoff" in body)

r = c.post(f"{BASE}/handoff/tickets/{tid}/reply", headers=H, json={"text": "您好，我是人工客服小周"})
check("P3 客服回复回写", r.status_code == 200)
msgs = c.get(f"{BASE}/conversations/{conv['id']}/messages", headers=H).json()
check("P3 用户可见人工回复", any("人工客服小周" in m["content"] for m in msgs))

r = c.post(f"{BASE}/handoff/tickets/{tid}/close", headers=H)
check("P3 结单回 bot", r.json()["ok"] is True)
r = c.get(f"{BASE}/handoff/outbox", headers=H)
check("P3 mock 外发卡片记录", any("转人工工单" in str(o) for o in r.json()))

# ---- P4 MCP 发现（stdio demo server）----
server = {"name": "demo-order", "transport": "stdio",
          "command": sys.executable, "args": "scripts/demo_mcp_server.py",
          "enabled": True, "require_confirm": True}
c.post(f"{BASE}/mcp/servers", headers=H, json=server)
sid = next(s["id"] for s in c.get(f"{BASE}/mcp/servers", headers=H).json() if s["name"] == "demo-order")
r = c.post(f"{BASE}/mcp/servers/{sid}/test", headers=H)
data = r.json()
check("P4 MCP 连接发现工具", data.get("status") == "connected" and
      any(t["name"] == "query_order" for t in data.get("tools", [])),
      f"err={str(data.get('error'))[:80]}")
check("B1-7 普通用户禁止登记 MCP",
      c.post(f"{BASE}/mcp/servers", headers=V, json={**server, "name": "viewer-attempt"}).status_code == 403)
# stdio 会拉起本机进程：不在白名单的命令必须存不进去（B1-7 的 RCE 门禁）
check("B1-7 MCP 命令白名单拦 shell",
      c.post(f"{BASE}/mcp/servers", headers=H,
             json={**server, "name": "evil-attempt", "command": "powershell"}).status_code == 400)
check("B1-7 白名单外服务器未落库",
      all(s["name"] != "evil-attempt" for s in c.get(f"{BASE}/mcp/servers", headers=H).json()))
r = c.get(f"{BASE}/mcp/pending", headers=H)
check("B2-3 待确认工具列表", r.status_code == 200 and isinstance(r.json().get("items"), list))
check("B2-3 过期确认 410", c.post(f"{BASE}/mcp/confirm", headers=H,
                              json={"request_id": "nonexistent", "approved": True}).status_code == 410)

# ---- B2-1 平台就绪自检（静态，不发网络请求）----
r = c.get(f"{BASE}/platforms/readiness", headers=H)
rd = r.json()
check("B2-1 四平台就绪清单", r.status_code == 200 and len(rd.get("platforms", [])) == 4
      and all({"ready_in", "ready_out", "ready_media", "items", "risks"} <= set(p) for p in rd["platforms"]))
check("B2-1 缺出站方法被点名", "reply_method" in rd.get("summary", {}).get("missing_keys", []),
      f"missing={rd.get('summary', {}).get('missing_keys')}")
check("B2-1 自检只报有无不回显值",
      all("value" not in item for p in rd.get("platforms", []) for item in p["items"]))
r = c.get(f"{BASE}/system/status", headers=H)
st = r.json()
check("B2-3 系统状态含队列与外发计数", r.status_code == 200
      and {"queued_running", "interrupted"} <= set(st.get("ingest_jobs", {}))
      and {"skipped", "failed"} <= set(st.get("outbox", {}))
      and "mcp_pending_confirm" in st, f"{str(r.text)[:80]}")

# ---- 收尾：删除本次冒烟上传的演示文件（chunk + 向量 + 商品行一起清）----
for did in demo_ids:
    c.delete(f"{BASE}/knowledge/files/{did}", headers=H)
c.delete(f"{BASE}/conversations/{conv['id']}", headers=H)

print(f"\n===== {ok} passed, {fail} failed =====")
sys.exit(1 if fail else 0)
